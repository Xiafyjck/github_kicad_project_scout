from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Release contents: every candidate repo + each repo's full file tree + fetch time + first-pass qualified repo list. Local read-only, no network.
CANDIDATES_DB_PATH = Path("data/cache/github_candidates/state.sqlite")
TREES_DB_PATH = Path("data/cache/github_trees/state.sqlite")
FILTER_DB_PATH = Path("data/cache/filter_kicad_projects/state.sqlite")
RELEASES_DIR = Path("data/releases")
RELEASE_VERSION = "A"
ZIP_COMPRESSION = zipfile.ZIP_DEFLATED
ZIP_COMPRESS_LEVEL = 6
TREE_ENTRY_FIELDS = ("path", "mode", "type", "sha", "size")
REPOS_CSV_FIELDS = (
    "repo_id",
    "repo_full_name",
    "repo_url",
    "fork",
    "status",
    "http_status_code",
    "tree_sha",
    "truncated",
    "tree_entry_count",
    "tree_fetched_at",
    "matched_file_count",
    "sources",
)
QUALIFIED_REPOS_CSV_FIELDS = (
    "repo_id",
    "repo_full_name",
    "repo_url",
    "fork",
    "project_count",
    "project_dirs",
    "readme_files",
    "tree_sha",
    "tree_entry_count",
    "filtered_at",
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def open_ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_meta(db: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in db.execute("select key, value from meta")}


def write_repos_csv(candidates_db: sqlite3.Connection, trees_db: sqlite3.Connection, path: Path) -> dict[str, Any]:
    tree_rows = {
        int(row["repo_id"]): dict(row)
        for row in trees_db.execute(
            """
            select s.repo_id, s.status, s.http_status_code,
                   t.tree_sha, t.truncated, t.tree_entry_count, t.fetched_at as tree_fetched_at
            from repo_status s
            left join repo_trees t on t.repo_id = s.repo_id
            """
        )
    }
    status_counts: dict[str, int] = {}
    row_count = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPOS_CSV_FIELDS)
        writer.writeheader()
        for row in candidates_db.execute(
            """
            select repo_id, repo_full_name, repo_url, fork, sources_json, matched_files_json
            from candidate_repos
            order by repo_id
            """
        ):
            tree_row = tree_rows.get(int(row["repo_id"]), {})
            status = tree_row.get("status") or "pending"
            status_counts[status] = status_counts.get(status, 0) + 1
            writer.writerow(
                {
                    "repo_id": row["repo_id"],
                    "repo_full_name": row["repo_full_name"],
                    "repo_url": row["repo_url"],
                    "fork": "" if row["fork"] is None else int(row["fork"]),
                    "status": status,
                    "http_status_code": tree_row.get("http_status_code") or "",
                    "tree_sha": tree_row.get("tree_sha") or "",
                    "truncated": "" if tree_row.get("truncated") is None else int(tree_row["truncated"]),
                    "tree_entry_count": "" if tree_row.get("tree_entry_count") is None else tree_row["tree_entry_count"],
                    "tree_fetched_at": tree_row.get("tree_fetched_at") or "",
                    "matched_file_count": len(json.loads(row["matched_files_json"])),
                    "sources": row["sources_json"],
                }
            )
            row_count += 1
    return {"row_count": row_count, "status_counts": status_counts}


def write_qualified_repos_csv(filter_db: sqlite3.Connection, path: Path) -> dict[str, Any]:
    row_count = 0
    project_count = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=QUALIFIED_REPOS_CSV_FIELDS)
        writer.writeheader()
        for row in filter_db.execute(
            """
            select repo_id, repo_full_name, repo_url, fork, project_count, project_dirs_json,
                   readme_files_json, tree_sha, tree_entry_count, filtered_at
            from qualified_repos
            order by repo_id
            """
        ):
            writer.writerow(
                {
                    "repo_id": row["repo_id"],
                    "repo_full_name": row["repo_full_name"],
                    "repo_url": row["repo_url"],
                    "fork": "" if row["fork"] is None else int(row["fork"]),
                    "project_count": row["project_count"],
                    "project_dirs": row["project_dirs_json"],
                    "readme_files": row["readme_files_json"],
                    "tree_sha": row["tree_sha"] or "",
                    "tree_entry_count": row["tree_entry_count"],
                    "filtered_at": row["filtered_at"],
                }
            )
            row_count += 1
            project_count += int(row["project_count"])
    return {"repo_count": row_count, "project_count": project_count}


def write_trees_jsonl(candidates_db: sqlite3.Connection, trees_db: sqlite3.Connection, path: Path) -> dict[str, Any]:
    names = {
        int(row["repo_id"]): row["repo_full_name"]
        for row in candidates_db.execute("select repo_id, repo_full_name from candidate_repos")
    }
    repo_count = 0
    entry_count = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in trees_db.execute(
            """
            select t.repo_id, t.tree_sha, t.truncated, t.fetched_at, a.response_json
            from repo_trees t
            join api_cache a on a.id = t.api_cache_id
            order by t.repo_id
            """
        ):
            tree = json.loads(row["response_json"])["tree"]
            if not isinstance(tree, list):
                raise TypeError(f"repo {row['repo_id']}: tree is not a list")
            entries = [{field: entry.get(field) for field in TREE_ENTRY_FIELDS} for entry in tree]
            record = {
                "repo_id": row["repo_id"],
                "repo_full_name": names[int(row["repo_id"])],
                "tree_sha": row["tree_sha"],
                "truncated": bool(row["truncated"]),
                "tree_entry_count": len(entries),
                "fetched_at": row["fetched_at"],
                "tree": entries,
            }
            fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            fh.write("\n")
            repo_count += 1
            entry_count += len(entries)
            if repo_count % 5000 == 0:
                print(f"trees written {repo_count}", flush=True)
    return {"repo_count": repo_count, "entry_count": entry_count}


def write_release_zip(out_dir: Path, files: list[Path]) -> Path:
    # One archive next to the release dir; the loose files stay for local use and are gitignored.
    zip_path = out_dir.with_suffix(".zip")
    tmp_path = zip_path.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp_path, "w", compression=ZIP_COMPRESSION, compresslevel=ZIP_COMPRESS_LEVEL) as zf:
        for file_path in files:
            zf.write(file_path, arcname=f"{out_dir.name}/{file_path.name}")
    tmp_path.replace(zip_path)
    return zip_path


def release_github_trees() -> dict[str, Any]:
    started_at = now_utc()
    out_dir = RELEASES_DIR / started_at.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    repos_path = out_dir / "repos.csv"
    trees_path = out_dir / "trees.jsonl"
    qualified_path = out_dir / "qualified_repos.csv"
    manifest_path = out_dir / "manifest.json"
    print(f"start release={RELEASE_VERSION} out_dir={out_dir}", flush=True)

    with closing(open_ro(CANDIDATES_DB_PATH)) as candidates_db, closing(open_ro(TREES_DB_PATH)) as trees_db, closing(
        open_ro(FILTER_DB_PATH)
    ) as filter_db:
        source_meta = {
            "candidates": read_meta(candidates_db),
            "trees": {k: v for k, v in read_meta(trees_db).items() if not k.startswith("startup_")},
            "filter": read_meta(filter_db),
        }
        repos_summary = write_repos_csv(candidates_db, trees_db, repos_path)
        print(f"repos.csv rows={repos_summary['row_count']}", flush=True)
        qualified_summary = write_qualified_repos_csv(filter_db, qualified_path)
        print(
            f"qualified_repos.csv repos={qualified_summary['repo_count']} projects={qualified_summary['project_count']}",
            flush=True,
        )
        trees_summary = write_trees_jsonl(candidates_db, trees_db, trees_path)
        print(f"trees.jsonl repos={trees_summary['repo_count']} entries={trees_summary['entry_count']}", flush=True)

    files = {}
    for file_path in (repos_path, trees_path, qualified_path):
        files[file_path.name] = {"bytes": file_path.stat().st_size, "sha256": sha256_file(file_path)}

    manifest = {
        "release": RELEASE_VERSION,
        "generated_at": started_at.isoformat(),
        "source_dbs": {
            "candidates": str(CANDIDATES_DB_PATH),
            "trees": str(TREES_DB_PATH),
            "filter": str(FILTER_DB_PATH),
        },
        "source_meta": source_meta,
        "repos": repos_summary,
        "trees": trees_summary,
        "tree_entry_fields": list(TREE_ENTRY_FIELDS),
        "qualified_repos": qualified_summary,
        "notes": [
            "repos.csv: every candidate repo with fetch status; status truncated means the recursive tree API response was cut off and the repo is ignored downstream.",
            "trees.jsonl: one JSON object per repo with a 200 tree response; tree entries keep path/mode/type/sha/size, per-entry url dropped.",
            "not_found repos have no tree line.",
            "qualified_repos.csv: post-processing first pass, one row per repo; project_dirs lists every .kicad_pro directory that also holds .kicad_pcb and .kicad_sch and has a README at root, in the dir, or in its parent. List columns are JSON arrays. Truncated repos are not filtered.",
        ],
        "files": files,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    zip_path = write_release_zip(out_dir, [repos_path, qualified_path, trees_path, manifest_path])
    print(f"{zip_path.name} bytes={zip_path.stat().st_size}", flush=True)
    return {"zip": {"path": str(zip_path), "bytes": zip_path.stat().st_size, "sha256": sha256_file(zip_path)}, **manifest}


def main() -> None:
    manifest = release_github_trees()
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
