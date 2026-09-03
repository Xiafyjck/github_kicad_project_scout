from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

import zstandard
from modelscope import snapshot_download


# Stage 00: restore the published caches so no GitHub API call is needed to run stages 01..09.
# The dataset mirrors data/cache/: cache/<stage>/state.sqlite.zst plus manifest.json.
# Pick the stages you need; everything downstream of a restored stage reads it read-only.
REPO_ID = "Mask2X/pcb-project-scout"
REVISION = "master"
STAGES = (
    "github_code_search_kicad_pro",   # 01: code search caches, one per suffix
    "github_code_search_kicad_pcb",
    "github_code_search_kicad_sch",
    "github_code_search_sch",
    "github_candidates",              # 02: merged candidate list
    "github_trees",                   # 03: full file trees
    "filter_kicad_projects",          # 04: local first pass
    "github_repo_stats",              # 06: GraphQL activity stats
    "github_repo_history",            # 07: commits / PRs / PR files / issues (24 GB unpacked)
    "github_commit_files",            # 09: changed files of every commit (50 GB unpacked)
    "improvement_events",             # 08: derived events (can be rebuilt by running 08)
)
CACHE_DIR = Path("data/cache")
DOWNLOAD_DIR = Path("data/releases/download")
OVERWRITE = False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(patterns: list[str]) -> Path:
    root = snapshot_download(repo_id=REPO_ID, repo_type="dataset", revision=REVISION, local_dir=str(DOWNLOAD_DIR), allow_patterns=patterns)
    return Path(root)


def restore_stage(root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    stage = entry["stage"]
    src = root / entry["path"]
    target = CACHE_DIR / stage / "state.sqlite"
    if target.exists() and not OVERWRITE:
        print(f"{stage}: {target} exists, skip (set OVERWRITE = True to replace)", flush=True)
        return {"stage": stage, "status": "skipped"}
    started = time.monotonic()
    digest = sha256_file(src)
    if digest != entry["zst_sha256"]:
        raise RuntimeError(f"{stage}: sha256 mismatch for {src}: {digest} != {entry['zst_sha256']}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".sqlite.part")
    for stale in (target.with_name("state.sqlite-wal"), target.with_name("state.sqlite-shm")):
        stale.unlink(missing_ok=True)
    with src.open("rb") as fin, tmp.open("wb") as fout:
        zstandard.ZstdDecompressor().copy_stream(fin, fout, read_size=1 << 22, write_size=1 << 22)
    tmp.replace(target)
    print(f"{stage}: restored {target.stat().st_size / 1e9:.2f} GB in {time.monotonic() - started:.0f}s", flush=True)
    return {"stage": stage, "status": "restored", "sqlite_bytes": target.stat().st_size}


def restore_cache() -> dict[str, Any]:
    root = download(["manifest.json"])
    manifest = json.loads((root / "manifest.json").read_text())
    wanted = [entry for entry in manifest["stages"] if entry["stage"] in STAGES]
    missing = set(STAGES) - {entry["stage"] for entry in wanted}
    if missing:
        raise RuntimeError(f"stages not in release {manifest['release']}: {sorted(missing)}")
    print(f"release {manifest['release']} from {REPO_ID}@{REVISION}: {len(wanted)} stages, "
          f"{sum(e['zst_bytes'] for e in wanted) / 1e9:.1f} GB to download, {sum(e['sqlite_bytes'] for e in wanted) / 1e9:.1f} GB unpacked", flush=True)
    download([entry["path"] for entry in wanted])
    results = [restore_stage(root, entry) for entry in wanted]
    return {"release": manifest["release"], "repo_id": REPO_ID, "download_dir": str(DOWNLOAD_DIR), "results": results}


def main() -> None:
    print(json.dumps(restore_cache(), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
