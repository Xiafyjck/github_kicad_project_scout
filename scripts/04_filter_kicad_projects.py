from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Stage 04, local first pass: read candidate and tree DBs read-only, run offline, recompute everything each run. Rule changes cost no API quota.
CANDIDATES_DB_PATH = Path("data/cache/github_candidates/state.sqlite")
TREES_DB_PATH = Path("data/cache/github_trees/state.sqlite")
CACHE_DIR = Path("data/cache/filter_kicad_projects")
DB_PATH = CACHE_DIR / "state.sqlite"
SCHEMA_VERSION = 1

PROJECT_FILTER_RULE = {
    "project_dir": ".kicad_pro directory",
    "required_same_dir_suffixes": [".kicad_pro", ".kicad_pcb", ".kicad_sch"],
    "readme": "root README plus nearby project_dir/parent README files",
    "truncated": "skipped; repo marked as unfiltered",
    "forks": "keep and mark",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def readme_dir(path: str) -> str:
    if "/" not in path:
        return ""
    return path.rsplit("/", 1)[0]


def parent_dir(path: str) -> str | None:
    if path == "":
        return None
    if "/" not in path:
        return ""
    return path.rsplit("/", 1)[0]


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

            -- qualified_projects: one row per KiCad project directory that passes the first-pass rule.
            create table if not exists qualified_projects (
              id integer primary key autoincrement,
              repo_id integer not null,
              repo_full_name text not null,
              repo_url text not null,
              fork integer,
              project_dir text not null,
              pro_files_json text not null,
              pcb_files_json text not null,
              sch_files_json text not null,
              readme_files_json text not null,
              filtered_at text not null,
              unique (repo_id, project_dir)
            );

            -- qualified_repos: repo-level roll-up of qualified_projects.
            create table if not exists qualified_repos (
              repo_id integer primary key,
              repo_full_name text not null,
              repo_url text not null,
              fork integer,
              project_count integer not null,
              project_dirs_json text not null,
              readme_files_json text not null,
              tree_sha text,
              tree_entry_count integer not null,
              filtered_at text not null
            );

            -- repo_filter_status: every candidate repo and why it did or did not get filtered.
            create table if not exists repo_filter_status (
              repo_id integer primary key,
              repo_full_name text not null,
              status text not null,
              project_count integer not null,
              message text not null,
              filtered_at text not null
            );

            create index if not exists idx_qualified_projects_repo
              on qualified_projects(repo_id);
            """
        )
        db.executemany(
            "insert or replace into meta (key, value) values (?, ?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("candidates_db", str(CANDIDATES_DB_PATH)),
                ("trees_db", str(TREES_DB_PATH)),
                ("project_filter_rule_json", json.dumps(PROJECT_FILTER_RULE, ensure_ascii=False)),
            ],
        )
    return db


def find_projects(tree: list[Any]) -> list[dict[str, Any]]:
    pro_by_dir: dict[str, list[str]] = {}
    pcb_by_dir: dict[str, list[str]] = {}
    sch_by_dir: dict[str, list[str]] = {}
    readmes_by_dir: dict[str, list[str]] = {}

    for entry in tree:
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        lower = path.lower()
        directory = readme_dir(path)
        basename = path.rsplit("/", 1)[-1].lower()

        if lower.endswith(".kicad_pro"):
            pro_by_dir.setdefault(directory, []).append(path)
        elif lower.endswith(".kicad_pcb"):
            pcb_by_dir.setdefault(directory, []).append(path)
        elif lower.endswith(".kicad_sch"):
            sch_by_dir.setdefault(directory, []).append(path)

        if basename.startswith("readme"):
            readmes_by_dir.setdefault(directory, []).append(path)

    root_readmes = sorted(readmes_by_dir.get("", []))
    projects = []
    for project_dir, pro_files in sorted(pro_by_dir.items()):
        pcb_files = sorted(pcb_by_dir.get(project_dir, []))
        sch_files = sorted(sch_by_dir.get(project_dir, []))
        if not pcb_files or not sch_files:
            continue

        readme_files = list(root_readmes)
        readme_files.extend(sorted(readmes_by_dir.get(project_dir, [])))
        parent = parent_dir(project_dir)
        if parent is not None:
            readme_files.extend(sorted(readmes_by_dir.get(parent, [])))
        readme_files = sorted(set(readme_files))
        if not readme_files:
            continue

        projects.append(
            {
                "project_dir": project_dir,
                "pro_files": sorted(pro_files),
                "pcb_files": pcb_files,
                "sch_files": sch_files,
                "readme_files": readme_files,
            }
        )
    return projects


def filter_kicad_projects() -> dict[str, Any]:
    filtered_at = now_utc()
    with closing(open_ro(CANDIDATES_DB_PATH)) as candidates_db:
        candidates = {
            int(row["repo_id"]): dict(row)
            for row in candidates_db.execute(
                "select repo_id, repo_full_name, repo_url, fork from candidate_repos"
            )
        }

    project_rows = []
    repo_rows = []
    status_rows = []
    status_counts: dict[str, int] = {}
    processed = 0

    with closing(open_ro(TREES_DB_PATH)) as trees_db:
        for row in trees_db.execute(
            """
            select s.repo_id, s.repo_full_name, s.status, t.tree_sha, t.truncated, t.tree_entry_count,
                   a.response_json
            from repo_status s
            left join repo_trees t on t.repo_id = s.repo_id
            left join api_cache a on a.id = t.api_cache_id and t.truncated = 0
            order by s.repo_id
            """
        ):
            repo_id = int(row["repo_id"])
            candidate = candidates.get(repo_id)
            if candidate is None:
                raise KeyError(f"repo {repo_id} in trees DB but missing from candidates DB")

            if row["status"] == "truncated":
                status, projects, message = "unfiltered_truncated", [], "recursive tree response was truncated"
            elif row["response_json"] is None:
                status, projects, message = "unfiltered_no_tree", [], f"tree status={row['status']}"
            else:
                tree = json.loads(row["response_json"])["tree"]
                if not isinstance(tree, list):
                    raise TypeError(f"repo {repo_id}: tree is not a list")
                projects = find_projects(tree)
                status = "qualified" if projects else "unqualified"
                message = f"qualified_projects={len(projects)}"

            status_counts[status] = status_counts.get(status, 0) + 1
            status_rows.append((repo_id, candidate["repo_full_name"], status, len(projects), message, filtered_at))

            for project in projects:
                project_rows.append(
                    (
                        repo_id,
                        candidate["repo_full_name"],
                        candidate["repo_url"],
                        candidate["fork"],
                        project["project_dir"],
                        json.dumps(project["pro_files"], ensure_ascii=False),
                        json.dumps(project["pcb_files"], ensure_ascii=False),
                        json.dumps(project["sch_files"], ensure_ascii=False),
                        json.dumps(project["readme_files"], ensure_ascii=False),
                        filtered_at,
                    )
                )
            if projects:
                readme_union = sorted({path for project in projects for path in project["readme_files"]})
                repo_rows.append(
                    (
                        repo_id,
                        candidate["repo_full_name"],
                        candidate["repo_url"],
                        candidate["fork"],
                        len(projects),
                        json.dumps([project["project_dir"] for project in projects], ensure_ascii=False),
                        json.dumps(readme_union, ensure_ascii=False),
                        row["tree_sha"],
                        int(row["tree_entry_count"]),
                        filtered_at,
                    )
                )

            processed += 1
            if processed % 5000 == 0:
                print(f"filtered {processed} repos", flush=True)

    with closing(setup_db()) as db:
        with db:
            db.execute("delete from qualified_projects")
            db.execute("delete from qualified_repos")
            db.execute("delete from repo_filter_status")
            db.executemany(
                """
                insert into qualified_projects (
                  repo_id, repo_full_name, repo_url, fork, project_dir,
                  pro_files_json, pcb_files_json, sch_files_json, readme_files_json, filtered_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                project_rows,
            )
            db.executemany(
                """
                insert into qualified_repos (
                  repo_id, repo_full_name, repo_url, fork, project_count, project_dirs_json,
                  readme_files_json, tree_sha, tree_entry_count, filtered_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                repo_rows,
            )
            db.executemany(
                """
                insert into repo_filter_status (
                  repo_id, repo_full_name, status, project_count, message, filtered_at
                )
                values (?, ?, ?, ?, ?, ?)
                """,
                status_rows,
            )
            db.execute("insert or replace into meta (key, value) values ('last_filtered_at', ?)", (filtered_at,))

    return {
        "schema_version": SCHEMA_VERSION,
        "db_path": str(DB_PATH),
        "candidates_db": str(CANDIDATES_DB_PATH),
        "trees_db": str(TREES_DB_PATH),
        "filtered_at": filtered_at,
        "processed_repo_count": processed,
        "repo_filter_status_counts": status_counts,
        "qualified_repo_count": len(repo_rows),
        "qualified_project_count": len(project_rows),
    }


def main() -> None:
    print(json.dumps(filter_kicad_projects(), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
