# pcb_project_scout

English | [中文](README.zh-CN.md)

Mine GitHub for open-source KiCad projects, keep the complete and documented ones, and extract every documented design change (PR or commit) as raw material for a PCB-editing benchmark: the project before the change is the input, the project after is the reference answer, the PR / issue / commit text is the task statement.

Conventions for contributors and coding agents live in [AGENTS.md](AGENTS.md).

## Get the data without touching the GitHub API

Every stage caches its raw API responses in its own SQLite file under `data/cache/`. The whole cache is published as a ModelScope dataset, [Mask2X/pcb-project-scout](https://modelscope.cn/datasets/Mask2X/pcb-project-scout), laid out exactly like `data/cache/`: one `cache/<stage>/state.sqlite.zst` per stage plus `manifest.json`.

```bash
uv run scripts/00_restore_cache.py
```

Downloads the stages listed in the `STAGES` constant, verifies sha256, and unpacks them to `data/cache/<stage>/state.sqlite`. With the caches in place every later script is a cache hit and makes no API call. Comment out the big stages (`github_repo_history` 24 GB, `github_commit_files` 50 GB unpacked) if you only need the derived tables.

| stage | script | unpacked | contents |
|---|---|---|---|
| github_code_search_kicad_pro / kicad_pcb / kicad_sch / sch | 01 | 8.3 GB | code search pages per suffix |
| github_candidates | 02 | 0.7 GB | merged candidate repo list |
| github_trees | 03 | 5.0 GB | full file tree of every repo |
| filter_kicad_projects | 04 | 0.04 GB | qualified projects and repos |
| github_repo_stats | 06 | 0.05 GB | commit / PR / issue / star counts per repo |
| github_repo_history | 07 | 24.6 GB | commits, PRs, PR changed files with patch, issues |
| improvement_events | 08 | 6.1 GB | derived: commits, PRs, issues, files, improvement events |
| github_commit_files | 09 | 50.3 GB | changed files with patch of every commit in a project dir |

## Pipeline

> Tokens from several GitHub accounts run in parallel and speed things up. Several tokens from one account share one quota and gain nothing.

0. **Restore caches** `00_restore_cache.py`. Optional. Downloads the published caches from ModelScope (see above).
1. **Multi-strategy search** `01_github_code_search_bins.py`. The GitHub Search API never guarantees a complete result set and only indexes files under 384 KB, so several suffixes are searched and combined. Each suffix in the `SUFFIXES` constant is searched by bisecting file size and cached on its own in `data/cache/github_code_search_<suffix>/state.sqlite`.
2. **Merge candidates** `02_github_merge_candidates.py`. Collects every repo returned by every suffix search, de-duplicates by `repo_id`, and writes one unified candidate list to `data/cache/github_candidates/state.sqlite`. Local only, safe to rerun.
3. **Fetch file trees** `03_github_fetch_trees.py`. Reads the candidate DB read-only, pulls each repo's full recursive file list through the GitHub Tree API, and stores the raw response plus a per-repo status in `data/cache/github_trees/state.sqlite`. No business logic. Repos whose listing GitHub truncated (21 of 39902) are marked `truncated` and ignored downstream.
4. **Local first pass** `04_filter_kicad_projects.py`. Reads the candidate and tree DBs read-only, runs offline, recomputes everything on each run, and writes `data/cache/filter_kicad_projects/state.sqlite`: `qualified_projects` (one row per directory holding `.kicad_pro` + `.kicad_pcb` + `.kicad_sch` with a README nearby), `qualified_repos` (one row per repo), `repo_filter_status`.
5. **Release tables** `05_release_github_trees.py`. Exports `repos.csv`, `trees.jsonl`, `qualified_repos.csv`, `manifest.json` to `data/releases/<date>/` and zips them. Kept for the GitHub Release A artifacts; the ModelScope dataset supersedes it.
6. **Repo activity stats** `06_github_fetch_repo_stats.py`. GitHub GraphQL, 25 repos per query: default-branch commit count, PR count total and merged, issue count, fork / archived / disabled flags, parent, pushedAt, stars. Raw responses cached by query + variables, one `repo_stats` row per repo.
7. **Repo history** `07_github_fetch_repo_history.py`. For every candidate repo, four REST listings with all pages: `commits?path=<project_dir>` per qualified project dir (whole-repo history when the repo has none), every PR, the changed files of every PR (with patch), every issue. Raw pages cached by URL + params; `listing_pages` indexes them by repo, kind, subject, page.
8. **Improvement events** `08_build_improvement_events.py`. Local post-processing over stages 07 and 09: `commits`, `commit_touches`, `pull_requests`, `pull_request_files`, `commit_files`, `issues`, and `improvement_events`. An event is one PCB change with its explanation: a PR whose changed files include `.kicad_pcb` / `.kicad_sch` / `.kicad_pro` (one event per PR and project dir, before = base sha, after = merge or head sha), or a commit listed under a qualified project dir (before = first parent, after = the commit). Per-suffix file counts, changed-file lists, and `#N` issue references are attached.
9. **Commit files** `09_github_fetch_commit_files.py`. For every commit stage 07 listed under a qualified project dir, fetches `commits/{sha}` with all file pages so commit events carry the same file detail as PR events. Rerun stage 08 afterwards.

Run in numeric order:

```bash
uv run scripts/00_restore_cache.py          # optional, skips the API entirely
uv run scripts/01_github_code_search_bins.py
uv run scripts/02_github_merge_candidates.py
uv run scripts/03_github_fetch_trees.py
uv run scripts/04_filter_kicad_projects.py
uv run scripts/06_github_fetch_repo_stats.py
uv run scripts/07_github_fetch_repo_history.py
uv run scripts/08_build_improvement_events.py
uv run scripts/09_github_fetch_commit_files.py
uv run scripts/08_build_improvement_events.py   # again, to fill commit events with files
```

Every script resumes from its own SQLite cache. Rerunning the whole chain against a complete cache makes no API call. Scripts take no command-line arguments; run parameters are constants at the top of each file.

## Results so far (2026-09-03, deep analysis covers the 39902 repos found by `.kicad_pro`)

- **Discovery.** 69181 repos contain PCB design files; 50796 use the KiCad 6+ formats, 18385 only the legacy `.sch`. Code search only indexes files under 384 KB and half of all `.kicad_pcb` files are larger, so the `.kicad_pcb` search alone finds 43% of complete projects; `.kicad_pro` is the reliable anchor and the union of suffixes is used.
- **Complete projects.** Of the 39902 `.kicad_pro` repos, 32029 (80%) hold at least one directory with project + schematic + layout files and a README nearby; 66793 such project directories in total.
- **Activity.** Median repo: 16 commits, 0 PRs, 0 issues, 0 stars. 80% never received a PR, 84% never had an issue; 6897 repos (17%) have a merged PR. 91% were created after 2022; 67% were pushed within the last year.
- **History.** 140678 PRs, 18348 of them (13%) touch KiCad files, from 4418 repos and 5453 authors; 15483 merged. KiCad PRs double every year (2022: 1287, 2024: 3016, 2025: 4031, 2026 to date: 3871). 423293 commits touch a complete project directory; 97849 issues.
- **Benchmark material.** Merged PRs inside a complete project that change a `.kicad_pcb`: 8719 PRs from 3022 repos, 3292 with a description over 100 characters, 801 linked to an issue. Commit events with a `.kicad_pcb` change: 275885, of which 34535 have a multi-line message and 15864 a body over 100 characters. Each event carries before / after sha, the changed KiCad files with status and line counts, and the patch text in the cache.

## Layout

```
pcb_project_scout/
├── README.md
├── README.zh-CN.md
├── AGENTS.md         # conventions for contributors and coding agents
├── pyproject.toml    # deps: uv, httpx, python-dotenv, modelscope, zstandard
├── .env.example      # env: GITHUB_TOKEN_1..N (fetching), MODELSCOPE_TOKEN (publishing only)
├── scripts/          # self-contained stage scripts, run in numeric order, all resumable or fully recomputable
│   ├── 00_restore_cache.py             # download + unpack the published caches from ModelScope
│   ├── 01_github_code_search_bins.py   # multi-suffix code search (network)
│   ├── 02_github_merge_candidates.py   # merge candidates (local)
│   ├── 03_github_fetch_trees.py        # fetch file trees (network, raw responses only)
│   ├── 04_filter_kicad_projects.py     # local first pass (local)
│   ├── 05_release_github_trees.py      # CSV / JSONL export for GitHub Release A (local)
│   ├── 06_github_fetch_repo_stats.py   # repo activity stats via GraphQL (network)
│   ├── 07_github_fetch_repo_history.py # commits / PRs / PR files / issues per repo (network)
│   ├── 08_build_improvement_events.py  # improvement events from the history caches (local)
│   └── 09_github_fetch_commit_files.py # changed files of every commit in a project dir (network)
└── data/             # gitignored; caches stay local, releases go to ModelScope
    ├── cache/<stage>/state.sqlite    # per-stage resumable cache; upstream DBs are read-only downstream
    └── releases/                     # packed releases and the restore download dir
```

The script that packs and uploads the caches is not part of this repo.

## TODO

### Search strategies

- [x] Repos containing `kicad_pro` files (39902 repos)
- [x] Repos containing `kicad_pcb` files (26676 repos)
- [x] Repos containing `kicad_sch` files (37202 repos)
- [x] Repos containing `sch` files (30515 repos; merged candidate list is 69181 repos, 50796 with KiCad 6+ files)

### File trees and release

- [x] Fetch file trees for the first candidate set
- [x] Truncated repos: decided to ignore (tiny share, completion too costly)
- [x] Release A on GitHub (`release-A`: repos, trees, qualified repos; 39902 repos, 39898 trees, 17.3M entries)
- [ ] Release B on ModelScope: every stage cache as `cache/<stage>/state.sqlite.zst`
- [ ] Run stages 03 to 09 on the 29279 repos added by the other three suffix searches

### Post-processing

- [x] First pass: repos with complete projects (kicad_pro, kicad_sch, kicad_pcb, README)
- [ ] Filter repos that ship 3D models

### Further filtering

- [x] Repo activity stats, all ~40k repos (39895 fetched, 7 not found, 1597 GraphQL queries)
- [x] Improvement history for every candidate repo (321k requests, 964k commits, 140k PRs with 3.45M changed files, 238k issues)
- [x] Commit files for every commit in a project dir (423293 commits, 8.87M changed files)
- [x] Improvement events table (523908 events: 33844 from PRs, 490064 from commits, all with changed-file detail)
- [ ] Event quality filters: drop template / TODO bodies, cap change size, detect save-only rewrites of `.kicad_sch` by comparing netlists, per-repo quota

### Refactoring

- [x] Split scripts so API caching and business logic are decoupled (one stage DB per stage)
