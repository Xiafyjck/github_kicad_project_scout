from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Stage 02, merge candidates: union every search-strategy cache, de-duplicate by repo_id. Local only, no network.
SOURCE_DB_GLOB = "data/cache/github_code_search_*/state.sqlite"
CACHE_DIR = Path("data/cache/github_candidates")
DB_PATH = CACHE_DIR / "state.sqlite"
SCHEMA_VERSION = 1


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def source_db_paths() -> list[Path]:
    paths = sorted(Path(".").glob(SOURCE_DB_GLOB))
    if not paths:
        raise RuntimeError(f"no source DB found: {SOURCE_DB_GLOB}")
    return paths


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

            -- candidate_repos: unified repo list; downstream does not care which strategy found a repo.
            -- sources_json lists every search cache that hit the repo; matched_files_json keeps the hits.
            create table if not exists candidate_repos (
              repo_id integer primary key,
              repo_full_name text not null,
              repo_url text not null,
              fork integer,
              sources_json text not null,
              matched_files_json text not null,
              merged_at text not null
            );
            """
        )
        db.executemany(
            "insert or replace into meta (key, value) values (?, ?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("source_db_glob", SOURCE_DB_GLOB),
            ],
        )
    return db


def iter_hits(source_db: Path):
    with closing(sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=30)) as source:
        source.row_factory = sqlite3.Row
        for page_row in source.execute("select query, page, response_json from pages"):
            items = json.loads(page_row["response_json"])["items"]
            if not isinstance(items, list):
                raise TypeError(f"{source_db}: pages.items is not a list")
            for item in items:
                if not isinstance(item, dict):
                    raise TypeError(f"{source_db}: page item is not a dict")
                repo_info = item["repository"]
                if not isinstance(repo_info, dict):
                    raise TypeError(f"{source_db}: item.repository is not a dict")
                yield {
                    "repo_id": int(repo_info["id"]),
                    "repo_full_name": str(repo_info["full_name"]),
                    "repo_url": str(repo_info["html_url"]),
                    "fork": repo_info.get("fork"),
                    "file_hit": {
                        "source_db": str(source_db),
                        "path": item.get("path", ""),
                        "html_url": item.get("html_url", ""),
                        "sha": item.get("sha", ""),
                        "query": page_row["query"],
                        "page": page_row["page"],
                    },
                }


def merge_candidates() -> dict[str, Any]:
    merged_at = now_utc()
    sources = source_db_paths()
    candidates: dict[int, dict[str, Any]] = {}
    hit_counts: dict[str, int] = {}

    for source_db in sources:
        for hit in iter_hits(source_db):
            hit_counts[str(source_db)] = hit_counts.get(str(source_db), 0) + 1
            candidate = candidates.setdefault(
                hit["repo_id"],
                {
                    "repo_id": hit["repo_id"],
                    "repo_full_name": hit["repo_full_name"],
                    "repo_url": hit["repo_url"],
                    "fork": hit["fork"],
                    "sources": set(),
                    "matched_files": {},
                },
            )
            if candidate["fork"] is None and hit["fork"] is not None:
                candidate["fork"] = hit["fork"]
            candidate["sources"].add(str(source_db))
            file_hit = hit["file_hit"]
            key = canonical_json({k: file_hit.get(k) for k in ("source_db", "path", "sha", "query", "page")})
            candidate["matched_files"][key] = file_hit

    rows = []
    for candidate in candidates.values():
        matched_files = sorted(
            candidate["matched_files"].values(),
            key=lambda hit: (hit["source_db"], hit["query"], hit["page"], hit["path"], hit["sha"]),
        )
        rows.append(
            (
                candidate["repo_id"],
                candidate["repo_full_name"],
                candidate["repo_url"],
                None if candidate["fork"] is None else int(bool(candidate["fork"])),
                json.dumps(sorted(candidate["sources"]), ensure_ascii=False),
                json.dumps(matched_files, ensure_ascii=False),
                merged_at,
            )
        )

    with closing(setup_db()) as db:
        with db:
            db.executemany(
                """
                insert into candidate_repos (
                  repo_id, repo_full_name, repo_url, fork, sources_json, matched_files_json, merged_at
                )
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict(repo_id) do update set
                  repo_full_name = excluded.repo_full_name,
                  repo_url = excluded.repo_url,
                  fork = coalesce(excluded.fork, candidate_repos.fork),
                  sources_json = excluded.sources_json,
                  matched_files_json = excluded.matched_files_json,
                  merged_at = excluded.merged_at
                """,
                rows,
            )
            db.executemany(
                "insert or replace into meta (key, value) values (?, ?)",
                [
                    ("source_dbs_json", json.dumps([str(p) for p in sources], ensure_ascii=False)),
                    ("last_merged_at", merged_at),
                ],
            )
        candidate_count = db.execute("select count(*) from candidate_repos").fetchone()[0]

    return {
        "schema_version": SCHEMA_VERSION,
        "db_path": str(DB_PATH),
        "source_dbs": [str(p) for p in sources],
        "hit_counts": hit_counts,
        "merged_candidate_count": len(rows),
        "candidate_repo_count": candidate_count,
    }


def main() -> None:
    print(json.dumps(merge_candidates(), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
