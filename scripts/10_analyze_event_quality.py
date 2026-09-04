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
            # Template bodies are not rejected outright: checklist and comment lines are stripped by
            # clean_body and the remaining text has to clear the length bar like any other body.
            text_ok = len(cleaned) >= MIN_BODY_CHARS and todo_ratio < MAX_TODO_LINE_RATIO and not refs_only
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
        ("全部事件", lambda r: True),
        ("落在合格工程目录内", lambda r: r["in_qualified"]),
        ("PR 已合并（commit 一律通过）", lambda r: r["kind"] == "commit" or r["merged"]),
        ("改动了 .kicad_pcb", lambda r: r["pcb_changed"]),
        (f"文本：删模板行后正文 >= {MIN_BODY_CHARS} 字，非 TODO / 纯引用", lambda r: r["text_ok"]),
        (f"规模：KiCad 改动行 <= {MAX_KICAD_LINES}，改动文件 <= {MAX_CHANGED_FILES}", lambda r: r["size_ok"]),
        ("KiCad 文件全为 modified（无 added / renamed / removed）", lambda r: r["modified_only"]),
        ("patch 文本可用", lambda r: r["patch_available"]),
        (f"语义改动行 >= {MIN_SEMANTIC_LINES}（非只存盘 churn）", lambda r: r["semantic_ok"]),
        (f"仓库限额 {REPO_QUOTA} 以内", lambda r: r.get("in_quota") and r["tier"] in ("A", "B")),
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
    add(f"# 改进事件质量报告\n\n生成时间 {now_utc()}，由 `scripts/10_analyze_event_quality.py` 对阶段 08 的 {len(rows)} 个事件计算，"
        f"覆盖 `.kicad_pro` 搜到的 39902 个仓库。每个事件的标志在 `data/cache/event_quality/state.sqlite` 的 `event_quality` 表。\n")
    add("## 过滤规则\n")
    add("| 过滤 | 规则 |\n|---|---|")
    add("| 范围 | 事件落在合格工程目录内；PR 事件必须已合并；至少改动一个 `.kicad_pcb` |")
    add(f"| 文本 | 标题 + 正文去掉 checklist、HTML 注释、引用、签名后 >= {MIN_BODY_CHARS} 字（PR 模板行删掉后按剩余文字计）；TODO 行 < {int(MAX_TODO_LINE_RATIO*100)}%；不是纯 issue 编号 / URL |")
    add(f"| 规模 | 事件内 KiCad 文件的增删行合计 <= {MAX_KICAD_LINES}；改动文件总数 <= {MAX_CHANGED_FILES} |")
    add("| 只改已有文件 | 每个 KiCad 文件状态都是 `modified`，保证前后状态一一对应 |")
    add(f"| 语义 | 从 patch 文本看，行首 token 不属于存盘 churn（`uuid`、`tstamp`、`version`、`generator` 等）的改动行 >= {MIN_SEMANTIC_LINES}；这是网表比较的代理，完整比较需要整个文件 |")
    add(f"| 限额 | 每个仓库最多 {REPO_QUOTA} 个事件，先按等级再按文本长度 |")
    add("\n等级：**A** 全部通过且关联 issue；**B** 全部通过；**C** 文本 / 规模 / modified 通过但 GitHub 省略了 patch（文件过大），语义未知；**X** 排除。\n")

    for kind, label in (("pull_request", "PR 事件"), ("commit", "commit 事件")):
        add(f"## 漏斗：{label}\n")
        add("| 步骤 | 事件 | 仓库 |\n|---|---|---|")
        for step, n, repos in funnel(rows, kind):
            add(f"| {step} | {n} | {repos} |")
        add("")

    add("## 分层\n")
    add("| 等级 | PR 事件 | commit 事件 | 仓库 |\n|---|---|---|---|")
    for tier in ("A", "B", "C", "X"):
        sub = [r for r in rows if r["tier"] == tier]
        add(f"| {tier} | {sum(1 for r in sub if r['kind']=='pull_request')} | {sum(1 for r in sub if r['kind']=='commit')} | {len({r['repo_id'] for r in sub})} |")
    pool = [r for r in rows if r["tier"] in ("A", "B") and r.get("in_quota")]
    add(f"\n最终池（A + B 且在限额内）：**{len(pool)}** 个事件，来自 **{len({r['repo_id'] for r in pool})}** 个仓库"
        f"（PR {sum(1 for r in pool if r['kind']=='pull_request')}，commit {sum(1 for r in pool if r['kind']=='commit')}）；限额去掉 {sum(1 for r in rows if r['tier'] in ('A','B') and not r.get('in_quota'))} 个。\n")

    add("## 排除原因\n")
    add("在范围内事件（合格目录、已合并或 commit、改了 pcb）上统计；一个事件可能同时不过多个过滤。\n")
    add("| 原因 | PR 事件 | commit 事件 |\n|---|---|---|")
    eligible = [r for r in rows if r["in_qualified"] and r["pcb_changed"] and (r["kind"] == "commit" or r["merged"])]
    for reason, label in (("text", "文本"), ("size", "规模"), ("not_modified_only", "含 added / renamed / removed"), ("save_only_churn", "只存盘 churn")):
        add(f"| {label} | {sum(1 for r in eligible if r['kind']=='pull_request' and reason in r['reasons'])} | {sum(1 for r in eligible if r['kind']=='commit' and reason in r['reasons'])} |")
    add("")
    add("文本不合格的细分：\n")
    add("| 细分 | PR 事件 | commit 事件 |\n|---|---|---|")
    for label, pred in (("清洗后正文短于阈值", lambda r: r["body_clean_chars"] < MIN_BODY_CHARS),
                        ("带 PR 模板且删模板行后仍太短", lambda r: r["is_template"] and r["body_clean_chars"] < MIN_BODY_CHARS),
                        ("TODO 为主", lambda r: r["todo_line_ratio"] >= MAX_TODO_LINE_RATIO),
                        ("只有 issue 编号 / URL", lambda r: r["is_refs_only"])):
        add(f"| {label} | {sum(1 for r in eligible if r['kind']=='pull_request' and pred(r))} | {sum(1 for r in eligible if r['kind']=='commit' and pred(r))} |")
    add("")

    add("## 只存盘 churn\n")
    with_patch = [r for r in rows if r["patch_available"]]
    add(f"{len(with_patch)} 个事件的每个 KiCad 文件都有 patch 文本。语义行与 churn 行：\n")
    add("| | 事件 | 语义行中位数 | churn 行中位数 | 只有 churn 的事件 |\n|---|---|---|---|---|")
    for kind in ("pull_request", "commit"):
        sub = [r for r in with_patch if r["kind"] == kind]
        if not sub:
            continue
        sem = sorted(r["semantic_lines"] for r in sub); ch = sorted(r["churn_lines"] for r in sub)
        add(f"| {kind} | {len(sub)} | {sem[len(sem)//2]} | {ch[len(ch)//2]} | {sum(1 for r in sub if not r['semantic_ok'])} |")
    add("")

    add("## 仓库集中度\n")
    add("限额前 A + B 事件最多的仓库：\n")
    add("| 仓库 | A + B 事件 | 限额后保留 |\n|---|---|---|")
    counts = Counter(r["repo_full_name"] for r in rows if r["tier"] in ("A", "B"))
    kept = Counter(r["repo_full_name"] for r in pool)
    for name, n in counts.most_common(12):
        add(f"| {name} | {n} | {kept[name]} |")
    add("")

    add("## 合格仓库中的 3D 模型\n")
    add(f"{models['qualified_repos']} 个合格仓库中，{models['repos_with_3d_models']} 个至少带一个 3D 模型文件"
        f"（{', '.join(MODEL_3D_SUFFIXES)}）；{models['repos_with_3d_models_in_project_dir']} 个在工程目录内。按后缀的文件数："
        + "，".join(f"{k} {v}" for k, v in models["suffix_totals"].items()) + "。见表 `repo_3d_models`。\n")

    add("## 样本\n")
    for tier in ("A", "B"):
        add(f"### {tier} 级\n")
        for r in store.execute("select e.*, q.semantic_lines, q.churn_lines, q.body_clean_chars from event_quality q join events.improvement_events e on e.id = q.event_id where q.tier = ? and q.in_quota = 1 order by random() limit 4", (tier,)):
            ref = f"PR #{r['number']}" if r["kind"] == "pull_request" else f"commit {r['sha'][:10]}"
            add(f"- **{r['repo_full_name']}** {ref}，目录 `{r['project_dir'] or '/'}`，pcb {r['kicad_pcb_count']} sch {r['kicad_sch_count']} pro {r['kicad_pro_count']}，"
                f"语义 {r['semantic_lines']} 行 churn {r['churn_lines']} 行，文本 {r['body_clean_chars']} 字，issue {r['linked_issues_json']}。"
                f"*{(r['title'] or '').strip()[:100]}* [链接]({r['html_url']})")
        add("")

    add("## 下一步\n")
    add("- 网表比较：在改动前后 sha 上解析 `.kicad_sch` / `.kicad_pcb` 的 S 表达式（需要完整文件，即 checkout），比较网络、元件、封装；上面的 churn 启发式只读 patch。")
    add("- 用 `kicad-cli`（本机在 /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli）对前后状态跑 DRC / ERC，保留错误数不增加的事件。")
    add(f"- 人工审阅 A 级种子集：`{SEED_CSV_PATH}`。")
    add("- 新增的 29279 个仓库拉完后重跑（Release C）。")
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
