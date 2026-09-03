# pcb_project_scout

English | [中文](README.zh-CN.md)

Mine GitHub for open-source KiCad projects and filter them down to complete, documented boards.

Conventions for contributors and coding agents live in [AGENTS.md](AGENTS.md).

## Pipeline

> Tokens from several GitHub accounts run in parallel and speed things up. Several tokens from one account share one quota and gain nothing.

1. **Multi-strategy search** `00_github_code_search_bins.py`. The GitHub Search API never guarantees a complete result set, so several search strategies are combined for coverage. Each suffix listed in the `SUFFIXES` constant is searched in turn and cached on its own in `data/cache/github_code_search_<suffix>/state.sqlite`. Add a suffix by editing the constant.
2. **Merge candidates** `01_github_merge_candidates.py`. Collects every repo returned by every strategy, de-duplicates by `repo_id`, and writes one unified candidate list to `data/cache/github_candidates/state.sqlite`. Local only, safe to rerun.
3. **Fetch file trees** `02_github_fetch_trees.py`. Reads the candidate DB read-only, pulls each repo's full recursive file list through the GitHub Tree API, and stores the raw response plus a per-repo status in `data/cache/github_trees/state.sqlite`. No business logic.
4. **Ignore truncated repos** Repos whose recursive tree response was cut off by GitHub (21 of 39902, about 0.05%) are ignored and never filtered. They are mostly large grab-bag repos; completing them by downloading source archives costs far more than it returns. Their status stays `truncated`.
5. **Local first pass (post-processing)** `03_filter_kicad_projects.py`. Reads the candidate and tree DBs read-only, runs entirely offline, recomputes everything on each run, and writes `data/cache/filter_kicad_projects/state.sqlite`: `qualified_projects` (one row per project directory), `qualified_repos` (one row per repo), `repo_filter_status` (verdict for every candidate repo).
6. **Release** `04_release_github_trees.py`. Reads the three DBs above read-only and exports to `data/releases/<date>/`: `repos.csv` (every candidate with fetch status), `trees.jsonl` (one full file tree per line, entries keep path/mode/type/sha/size), `qualified_repos.csv` (first-pass qualified repos, one row per repo), `manifest.json` (counts, per-DB meta, filter rule, file sha256). The whole folder is then packed into `data/releases/<date>.zip`, which is uploaded as a GitHub Release asset. Nothing under `data/` is committed.

7. **Repo activity stats** `05_github_fetch_repo_stats.py`. Reads the candidate DB read-only and asks the GitHub GraphQL API, 25 repos per query, for default-branch commit count, PR count total and merged, issue count, fork/archived/disabled flags, parent, pushedAt, stars, and more. Raw responses are cached by query + variables and one row per repo lands in `repo_stats` inside `data/cache/github_repo_stats/state.sqlite`. Feeds the shortlist for the improvement-history stage.

8. **Project improvement history** `06_github_fetch_repo_history.py`. Reads candidates, filter, and stats DBs read-only and walks, for every candidate repo, four REST listings with all pages: `commits?path=<project_dir>` per qualified project dir (whole-repo history when the repo has none), every PR, the changed files of every PR, and every issue. Raw pages are cached by URL + params in `data/cache/github_repo_history/state.sqlite`; `listing_pages` indexes them by repo, kind, subject, page. Shortlisted and qualified repos are queued first.

9. **Improvement events (post-processing)** `07_build_improvement_events.py`. Reads the stage 06 cache read-only, runs offline, recomputes everything each run, and writes `data/cache/improvement_events/state.sqlite`: `commits`, `commit_touches`, `pull_requests`, `pull_request_files` (patch text stays in the stage 06 cache, referenced by `api_cache_id`), `issues`, and `improvement_events`. An event is one PCB change with its explanation: a PR whose changed files include `.kicad_pcb` / `.kicad_sch` / `.kicad_pro` (one event per PR and project dir, before = base sha, after = merge or head sha, changed KiCad files listed), or a commit from `commits?path=<qualified project dir>` (before = first parent, after = the commit, files unknown). `#N` references in the text are resolved to the repo's issues.

10. **Commit files** `08_github_fetch_commit_files.py`. For every commit that stage 06 listed under a qualified project dir (from stage 07's `commit_touches`), fetches `commits/{sha}` with all file pages so commit events get the same changed-file detail as PR events. Raw responses cached in `data/cache/github_commit_files/state.sqlite`, indexed by `commit_pages`. Rerun stage 07 afterwards to fill in the commit events.

Run the scripts in numeric order:

```bash
uv run scripts/00_github_code_search_bins.py
uv run scripts/01_github_merge_candidates.py
uv run scripts/02_github_fetch_trees.py
uv run scripts/03_filter_kicad_projects.py
uv run scripts/04_release_github_trees.py
uv run scripts/05_github_fetch_repo_stats.py
uv run scripts/06_github_fetch_repo_history.py
uv run scripts/07_build_improvement_events.py
uv run scripts/08_github_fetch_commit_files.py
```

Every script resumes from its own SQLite cache. Rerunning the whole chain against a complete cache makes no API calls.

## Layout

```
pcb_project_scout/
├── README.md
├── README.zh-CN.md
├── AGENTS.md         # conventions for contributors and coding agents
├── pyproject.toml    # deps: uv, httpx, python-dotenv
├── .env.example      # env: GITHUB_TOKEN_1..N
├── scripts/          # self-contained stage scripts, run in numeric order, all resumable or fully recomputable
│   ├── 00_github_code_search_bins.py # multi-strategy search (network)
│   ├── 01_github_merge_candidates.py # merge candidates (local)
│   ├── 02_github_fetch_trees.py      # fetch file trees (network, raw responses only)
│   ├── 03_filter_kicad_projects.py   # local first pass (local)
│   ├── 04_release_github_trees.py    # release export (local)
│   ├── 05_github_fetch_repo_stats.py # repo activity stats via GraphQL (network)
│   ├── 06_github_fetch_repo_history.py # commits / PRs / PR files / issues per repo (network)
│   ├── 07_build_improvement_events.py  # improvement events from the history cache (local)
│   └── 08_github_fetch_commit_files.py # changed files of every commit in a qualified project dir (network)
└── data/             # gitignored; caches stay local, releases go to GitHub Releases
    ├── cache/<stage>/state.sqlite    # per-stage resumable cache; upstream DBs are read-only downstream
    ├── releases/<date>/              # release artifacts, unpacked
    └── releases/<date>.zip           # same release packed, uploaded to GitHub Releases
```

## TODO

### Search strategies

- [x] Repos containing `kicad_pro` files
- [x] Repos containing `kicad_pcb` files (26676 repos)
- [x] Repos containing `kicad_sch` files (37202 repos)
- [x] Repos containing `sch` files (30515 repos; merged candidate list is 69181 repos, 50796 with KiCad 6+ files)

### File trees and release

- [x] Fetch file trees for the first candidate set
- [x] Truncated repos: decided to ignore (tiny share, completion too costly)
- [x] Release A (`data/releases/2026-09-02/`, 39902 repos, 39898 trees, 17.3M entries, 32029 qualified repos / 66793 projects)

### Post-processing

- [x] First pass: repos with complete projects (kicad_pro, kicad_sch, kicad_pcb, README)
- [ ] Filter repos that ship 3D models

### Further filtering
- [x] Repo activity stats, all ~40k repos (done 2026-09-03: 39895 fetched, 7 not found, 1597 GraphQL queries). GraphQL returns default-branch commit count, PR count total and merged, issue count, isFork, isArchived, pushedAt, stars. About 400 to 800 requests, one token finishes within an hour. Caching unchanged: raw JSON keyed by query + variables in SQLite.
- [x] Improvement history for every candidate repo (done 2026-09-03: 321k requests, 964k commits, 140k PRs with 3.45M changed files, 238k issues, 24.6 GB cache): for every qualified project dir fetch `commits?path=<project_dir>` to get the commit sequence touching that project; for every PR fetch `files` to see whether kicad_pcb / kicad_sch changed; full issue list. REST API, cost scales with the shortlist.
- [x] (done 2026-09-03: 390317 events in `data/cache/improvement_events`, 29675 from PRs of which 27303 merged, 356473 from commits) Link commits / PRs / issues to project dirs and produce an "improvement event" table: tree sha before and after, changed files, PR or issue text. Benchmark tasks are picked from here: the before state is the input, the after state is the reference answer.
- [ ] Commit files for every commit event (stage 08, started 2026-09-03), then rebuild stage 07 so commit events carry per-suffix file counts
- [ ] Publish a release

### Refactoring

- [x] Split scripts so API caching and business logic are decoupled (one stage DB per stage)
