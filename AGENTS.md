# AGENTS.md

Rules for anyone, human or coding agent, who changes this repo. Read [README.md](README.md) first for the pipeline and layout.

## Architecture rules

- **Fetching and post-processing stay decoupled.** Fetch scripts only cache raw API responses and make no business decisions. Post-processing scripts only read local caches. Changing a filter rule must never cost API quota.
- **Candidate list is independent of search strategy.** Downstream stages receive one unified repo list and never care which strategy found a repo. A new suffix search writes its own cache DB; the merge stage picks it up automatically.
- **One SQLite per stage, and it is the only state.** Each stage owns `data/cache/<stage>/state.sqlite`. Upstream DBs are opened read-only (`mode=ro`) by downstream stages. Never write into another stage's DB.
- **Every stage is resumable or fully recomputable.** Network stages resume from their cache after any interruption. Local stages recompute everything on each run. Failed requests are retried by default; permanent bad rows are rare enough to ignore.
- **Cache only reusable responses.** Store 200 and terminal failures (404, 409, 451, blocked or DMCA 403). Do not store rate limits, 5xx, or other retryable errors; they are refetched next run.
- **Cache everything, chase nothing.** GitHub search results are stable for months, so responses are cached in full and freshness is not a goal. Raw responses are keyed by request URL plus params inside SQLite, never as loose files.

## Coding rules

- **No CLI.** No argparse, no `sys.argv`, no subcommands. Every run parameter (suffixes, intervals, paths) is a constant at the top of the script. Changing a parameter means editing code.
- **No speculative error handling.** Handle only failures actually observed (for example non-JSON API bodies). Do not pre-catch scenarios that have not happened; let unknown problems surface early and loudly.
- **Small, self-contained scripts.** One script per stage, no shared package yet. Reuse helper functions by copying while the script count is small; extract a common module only once the GitHub scripts have accumulated. Prefer incremental commits over large rewrites.
- **English only in code.** Comments, docstrings, log lines, and error messages are English. Chinese lives only in README.zh-CN.md.
- **Keep the README TODO list current.** Tick items when done, add new ones in both README files.
- **Run with uv.** `uv run scripts/<nn>_<name>.py` from the repo root. Python 3.11+, deps are httpx and python-dotenv only.
- **Tokens come from `.env`.** `GITHUB_TOKEN_1..N`. Never print, log, or commit them.

## Working with an agent

- Report cache state before assuming pipeline state: query `repo_status` in `data/cache/github_trees/state.sqlite` and `query_status` in the search caches.
- Do not delete or rewrite a stage DB without being asked. `data/` is gitignored: stage caches exist only on the machine that fetched them and cannot be recovered from git. Releases are published as GitHub Release assets.
- Keep README.md and README.zh-CN.md in sync when the pipeline or layout changes.
