from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Stage 10, local post-processing: score every improvement event for benchmark use and write a report.
# Reads stage 08 (events), 07 and 09 (patch text), 03 + 04 (trees, project dirs) read-only; recomputes
# everything each run; no network.
EVENTS_DB_PATH = Path("data/cache/improvement_events/state.sqlite")
HISTORY_DB_PATH = Path("data/cache/github_repo_history/state.sqlite")
COMMIT_FILES_DB_PATH = Path("data/cache/github_commit_files/state.sqlite")
TREES_DB_PATH = Path("data/cache/github_trees/state.sqlite")
FILTER_DB_PATH = Path("data/cache/filter_kicad_projects/state.sqlite")
CACHE_DIR = Path("data/cache/event_quality")
DB_PATH = CACHE_DIR / "state.sqlite"
REPORT_DIR = Path("reports")
REPORT_PATH = REPORT_DIR / "event_quality.md"
SEED_CSV_PATH = REPORT_DIR / "seed_issue_driven_prs.csv"
SCHEMA_VERSION = 1

# Filter thresholds. Text: cleaned body length and template / TODO / reference-only detection.
MIN_BODY_CHARS = 100
MAX_TODO_LINE_RATIO = 0.5
TEMPLATE_MARKERS = ("please check if the pull request", "pull request template", "## checklist", "type of change")
# Size: total added+deleted lines over the event's KiCad files, and total changed files of the change.
# A small layout edit already rewrites 1000+ lines of .kicad_pcb, so the cap only removes re-imports and bulk rewrites.
MAX_KICAD_LINES = 8000
MAX_CHANGED_FILES = 40
# Patch semantics: changed lines whose leading token is one of these are save-time churn, not design.
CHURN_TOKENS = {
    "uuid", "tstamp", "version", "generator", "generator_version", "embedded_fonts", "paper", "title_block",
    "date", "rev", "comment", "tedit", "kicad_sch", "kicad_pcb", "kicad_pro", "lib_symbols", "sheet_instances",
    "symbol_instances", "path", "page", "instances", "project", "general", "thickness", "setup",
}
NEUTRAL_LINE = re.compile(r"^[+-]\s*[()\s]*$")
TOKEN_RE = re.compile(r"^[+-]\s*\(?\s*([A-Za-z_]+)")
MIN_SEMANTIC_LINES = 5
# Per-repo quota on the final pool so tool-generated repos do not dominate.
REPO_QUOTA = 20
MODEL_3D_SUFFIXES = (".step", ".stp", ".wrl", ".stl", ".iges", ".igs", ".3mf", ".obj", ".f3d", ".fcstd", ".scad")

CHECKLIST_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]", re.MULTILINE)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
QUOTE_RE = re.compile(r"^\s*>.*$", re.MULTILINE)
ISSUE_REF_RE = re.compile(r"(?<![\w/])#\d+\b")
URL_RE = re.compile(r"https?://\S+")
CO_AUTHOR_RE = re.compile(r"^\s*(co-authored-by|signed-off-by):.*$", re.IGNORECASE | re.MULTILINE)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def setup_db() -> sqlite3.Connection:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    with db:
        db.execute("pragma journal_mode = wal")
        db.execute("pragma synchronous = normal")
        db.executescript(
            """
            create table if not exists meta (
              key text primary key,
              value text not null
            );

            -- event_quality: one row per improvement event with every filter flag and the final tier.
            -- tier A: all flags pass and an issue is linked; B: all flags pass; C: text/size/modified pass
            -- but patch text was unavailable so semantics are unknown; X: excluded (see reasons_json).
            create table if not exists event_quality (
              event_id integer primary key,
              repo_id integer not null,
              repo_full_name text not null,
              kind text not null,
              merged integer not null,
              in_qualified_project integer not null,
              body_clean_chars integer not null,
              todo_line_ratio real not null,
              is_template integer not null,
              is_refs_only integer not null,
              text_ok integer not null,
              kicad_lines integer,
              changed_file_count integer,
              size_ok integer not null,
              pcb_changed integer not null,
              modified_only integer not null,
              patch_available integer not null,
              semantic_lines integer,
              churn_lines integer,
              semantic_ok integer,
              has_issue integer not null,
              tier text not null,
              reasons_json text not null,
              rank_in_repo integer,
              in_quota integer not null
            );

            -- repo_3d_models: 3D model files found in each qualified repo's tree.
            create table if not exists repo_3d_models (
              repo_id integer primary key,
              repo_full_name text not null,
              model_file_count integer not null,
              in_project_dir_count integer not null,
              suffix_counts_json text not null
            );

            create index if not exists idx_eq_tier on event_quality(tier, kind, in_quota);
            create index if not exists idx_eq_repo on event_quality(repo_id);
            """
        )
        db.executemany(
            "insert or replace into meta (key, value) values (?, ?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("thresholds_json", json.dumps({
                    "min_body_chars": MIN_BODY_CHARS, "max_todo_line_ratio": MAX_TODO_LINE_RATIO,
                    "max_kicad_lines": MAX_KICAD_LINES, "max_changed_files": MAX_CHANGED_FILES,
                    "min_semantic_lines": MIN_SEMANTIC_LINES, "repo_quota": REPO_QUOTA,
                    "churn_tokens": sorted(CHURN_TOKENS), "model_3d_suffixes": list(MODEL_3D_SUFFIXES),
                })),
            ],
        )
    return db


def clean_body(body: str | None) -> tuple[str, float, bool, bool]:
    # Returns (cleaned text, TODO line ratio, looks like a PR template, only issue references / urls).
    raw = (body or "").replace("\r", "")
    lower = raw.lower()
    is_template = any(marker in lower for marker in TEMPLATE_MARKERS) or len(CHECKLIST_RE.findall(raw)) >= 3
    text = HTML_COMMENT_RE.sub("", raw)
    text = CHECKLIST_RE.sub("", text)
    text = QUOTE_RE.sub("", text)
    text = CO_AUTHOR_RE.sub("", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    todo_lines = sum(1 for line in lines if line.lower().startswith(("todo", "- todo", "* todo", "[ ]")))
    todo_ratio = todo_lines / len(lines) if lines else 0.0
    cleaned = "\n".join(lines)
    without_refs = URL_RE.sub("", ISSUE_REF_RE.sub("", cleaned))
    has_refs = bool(ISSUE_REF_RE.search(cleaned) or URL_RE.search(cleaned))
    refs_only = has_refs and len(re.sub(r"[\s#\d,.;:()\[\]-]", "", without_refs)) < 20
    return cleaned, todo_ratio, is_template, refs_only


def patch_semantics(patch: str) -> tuple[int, int]:
    semantic = churn = 0
    for line in patch.splitlines():
        if not line or line[0] not in "+-" or line.startswith(("+++", "---")) or NEUTRAL_LINE.match(line):
            continue
        match = TOKEN_RE.match(line)
        token = match.group(1).lower() if match else ""
        if token in CHURN_TOKENS:
            churn += 1
        else:
            semantic += 1
    return semantic, churn


def load_patches(db: sqlite3.Connection, requests: dict[int, set[str]]) -> dict[tuple[int, str], str | None]:
    # {(api_cache_id, filename): patch or None when GitHub omitted it}
    out: dict[tuple[int, str], str | None] = {}
    for cache_id, filenames in requests.items():
        row = db.execute("select response_json from api_cache where id = ?", (cache_id,)).fetchone()
        if row is None:
            continue
        data = json.loads(row[0])
        files = data.get("files") if isinstance(data, dict) else data
        for f in files or []:
            if f.get("filename") in filenames:
                out[(cache_id, f["filename"])] = f.get("patch")
    return out


def analyze_3d_models(store: sqlite3.Connection) -> dict[str, Any]:
    with closing(open_ro(FILTER_DB_PATH)) as filter_db:
        project_dirs: dict[int, list[str]] = defaultdict(list)
        names: dict[int, str] = {}
        for row in filter_db.execute("select repo_id, repo_full_name, project_dir from qualified_projects"):
            project_dirs[int(row["repo_id"])].append(str(row["project_dir"]))
            names[int(row["repo_id"])] = str(row["repo_full_name"])
    rows = []
    with closing(open_ro(TREES_DB_PATH)) as trees:
        for row in trees.execute("select t.repo_id, a.response_json from repo_trees t join api_cache a on a.id = t.api_cache_id where t.truncated = 0"):
            repo_id = int(row["repo_id"])
            if repo_id not in project_dirs:
                continue
            counts: Counter[str] = Counter()
            in_dir = 0
            dirs = project_dirs[repo_id]
            for entry in json.loads(row["response_json"])["tree"]:
                if entry.get("type") != "blob":
                    continue
                path = entry["path"]
                lower = path.lower()
                suffix = next((s for s in MODEL_3D_SUFFIXES if lower.endswith(s)), None)
                if suffix is None:
                    continue
                counts[suffix] += 1
                if any(d == "" or path.startswith(d + "/") for d in dirs):
                    in_dir += 1
            if counts:
                rows.append((repo_id, names[repo_id], sum(counts.values()), in_dir, json.dumps(dict(counts))))
    with store:
        store.execute("delete from repo_3d_models")
        store.executemany("insert into repo_3d_models values (?, ?, ?, ?, ?)", rows)
    suffix_totals: Counter[str] = Counter()
    for r in rows:
        suffix_totals.update(json.loads(r[4]))
    return {
        "qualified_repos": len(project_dirs),
        "repos_with_3d_models": len(rows),
        "repos_with_3d_models_in_project_dir": sum(1 for r in rows if r[3] > 0),
        "suffix_totals": dict(suffix_totals.most_common()),
    }


def analyze_events(store: sqlite3.Connection) -> dict[str, Any]:
    events = []
    patch_requests: dict[str, dict[int, set[str]]] = {"pull_request": defaultdict(set), "commit": defaultdict(set)}
    file_cache_ids: dict[tuple[int, str, str], int] = {}
    with closing(open_ro(EVENTS_DB_PATH)) as edb:
        pr_files = {(int(r["repo_id"]), int(r["number"]), r["filename"]): int(r["api_cache_id"])
                    for r in edb.execute("select repo_id, number, filename, api_cache_id from pull_request_files where lower(filename) like '%.kicad_%'")}
        commit_files = {(int(r["repo_id"]), r["sha"], r["filename"]): int(r["api_cache_id"])
                        for r in edb.execute("select repo_id, sha, filename, api_cache_id from commit_files where lower(filename) like '%.kicad_%'")}
        for r in edb.execute("select * from improvement_events"):
            cleaned, todo_ratio, is_template, refs_only = clean_body(f"{r['title'] or ''}\n{r['body'] or ''}" if r["kind"] == "commit" else r["body"])
            text_ok = len(cleaned) >= MIN_BODY_CHARS and todo_ratio < MAX_TODO_LINE_RATIO and not is_template and not refs_only
            files = json.loads(r["kicad_files_json"] or "[]")
            kicad_lines = sum((f.get("additions") or 0) + (f.get("deletions") or 0) for f in files) if r["files_known"] else None
            size_ok = bool(r["files_known"]) and kicad_lines <= MAX_KICAD_LINES and (r["changed_file_count"] or 0) <= MAX_CHANGED_FILES
            pcb_changed = (r["kicad_pcb_count"] or 0) > 0
            modified_only = bool(files) and all(f.get("status") == "modified" for f in files)
            has_issue = r["linked_issues_json"] != "[]"
            ev = {
                "event_id": int(r["id"]), "repo_id": int(r["repo_id"]), "repo_full_name": r["repo_full_name"], "kind": r["kind"],
                "merged": int(r["merged"]), "in_qualified_project": int(r["in_qualified_project"]),
                "body_clean_chars": len(cleaned), "todo_line_ratio": round(todo_ratio, 3), "is_template": int(is_template), "is_refs_only": int(refs_only),
                "text_ok": int(text_ok), "kicad_lines": kicad_lines, "changed_file_count": r["changed_file_count"], "size_ok": int(size_ok),
                "pcb_changed": int(pcb_changed), "modified_only": int(modified_only), "has_issue": int(has_issue),
                "files": files, "number": r["number"], "sha": r["sha"], "title": r["title"], "html_url": r["html_url"], "in_qualified": int(r["in_qualified_project"]),
            }
            events.append(ev)
            # Only events that pass the cheap filters need their patches read.
            if text_ok and size_ok and modified_only and pcb_changed and r["in_qualified_project"] and (r["kind"] == "commit" or r["merged"]):
                for f in files:
                    key = (ev["repo_id"], int(r["number"]), f["path"]) if r["kind"] == "pull_request" else (ev["repo_id"], r["sha"], f["path"])
                    cache_id = (pr_files if r["kind"] == "pull_request" else commit_files).get(key)
                    if cache_id is not None:
                        patch_requests[r["kind"]][cache_id].add(f["path"])
                        file_cache_ids[(ev["event_id"], r["kind"], f["path"])] = cache_id
    print(f"events loaded: {len(events)}; patch pages to read: pr={len(patch_requests['pull_request'])} commit={len(patch_requests['commit'])}", flush=True)

    patches: dict[tuple[int, str], str | None] = {}
    with closing(open_ro(HISTORY_DB_PATH)) as hdb:
        patches.update(load_patches(hdb, patch_requests["pull_request"]))
    with closing(open_ro(COMMIT_FILES_DB_PATH)) as cdb:
        patches.update({("c", k[0], k[1]): v for k, v in load_patches(cdb, patch_requests["commit"]).items()})
    print(f"patches loaded: {len(patches)}", flush=True)

    rows = []
    for ev in events:
        semantic = churn = 0
        found = missing = 0
        for f in ev["files"]:
            cache_id = file_cache_ids.get((ev["event_id"], ev["kind"], f["path"]))
            if cache_id is None:
                continue
            key = (cache_id, f["path"]) if ev["kind"] == "pull_request" else ("c", cache_id, f["path"])
            patch = patches.get(key)
            if patch is None:
                missing += 1
                continue
            found += 1
            s, c = patch_semantics(patch)
            semantic += s
            churn += c
        patch_available = int(found > 0 and missing == 0)
        semantic_ok = None if not patch_available else int(semantic >= MIN_SEMANTIC_LINES)

        reasons = []
        eligible = ev["in_qualified"] and ev["pcb_changed"] and (ev["kind"] == "commit" or ev["merged"])
        if not ev["in_qualified"]:
            reasons.append("outside_qualified_project")
        if not ev["pcb_changed"]:
            reasons.append("no_pcb_change")
        if ev["kind"] == "pull_request" and not ev["merged"]:
            reasons.append("pr_not_merged")
        if not ev["text_ok"]:
            reasons.append("text")
        if not ev["size_ok"]:
            reasons.append("size")
        if not ev["modified_only"]:
            reasons.append("not_modified_only")
        if eligible and ev["text_ok"] and ev["size_ok"] and ev["modified_only"]:
            if not patch_available:
                tier = "C"
            elif semantic_ok:
                tier = "A" if ev["has_issue"] else "B"
            else:
                tier = "X"
                reasons.append("save_only_churn")
        else:
            tier = "X"
        rows.append({**ev, "patch_available": patch_available, "semantic_lines": semantic if patch_available else None,
                     "churn_lines": churn if patch_available else None, "semantic_ok": semantic_ok, "tier": tier, "reasons": reasons})

    # Per-repo quota over tiers A/B/C: rank by tier then cleaned text length.
    tier_rank = {"A": 0, "B": 1, "C": 2}
    by_repo: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["tier"] in tier_rank:
            by_repo[row["repo_id"]].append(row)
    for repo_rows in by_repo.values():
        repo_rows.sort(key=lambda x: (tier_rank[x["tier"]], -x["body_clean_chars"]))
        for rank, row in enumerate(repo_rows, start=1):
            row["rank_in_repo"] = rank
            row["in_quota"] = int(rank <= REPO_QUOTA)

    with store:
        store.execute("delete from event_quality")
        store.executemany(
            """insert into event_quality values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (r["event_id"], r["repo_id"], r["repo_full_name"], r["kind"], r["merged"], r["in_qualified_project"],
                 r["body_clean_chars"], r["todo_line_ratio"], r["is_template"], r["is_refs_only"], r["text_ok"],
                 r["kicad_lines"], r["changed_file_count"], r["size_ok"], r["pcb_changed"], r["modified_only"],
                 r["patch_available"], r["semantic_lines"], r["churn_lines"], r["semantic_ok"], r["has_issue"],
                 r["tier"], json.dumps(r["reasons"]), r.get("rank_in_repo"), r.get("in_quota", 0))
                for r in rows
            ],
        )
    return {"rows": rows}


def funnel(rows: list[dict[str, Any]], kind: str) -> list[tuple[str, int, int]]:
    steps = [
        ("all events", lambda r: True),
        ("inside a qualified project dir", lambda r: r["in_qualified"]),
        ("PR merged (commits always pass)", lambda r: r["kind"] == "commit" or r["merged"]),
        (".kicad_pcb changed", lambda r: r["pcb_changed"]),
        ("text: cleaned body >= 100 chars, no template / TODO / refs-only", lambda r: r["text_ok"]),
        (f"size: KiCad lines <= {MAX_KICAD_LINES} and changed files <= {MAX_CHANGED_FILES}", lambda r: r["size_ok"]),
        ("all KiCad files modified (no add / rename / remove)", lambda r: r["modified_only"]),
        ("patch text available", lambda r: r["patch_available"]),
        ("semantic changed lines >= 5 (not save-only churn)", lambda r: r["semantic_ok"]),
        (f"within per-repo quota of {REPO_QUOTA}", lambda r: r.get("in_quota") and r["tier"] in ("A", "B")),
    ]
    out = []
    current = [r for r in rows if r["kind"] == kind]
    for label, pred in steps:
        current = [r for r in current if pred(r)]
        out.append((label, len(current), len({r["repo_id"] for r in current})))
    return out


def write_report(rows: list[dict[str, Any]], models: dict[str, Any], store: sqlite3.Connection) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    add = lines.append
    add(f"# Improvement event quality report\n\nGenerated {now_utc()} by `scripts/10_analyze_event_quality.py` over {len(rows)} events "
        f"(stage 08) for the 39902 repos found by `.kicad_pro`. Flags per event are in `data/cache/event_quality/state.sqlite`, table `event_quality`.\n")
    add("## Filters\n")
    add("| filter | rule |\n|---|---|")
    add("| scope | event inside a qualified project dir; PR events must be merged; at least one `.kicad_pcb` changed |")
    add(f"| text | title + body with checklists, HTML comments, quotes, sign-offs removed is >= {MIN_BODY_CHARS} chars; not a PR template (checklist markers); < {int(MAX_TODO_LINE_RATIO*100)}% TODO lines; not just issue numbers / URLs |")
    add(f"| size | added + deleted lines over the event's KiCad files <= {MAX_KICAD_LINES}; total changed files <= {MAX_CHANGED_FILES} |")
    add("| modified only | every KiCad file has status `modified`, so before and after states pair up |")
    add(f"| semantics | from the patch text, changed lines whose leading token is not save-time churn (`uuid`, `tstamp`, `version`, `generator`, ...) >= {MIN_SEMANTIC_LINES}; this is a proxy for a netlist comparison, which needs the full files |")
    add(f"| quota | at most {REPO_QUOTA} events per repo, best tier then longest text first |")
    add("\nTiers: **A** all filters pass and an issue is linked; **B** all filters pass; **C** text / size / modified pass but GitHub omitted the patch (large file), semantics unknown; **X** excluded.\n")

    for kind, label in (("pull_request", "PR events"), ("commit", "commit events")):
        add(f"## Funnel: {label}\n")
        add("| step | events | repos |\n|---|---|---|")
        for step, n, repos in funnel(rows, kind):
            add(f"| {step} | {n} | {repos} |")
        add("")

    add("## Tiers\n")
    add("| tier | PR events | commit events | repos |\n|---|---|---|---|")
    for tier in ("A", "B", "C", "X"):
        sub = [r for r in rows if r["tier"] == tier]
        add(f"| {tier} | {sum(1 for r in sub if r['kind']=='pull_request')} | {sum(1 for r in sub if r['kind']=='commit')} | {len({r['repo_id'] for r in sub})} |")
    pool = [r for r in rows if r["tier"] in ("A", "B") and r.get("in_quota")]
    add(f"\nFinal pool (tiers A + B within quota): **{len(pool)}** events from **{len({r['repo_id'] for r in pool})}** repos "
        f"({sum(1 for r in pool if r['kind']=='pull_request')} PR, {sum(1 for r in pool if r['kind']=='commit')} commit); "
        f"quota removed {sum(1 for r in rows if r['tier'] in ('A','B') and not r.get('in_quota'))} events.\n")

    add("## Why events are excluded\n")
    add("Counted over eligible events (qualified dir, merged or commit, pcb changed); an event can fail several filters.\n")
    add("| reason | PR events | commit events |\n|---|---|---|")
    eligible = [r for r in rows if r["in_qualified"] and r["pcb_changed"] and (r["kind"] == "commit" or r["merged"])]
    for reason in ("text", "size", "not_modified_only", "save_only_churn"):
        add(f"| {reason} | {sum(1 for r in eligible if r['kind']=='pull_request' and reason in r['reasons'])} | {sum(1 for r in eligible if r['kind']=='commit' and reason in r['reasons'])} |")
    add("")
    add("Text failures broken down:\n")
    add("| sub-reason | PR events | commit events |\n|---|---|---|")
    for label, pred in (("cleaned body shorter than threshold", lambda r: r["body_clean_chars"] < MIN_BODY_CHARS),
                        ("PR template", lambda r: r["is_template"]),
                        ("TODO-dominated", lambda r: r["todo_line_ratio"] >= MAX_TODO_LINE_RATIO),
                        ("issue numbers / URLs only", lambda r: r["is_refs_only"])):
        add(f"| {label} | {sum(1 for r in eligible if r['kind']=='pull_request' and pred(r))} | {sum(1 for r in eligible if r['kind']=='commit' and pred(r))} |")
    add("")

    add("## Save-only churn\n")
    with_patch = [r for r in rows if r["patch_available"]]
    add(f"{len(with_patch)} events had patch text for every KiCad file. Semantic vs churn changed lines:\n")
    add("| | events | median semantic lines | median churn lines | churn-only events |\n|---|---|---|---|---|")
    for kind in ("pull_request", "commit"):
        sub = [r for r in with_patch if r["kind"] == kind]
        if not sub:
            continue
        sem = sorted(r["semantic_lines"] for r in sub); ch = sorted(r["churn_lines"] for r in sub)
        add(f"| {kind} | {len(sub)} | {sem[len(sem)//2]} | {ch[len(ch)//2]} | {sum(1 for r in sub if not r['semantic_ok'])} |")
    add("")

    add("## Repo concentration\n")
    add("Top repos by tier A + B events before the quota:\n")
    add("| repo | A + B events | kept by quota |\n|---|---|---|")
    counts = Counter(r["repo_full_name"] for r in rows if r["tier"] in ("A", "B"))
    kept = Counter(r["repo_full_name"] for r in pool)
    for name, n in counts.most_common(12):
        add(f"| {name} | {n} | {kept[name]} |")
    add("")

    add("## 3D models in qualified repos\n")
    add(f"Of {models['qualified_repos']} qualified repos, {models['repos_with_3d_models']} ship at least one 3D model file "
        f"({', '.join(MODEL_3D_SUFFIXES)}); {models['repos_with_3d_models_in_project_dir']} have one inside a project dir. Files by suffix: "
        + ", ".join(f"{k} {v}" for k, v in models["suffix_totals"].items()) + ". Table `repo_3d_models`.\n")

    add("## Samples\n")
    for tier in ("A", "B"):
        add(f"### Tier {tier}\n")
        for r in store.execute("select e.*, q.semantic_lines, q.churn_lines, q.body_clean_chars from event_quality q join events.improvement_events e on e.id = q.event_id where q.tier = ? and q.in_quota = 1 order by random() limit 4", (tier,)):
            ref = f"PR #{r['number']}" if r["kind"] == "pull_request" else f"commit {r['sha'][:10]}"
            add(f"- **{r['repo_full_name']}** {ref}, dir `{r['project_dir'] or '/'}`, pcb {r['kicad_pcb_count']} sch {r['kicad_sch_count']} pro {r['kicad_pro_count']}, "
                f"semantic {r['semantic_lines']} churn {r['churn_lines']} lines, text {r['body_clean_chars']} chars, issues {r['linked_issues_json']}. "
                f"*{(r['title'] or '').strip()[:100]}* [link]({r['html_url']})")
        add("")

    add("## Next steps\n")
    add("- Netlist comparison: parse `.kicad_sch` / `.kicad_pcb` S-expressions at before and after sha (needs the full files, i.e. a checkout) and compare nets, components, footprints; the churn heuristic above only reads patches.")
    add("- DRC / ERC with `kicad-cli` (found at /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli) on both states; keep events whose error counts do not grow.")
    add(f"- Manual review of the tier A seed set: `{SEED_CSV_PATH}`.")
    add("- Rerun after the 29279 added repos are fetched (Release C).")
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    import csv
    with SEED_CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["repo_full_name", "kind", "number_or_sha", "project_dir", "title", "linked_issues", "kicad_pcb", "kicad_sch", "kicad_pro", "kicad_lines", "semantic_lines", "body_clean_chars", "html_url"])
        for r in store.execute("select e.*, q.kicad_lines, q.semantic_lines, q.body_clean_chars from event_quality q join events.improvement_events e on e.id = q.event_id where q.tier = 'A' and q.in_quota = 1 order by e.repo_full_name, e.number, e.sha"):
            writer.writerow([r["repo_full_name"], r["kind"], r["number"] or r["sha"], r["project_dir"], (r["title"] or "").strip(), r["linked_issues_json"], r["kicad_pcb_count"], r["kicad_sch_count"], r["kicad_pro_count"], r["kicad_lines"], r["semantic_lines"], r["body_clean_chars"], r["html_url"]])


def analyze_event_quality() -> dict[str, Any]:
    with closing(setup_db()) as store:
        models = analyze_3d_models(store)
        print(f"3d models: {models}", flush=True)
        result = analyze_events(store)
        store.execute("attach database ? as events", (str(EVENTS_DB_PATH),))
        write_report(result["rows"], models, store)
        with store:
            store.execute("insert or replace into meta (key, value) values ('last_analyzed_at', ?)", (now_utc(),))
        tiers = Counter(r["tier"] for r in result["rows"])
        pool = sum(1 for r in result["rows"] if r["tier"] in ("A", "B") and r.get("in_quota"))
        return {"db_path": str(DB_PATH), "report": str(REPORT_PATH), "seed_csv": str(SEED_CSV_PATH), "events": len(result["rows"]), "tiers": dict(tiers), "final_pool": pool, "models_3d": models}


def main() -> None:
    print(json.dumps(analyze_event_quality(), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
