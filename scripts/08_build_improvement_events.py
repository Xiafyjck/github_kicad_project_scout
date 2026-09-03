from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Stage 08, local post-processing: turn the raw stage 07 listings into commit / PR / issue tables and an
# "improvement event" table that links each PCB change to a project dir, its before/after states, and
# the text explaining it. Reads upstream DBs read-only, recomputes everything each run, no network.
CANDIDATES_DB_PATH = Path("data/cache/github_candidates/state.sqlite")
FILTER_DB_PATH = Path("data/cache/filter_kicad_projects/state.sqlite")
HISTORY_DB_PATH = Path("data/cache/github_repo_history/state.sqlite")
COMMIT_FILES_DB_PATH = Path("data/cache/github_commit_files/state.sqlite")  # stage 09, optional
CACHE_DIR = Path("data/cache/improvement_events")
DB_PATH = CACHE_DIR / "state.sqlite"
SCHEMA_VERSION = 1

KICAD_SUFFIXES = (".kicad_pcb", ".kicad_sch", ".kicad_pro")
ISSUE_REF_RE = re.compile(r"(?<![\w/])#(\d+)\b")
CLOSING_REF_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b", re.IGNORECASE)
EVENT_RULE = {
    "pull_request": "PR whose changed files include a .kicad_pcb/.kicad_sch/.kicad_pro file; one event per (PR, project dir); before = base sha, after = merge commit sha when GitHub reports one, else head sha",
    "commit": "commit listed by commits?path=<qualified project dir>; before = first parent, after = the commit; changed files from stage 09 when fetched (files_known = 1), else unknown (0)",
    "project_dir": "deepest qualified project dir containing the file (in_qualified_project = 1), else the file's own directory (0)",
    "linked_issues": "#N references in title/body that resolve to a non-PR issue of the same repo; closing keywords tracked separately",
}


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

            -- commits: every commit seen in any stage 07 commit listing of the repo (de-duplicated by sha).
            create table if not exists commits (
              repo_id integer not null,
              sha text not null,
              parent_shas_json text not null,
              tree_sha text,
              author_login text,
              author_name text,
              author_date text,
              committer_date text,
              message text,
              html_url text,
              primary key (repo_id, sha)
            );

            -- commit_touches: which qualified project dir listing each commit came from ('' = whole repo).
            create table if not exists commit_touches (
              repo_id integer not null,
              sha text not null,
              project_dir text not null,
              primary key (repo_id, sha, project_dir)
            );

            create table if not exists pull_requests (
              repo_id integer not null,
              number integer not null,
              title text,
              body text,
              state text,
              draft integer,
              merged integer not null,
              created_at text,
              merged_at text,
              closed_at text,
              user_login text,
              base_sha text,
              head_sha text,
              merge_commit_sha text,
              labels_json text not null,
              html_url text,
              files_fetched integer not null,
              changed_file_count integer not null,
              kicad_file_count integer not null,
              primary key (repo_id, number)
            );

            -- commit_files: one row per changed file of a stage 09 fetched commit; patch text stays in the
            -- stage 09 api_cache row referenced by api_cache_id.
            create table if not exists commit_files (
              repo_id integer not null,
              sha text not null,
              filename text not null,
              status text,
              additions integer,
              deletions integer,
              changes integer,
              blob_sha text,
              previous_filename text,
              patch_length integer not null,
              api_cache_id integer not null,
              primary key (repo_id, sha, filename)
            );

            -- pull_request_files: one row per changed file; the patch text stays in the stage 07 api_cache
            -- row referenced by api_cache_id.
            create table if not exists pull_request_files (
              repo_id integer not null,
              number integer not null,
              filename text not null,
              status text,
              additions integer,
              deletions integer,
              changes integer,
              blob_sha text,
              previous_filename text,
              patch_length integer not null,
              api_cache_id integer not null,
              primary key (repo_id, number, filename)
            );

            create table if not exists issues (
              repo_id integer not null,
              number integer not null,
              is_pull_request integer not null,
              title text,
              body text,
              state text,
              created_at text,
              closed_at text,
              user_login text,
              labels_json text not null,
              comment_count integer,
              html_url text,
              primary key (repo_id, number)
            );

            -- improvement_events: one row per PCB change with an explanation attached.
            create table if not exists improvement_events (
              id integer primary key autoincrement,
              repo_id integer not null,
              repo_full_name text not null,
              project_dir text not null,
              in_qualified_project integer not null,
              kind text not null,
              number integer,
              sha text,
              title text,
              body text,
              author_login text,
              created_at text,
              merged_at text,
              merged integer not null,
              before_sha text,
              after_sha text,
              before_tree_sha text,
              after_tree_sha text,
              files_known integer not null,
              changed_file_count integer,
              kicad_pcb_count integer,
              kicad_sch_count integer,
              kicad_pro_count integer,
              kicad_files_json text,
              linked_issues_json text not null,
              closing_issues_json text not null,
              html_url text,
              unique (repo_id, kind, number, sha, project_dir)
            );

            create index if not exists idx_events_repo on improvement_events(repo_id, project_dir);
            create index if not exists idx_events_kind on improvement_events(kind, merged, in_qualified_project);
            create index if not exists idx_prf_repo on pull_request_files(repo_id, number);
            create index if not exists idx_cf_repo on commit_files(repo_id, sha);
            """
        )
        db.executemany(
            "insert or replace into meta (key, value) values (?, ?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("candidates_db", str(CANDIDATES_DB_PATH)),
                ("filter_db", str(FILTER_DB_PATH)),
                ("history_db", str(HISTORY_DB_PATH)),
                ("commit_files_db", str(COMMIT_FILES_DB_PATH)),
                ("event_rule_json", json.dumps(EVENT_RULE, ensure_ascii=False)),
                ("kicad_suffixes_json", json.dumps(list(KICAD_SUFFIXES))),
            ],
        )
    return db


def file_dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def project_dir_for(path: str, dirs_by_depth: list[str]) -> tuple[str, int]:
    for project_dir in dirs_by_depth:
        if project_dir == "" or path.startswith(project_dir + "/"):
            return project_dir, 1
    return file_dir(path), 0


def kicad_suffix(path: str) -> str | None:
    lower = path.lower()
    for suffix in KICAD_SUFFIXES:
        if lower.endswith(suffix):
            return suffix
    return None


def issue_refs(text: str) -> tuple[list[int], list[int]]:
    all_refs = sorted({int(m) for m in ISSUE_REF_RE.findall(text)})
    closing = sorted({int(m) for m in CLOSING_REF_RE.findall(text)})
    return all_refs, closing


def load_repo_pages(history: sqlite3.Connection, repo_id: int) -> dict[str, dict[str, list[tuple[int, int, Any]]]]:
    # {kind: {subject: [(page, api_cache_id, items)]}}; only 200 pages carry items.
    pages: dict[str, dict[str, list[tuple[int, int, Any]]]] = {}
    for row in history.execute(
        """
        select l.kind, l.subject, l.page, l.api_cache_id, l.status_code, a.response_json
        from listing_pages l join api_cache a on a.id = l.api_cache_id
        where l.repo_id = ?
        order by l.kind, l.subject, l.page
        """,
        (repo_id,),
    ):
        if int(row["status_code"]) != 200:
            continue
        items = json.loads(row["response_json"])
        pages.setdefault(row["kind"], {}).setdefault(row["subject"], []).append((int(row["page"]), int(row["api_cache_id"]), items))
    return pages


def load_commit_files(commit_files_db: sqlite3.Connection | None, repo_id: int) -> dict[str, list[tuple[int, dict[str, Any]]]]:
    # {sha: [(api_cache_id, file)]} for every commit stage 09 fetched with HTTP 200 for this repo.
    files: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    if commit_files_db is None:
        return files
    for row in commit_files_db.execute(
        """
        select p.sha, p.page, p.api_cache_id, a.response_json
        from commit_pages p join api_cache a on a.id = p.api_cache_id
        where p.repo_id = ? and p.status_code = 200
        order by p.sha, p.page
        """,
        (repo_id,),
    ):
        data = json.loads(row["response_json"])
        files.setdefault(row["sha"], []).extend((int(row["api_cache_id"]), f) for f in data.get("files") or [])
    return files


def build_repo(
    repo: dict[str, Any],
    project_dirs: list[str],
    pages: dict[str, dict[str, list[tuple[int, int, Any]]]],
    commit_files: dict[str, list[tuple[int, dict[str, Any]]]],
) -> dict[str, list[tuple[Any, ...]]]:
    repo_id = repo["repo_id"]
    dirs_by_depth = sorted(project_dirs, key=lambda d: -len(d))
    out: dict[str, list[tuple[Any, ...]]] = {"commits": [], "commit_touches": [], "pull_requests": [], "pull_request_files": [], "commit_files": [], "issues": [], "events": []}

    commits: dict[str, dict[str, Any]] = {}
    for subject, page_list in pages.get("commits", {}).items():
        for _page, _cache_id, items in page_list:
            for item in items:
                sha = item["sha"]
                c = item.get("commit") or {}
                if sha not in commits:
                    commits[sha] = {
                        "parents": [p.get("sha") for p in item.get("parents") or []],
                        "tree_sha": (c.get("tree") or {}).get("sha"),
                        "author_login": (item.get("author") or {}).get("login"),
                        "author_name": (c.get("author") or {}).get("name"),
                        "author_date": (c.get("author") or {}).get("date"),
                        "committer_date": (c.get("committer") or {}).get("date"),
                        "message": c.get("message") or "",
                        "html_url": item.get("html_url"),
                    }
                out["commit_touches"].append((repo_id, sha, subject))
    for sha, c in commits.items():
        out["commits"].append((repo_id, sha, json.dumps(c["parents"]), c["tree_sha"], c["author_login"], c["author_name"], c["author_date"], c["committer_date"], c["message"], c["html_url"]))

    issues: dict[int, dict[str, Any]] = {}
    for _subject, page_list in pages.get("issues", {}).items():
        for _page, _cache_id, items in page_list:
            for item in items:
                number = int(item["number"])
                issues[number] = item
                out["issues"].append(
                    (
                        repo_id, number, int("pull_request" in item), item.get("title"), item.get("body"), item.get("state"),
                        item.get("created_at"), item.get("closed_at"), (item.get("user") or {}).get("login"),
                        json.dumps([label.get("name") for label in item.get("labels") or [] if isinstance(label, dict)], ensure_ascii=False),
                        item.get("comments"), item.get("html_url"),
                    )
                )
    plain_issue_numbers = {n for n, item in issues.items() if "pull_request" not in item}

    def resolve_refs(text: str) -> tuple[list[int], list[int]]:
        refs, closing = issue_refs(text)
        refs = [n for n in refs if n in plain_issue_numbers]
        closing = [n for n in closing if n in plain_issue_numbers]
        return refs, closing

    files_by_pr: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for subject, page_list in pages.get("pull_files", {}).items():
        number = int(subject)
        for _page, cache_id, items in page_list:
            files_by_pr.setdefault(number, []).extend((cache_id, item) for item in items)

    for _subject, page_list in pages.get("pulls", {}).items():
        for _page, _cache_id, items in page_list:
            for pr in items:
                number = int(pr["number"])
                files = files_by_pr.get(number)
                files_fetched = int(files is not None)
                files = files or []
                merged = int(bool(pr.get("merged_at")))
                kicad_files: dict[str, list[dict[str, Any]]] = {}
                kicad_total = 0
                for cache_id, f in files:
                    filename = f.get("filename") or ""
                    patch = f.get("patch") or ""
                    out["pull_request_files"].append(
                        (repo_id, number, filename, f.get("status"), f.get("additions"), f.get("deletions"), f.get("changes"), f.get("sha"), f.get("previous_filename"), len(patch), cache_id)
                    )
                    suffix = kicad_suffix(filename)
                    if suffix is None:
                        continue
                    kicad_total += 1
                    project_dir, _in_q = project_dir_for(filename, dirs_by_depth)
                    kicad_files.setdefault(project_dir, []).append({"path": filename, "status": f.get("status"), "suffix": suffix, "additions": f.get("additions"), "deletions": f.get("deletions"), "blob_sha": f.get("sha")})
                out["pull_requests"].append(
                    (
                        repo_id, number, pr.get("title"), pr.get("body"), pr.get("state"), int(bool(pr.get("draft"))), merged,
                        pr.get("created_at"), pr.get("merged_at"), pr.get("closed_at"), (pr.get("user") or {}).get("login"),
                        (pr.get("base") or {}).get("sha"), (pr.get("head") or {}).get("sha"), pr.get("merge_commit_sha"),
                        json.dumps([label.get("name") for label in pr.get("labels") or [] if isinstance(label, dict)], ensure_ascii=False),
                        pr.get("html_url"), files_fetched, len(files), kicad_total,
                    )
                )
                if not kicad_files:
                    continue
                text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
                refs, closing = resolve_refs(text)
                # merge_commit_sha is often null in list responses even for merged PRs; head sha is the
                # PR's own after-state relative to base, so fall back to it.
                after_sha = pr.get("merge_commit_sha") or (pr.get("head") or {}).get("sha")
                for project_dir, changed in kicad_files.items():
                    in_q = int(project_dir in project_dirs)
                    out["events"].append(
                        (
                            repo_id, repo["repo_full_name"], project_dir, in_q, "pull_request", number, None,
                            pr.get("title"), pr.get("body"), (pr.get("user") or {}).get("login"), pr.get("created_at"), pr.get("merged_at"), merged,
                            (pr.get("base") or {}).get("sha"), after_sha,
                            None, (commits.get(after_sha) or {}).get("tree_sha"),
                            1, len(files),
                            sum(1 for f in changed if f["suffix"] == ".kicad_pcb"),
                            sum(1 for f in changed if f["suffix"] == ".kicad_sch"),
                            sum(1 for f in changed if f["suffix"] == ".kicad_pro"),
                            json.dumps(changed, ensure_ascii=False), json.dumps(refs), json.dumps(closing), pr.get("html_url"),
                        )
                    )

    kicad_by_commit: dict[str, list[dict[str, Any]]] = {}
    file_count_by_commit: dict[str, int] = {}
    for sha, entries in commit_files.items():
        file_count_by_commit[sha] = len(entries)
        for cache_id, f in entries:
            filename = f.get("filename") or ""
            patch = f.get("patch") or ""
            out["commit_files"].append(
                (repo_id, sha, filename, f.get("status"), f.get("additions"), f.get("deletions"), f.get("changes"), f.get("sha"), f.get("previous_filename"), len(patch), cache_id)
            )
            suffix = kicad_suffix(filename)
            if suffix is not None:
                kicad_by_commit.setdefault(sha, []).append({"path": filename, "status": f.get("status"), "suffix": suffix, "additions": f.get("additions"), "deletions": f.get("deletions"), "blob_sha": f.get("sha")})

    seen_commit_events: set[tuple[str, str]] = set()
    for subject, page_list in pages.get("commits", {}).items():
        # subject '' is both the whole-repo listing (repos with no qualified dir) and a root-level project;
        # only the latter is in project_dirs.
        if subject not in project_dirs:
            continue
        for _page, _cache_id, items in page_list:
            for item in items:
                sha = item["sha"]
                if (sha, subject) in seen_commit_events:
                    continue
                seen_commit_events.add((sha, subject))
                c = commits[sha]
                message = c["message"]
                title, _, body = message.partition("\n")
                refs, closing = resolve_refs(message)
                parent = c["parents"][0] if c["parents"] else None
                files_known = int(sha in file_count_by_commit)
                changed = [f for f in kicad_by_commit.get(sha, []) if subject == "" or f["path"].startswith(subject + "/")]
                out["events"].append(
                    (
                        repo_id, repo["repo_full_name"], subject, 1, "commit", None, sha,
                        title.strip(), body.strip(), c["author_login"], c["author_date"], None, 0,
                        parent, sha,
                        (commits.get(parent) or {}).get("tree_sha") if parent else None, c["tree_sha"],
                        files_known,
                        file_count_by_commit.get(sha) if files_known else None,
                        sum(1 for f in changed if f["suffix"] == ".kicad_pcb") if files_known else None,
                        sum(1 for f in changed if f["suffix"] == ".kicad_sch") if files_known else None,
                        sum(1 for f in changed if f["suffix"] == ".kicad_pro") if files_known else None,
                        json.dumps(changed, ensure_ascii=False) if files_known else None,
                        json.dumps(refs), json.dumps(closing), c["html_url"],
                    )
                )
    return out


INSERTS = {
    "commits": "insert or replace into commits values (?,?,?,?,?,?,?,?,?,?)",
    "commit_touches": "insert or replace into commit_touches values (?,?,?)",
    "pull_requests": "insert or replace into pull_requests values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    "pull_request_files": "insert or replace into pull_request_files values (?,?,?,?,?,?,?,?,?,?,?)",
    "commit_files": "insert or replace into commit_files values (?,?,?,?,?,?,?,?,?,?,?)",
    "issues": "insert or replace into issues values (?,?,?,?,?,?,?,?,?,?,?,?)",
    "events": """insert or replace into improvement_events (
        repo_id, repo_full_name, project_dir, in_qualified_project, kind, number, sha,
        title, body, author_login, created_at, merged_at, merged,
        before_sha, after_sha, before_tree_sha, after_tree_sha,
        files_known, changed_file_count, kicad_pcb_count, kicad_sch_count, kicad_pro_count,
        kicad_files_json, linked_issues_json, closing_issues_json, html_url
    ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
}


def build_improvement_events() -> dict[str, Any]:
    built_at = now_utc()
    with closing(open_ro(CANDIDATES_DB_PATH)) as candidates_db:
        repos = [dict(r) for r in candidates_db.execute("select repo_id, repo_full_name from candidate_repos order by repo_id")]
    project_dirs: dict[int, list[str]] = {}
    with closing(open_ro(FILTER_DB_PATH)) as filter_db:
        for row in filter_db.execute("select repo_id, project_dir from qualified_projects"):
            project_dirs.setdefault(int(row["repo_id"]), []).append(str(row["project_dir"]))

    totals = {key: 0 for key in INSERTS}
    commit_files_db = open_ro(COMMIT_FILES_DB_PATH) if COMMIT_FILES_DB_PATH.exists() else None
    print(f"commit files db: {'present' if commit_files_db else 'absent'} ({COMMIT_FILES_DB_PATH})", flush=True)
    with closing(setup_db()) as db, closing(open_ro(HISTORY_DB_PATH)) as history:
        with db:
            for table in ("commits", "commit_touches", "pull_requests", "pull_request_files", "commit_files", "issues", "improvement_events"):
                db.execute(f"delete from {table}")
        for index, repo in enumerate(repos, start=1):
            pages = load_repo_pages(history, int(repo["repo_id"]))
            if not pages:
                continue
            commit_files = load_commit_files(commit_files_db, int(repo["repo_id"]))
            out = build_repo(repo, project_dirs.get(int(repo["repo_id"]), []), pages, commit_files)
            with db:
                for key, rows in out.items():
                    if rows:
                        db.executemany(INSERTS[key], rows)
                        totals[key] += len(rows)
            if index % 2000 == 0:
                print(f"processed {index}/{len(repos)} repos, events={totals['events']}", flush=True)

        with db:
            db.execute("insert or replace into meta (key, value) values ('last_built_at', ?)", (built_at,))
            summary_rows = {
                "event_kind_counts": {f"{r[0]}/merged={r[1]}/qualified={r[2]}": r[3] for r in db.execute("select kind, merged, in_qualified_project, count(*) from improvement_events group by 1,2,3")},
                "event_repo_count": db.execute("select count(distinct repo_id) from improvement_events").fetchone()[0],
                "pr_events_with_linked_issue": db.execute("select count(*) from improvement_events where kind='pull_request' and linked_issues_json != '[]'").fetchone()[0],
                "pr_events_with_pcb_change": db.execute("select count(*) from improvement_events where kind='pull_request' and kicad_pcb_count > 0").fetchone()[0],
                "commit_events_files_known": db.execute("select count(*) from improvement_events where kind='commit' and files_known = 1").fetchone()[0],
                "commit_events_with_pcb_change": db.execute("select count(*) from improvement_events where kind='commit' and kicad_pcb_count > 0").fetchone()[0],
            }
    if commit_files_db is not None:
        commit_files_db.close()
    return {"schema_version": SCHEMA_VERSION, "db_path": str(DB_PATH), "built_at": built_at, "repo_count": len(repos), "row_counts": totals, **summary_rows}


def main() -> None:
    print(json.dumps(build_improvement_events(), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
