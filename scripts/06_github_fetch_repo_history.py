from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import find_dotenv, load_dotenv


# GitHub API facts: REST v3 list endpoints, addressed by numeric repo id so renames do not matter.
GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_RATE_LIMIT_ENDPOINT = "/rate_limit"
GITHUB_ACCEPT_HEADER = "application/vnd.github+json"
GITHUB_PER_PAGE_MAX = 100
COMMITS_ENDPOINT_TEMPLATE = "/repositories/{repo_id}/commits"
PULLS_ENDPOINT_TEMPLATE = "/repositories/{repo_id}/pulls"
PULL_FILES_ENDPOINT_TEMPLATE = "/repositories/{repo_id}/pulls/{number}/files"
ISSUES_ENDPOINT_TEMPLATE = "/repositories/{repo_id}/issues"

# Collection policy: every candidate repo, four kinds of listing, all pages.
#   commits: one listing per qualified project dir (commits?path=<dir>); repos without a qualified
#            project get the whole-repo history instead.
#   pulls:   every PR, then the changed-file list of every PR.
#   issues:  every issue (GitHub includes PRs here too; kept raw, filtered downstream).
# Core rate limit is 5000/hour per token; the interval keeps one token under that.
REQUEST_INTERVAL_SECONDS = 0.75
MAX_SERVER_ERROR_RETRIES = 4
MAX_SECONDARY_RATE_LIMIT_RETRIES = 6
PRIMARY_RATE_LIMIT_SAFETY_REMAINING = 25

# Local state: stage 06 caches raw list pages and an index of which page belongs to which repo/kind.
# Upstream DBs are read-only: candidates (repo list), filter (project dirs), stats (priority).
CANDIDATES_DB_PATH = Path("data/cache/github_candidates/state.sqlite")
FILTER_DB_PATH = Path("data/cache/filter_kicad_projects/state.sqlite")
STATS_DB_PATH = Path("data/cache/github_repo_stats/state.sqlite")
CACHE_DIR = Path("data/cache/github_repo_history")
DB_PATH = CACHE_DIR / "state.sqlite"
SCHEMA_VERSION = 1

CACHEABLE_FAILURE_STATUSES = {"not_found", "empty_repo", "unavailable_legal", "access_blocked"}


@dataclass(frozen=True)
class PageResult:
    status_code: int
    data: Any
    api_cache_id: int | None
    from_cache: bool
    next_page: int | None
    failure_status: str | None = None


class StopRequested(Exception):
    pass


ENV_PATH = find_dotenv(usecwd=True)
if not ENV_PATH:
    raise RuntimeError("create .env in the repo root")
load_dotenv(ENV_PATH, override=False)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_tokens() -> list[str]:
    tokens = []
    seen = set()
    for i in range(1, 100):
        token = os.getenv(f"GITHUB_TOKEN_{i}")
        if token is None or token.strip() == "":
            continue
        token = token.strip()
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    if not tokens:
        raise RuntimeError("set GITHUB_TOKEN_1 in .env")
    return tokens


def github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": GITHUB_ACCEPT_HEADER,
        "Authorization": f"Bearer {token}",
        "User-Agent": "pcb-project-scout-repo-history-cache",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


def response_data(response: httpx.Response) -> Any:
    text = response.text
    stripped = text.lstrip()
    content_type = response.headers.get("content-type", "").lower()
    if not stripped:
        return {"_non_json_text": ""}
    if "json" not in content_type and not stripped.startswith(("{", "[")):
        return {"_non_json_text": stripped[:300]}
    return json.loads(text)


def github_message_from_data(data: Any) -> str:
    if isinstance(data, dict):
        if "_non_json_text" in data:
            return str(data["_non_json_text"])[:300]
        return str(data.get("message", ""))
    return str(data)[:300]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def response_json_text(response: httpx.Response, data: Any) -> str:
    if isinstance(data, dict) and "_non_json_text" in data:
        return json.dumps(data, ensure_ascii=False)
    return response.text


def cache_key(endpoint: str, params: dict[str, Any]) -> str:
    return f"rest:{endpoint}:{canonical_json(params)}"


def parse_next_page(link_header: str | None) -> int | None:
    # Link: <https://api.github.com/...?page=3>; rel="next", <...>; rel="last"
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip()
        if 'rel="next"' not in section:
            continue
        url = section.split(";", 1)[0].strip().strip("<>")
        query = url.split("?", 1)[1] if "?" in url else ""
        for pair in query.split("&"):
            if pair.startswith("page="):
                return int(pair[len("page=") :])
    return None


def is_rate_limit_response(status_code: int, headers: dict[str, str], message: str) -> bool:
    if status_code not in {403, 429}:
        return False
    if headers.get("x-ratelimit-remaining") == "0":
        return True
    message_lower = message.lower()
    return "rate limit" in message_lower or "abuse detection" in message_lower


def failure_status(status_code: int, headers: dict[str, str], data: Any) -> str:
    message = github_message_from_data(data).lower()
    if status_code == 404:
        return "not_found"
    if status_code == 409:
        return "empty_repo"
    if status_code == 451:
        return "unavailable_legal"
    if is_rate_limit_response(status_code, headers, message):
        return "retryable_error"
    if status_code >= 500 or status_code in {429, 422}:
        return "retryable_error"
    if status_code == 403 and ("blocked" in message or "dmca" in message):
        return "access_blocked"
    if status_code == 403:
        return "retryable_error"
    return "error"


def should_cache(status_code: int, headers: dict[str, str], data: Any) -> bool:
    if status_code == 200:
        return True
    return failure_status(status_code, headers, data) in CACHEABLE_FAILURE_STATUSES


def setup_db() -> sqlite3.Connection:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
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

            -- api_cache: raw REST list pages. 200 plus terminal failures (404, 409, 451, blocked 403).
            create table if not exists api_cache (
              id integer primary key autoincrement,
              cache_key text not null unique,
              endpoint text not null,
              params_json text not null,
              status_code integer not null,
              response_json text not null,
              headers_json text not null,
              token_no integer,
              fetched_at text not null
            );

            -- listing_pages: index of every cached page by repo, kind and subject.
            -- kind: commits (subject = project dir, '' = whole repo), pulls (subject ''),
            --       pull_files (subject = PR number), issues (subject '').
            create table if not exists listing_pages (
              repo_id integer not null,
              kind text not null,
              subject text not null,
              page integer not null,
              api_cache_id integer not null,
              status_code integer not null,
              item_count integer not null,
              next_page integer,
              fetched_at text not null,
              primary key (repo_id, kind, subject, page),
              foreign key (api_cache_id) references api_cache(id)
            );

            -- repo_status: resumable per-repo state. A repo is fetched once every listing it needs is
            -- cached; a rerun replays cached pages for free and only requests what is missing.
            create table if not exists repo_status (
              repo_id integer not null primary key,
              repo_full_name text not null,
              priority integer not null,
              status text not null,
              message text not null,
              request_count integer not null,
              attempt_count integer not null,
              token_no integer,
              updated_at text not null
            );

            create table if not exists errors (
              id integer primary key autoincrement,
              repo_id integer,
              repo_full_name text,
              token_no integer,
              endpoint text not null,
              status_code integer,
              error text not null,
              message text not null,
              seen_at text not null
            );

            create index if not exists idx_api_cache_endpoint
              on api_cache(endpoint, params_json);
            create index if not exists idx_repo_status_status
              on repo_status(status, priority);
            create index if not exists idx_listing_pages_repo
              on listing_pages(repo_id, kind);
            """
        )
        db.executemany(
            "insert or replace into meta (key, value) values (?, ?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("github_api_version", GITHUB_API_VERSION),
                ("candidates_db", str(CANDIDATES_DB_PATH)),
                ("filter_db", str(FILTER_DB_PATH)),
                ("stats_db", str(STATS_DB_PATH)),
                ("commits_endpoint", COMMITS_ENDPOINT_TEMPLATE),
                ("pulls_endpoint", PULLS_ENDPOINT_TEMPLATE),
                ("pull_files_endpoint", PULL_FILES_ENDPOINT_TEMPLATE),
                ("issues_endpoint", ISSUES_ENDPOINT_TEMPLATE),
                ("per_page", str(GITHUB_PER_PAGE_MAX)),
            ],
        )
    return db


def open_ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


class Store:
    def __init__(self) -> None:
        self.db = setup_db()
        self.db_lock = threading.Lock()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.db.close()

    def write_error(
        self,
        repo: dict[str, Any] | None,
        token_no: int | None,
        endpoint: str,
        status_code: int | None,
        error: str,
        message: str,
    ) -> None:
        with self.db_lock:
            with self.db:
                self.db.execute(
                    """
                    insert into errors (
                      repo_id, repo_full_name, token_no, endpoint,
                      status_code, error, message, seen_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        None if repo is None else repo.get("repo_id"),
                        None if repo is None else repo.get("repo_full_name"),
                        token_no,
                        endpoint,
                        status_code,
                        error,
                        message[:1000],
                        now_utc(),
                    ),
                )

    def write_api_cache(
        self,
        key: str,
        endpoint: str,
        params: dict[str, Any],
        response: httpx.Response,
        data: Any,
        token_no: int,
    ) -> int:
        with self.db_lock:
            with self.db:
                self.db.execute(
                    """
                    insert into api_cache (
                      cache_key, endpoint, params_json, status_code,
                      response_json, headers_json, token_no, fetched_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(cache_key) do update set
                      status_code = excluded.status_code,
                      response_json = excluded.response_json,
                      headers_json = excluded.headers_json,
                      token_no = excluded.token_no,
                      fetched_at = excluded.fetched_at
                    """,
                    (
                        key,
                        endpoint,
                        canonical_json(params),
                        response.status_code,
                        response_json_text(response, data),
                        json.dumps(dict(response.headers), ensure_ascii=False),
                        token_no,
                        now_utc(),
                    ),
                )
                row = self.db.execute("select id from api_cache where cache_key = ?", (key,)).fetchone()
        if row is None:
            raise RuntimeError(f"failed to write api_cache for {key}")
        return int(row["id"])

    def cached_page(self, key: str) -> PageResult | None:
        with self.db_lock:
            row = self.db.execute(
                "select id, status_code, response_json, headers_json from api_cache where cache_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row["response_json"])
        headers = json.loads(row["headers_json"])
        status_code = int(row["status_code"])
        if not should_cache(status_code, headers, data):
            return None
        return PageResult(
            status_code=status_code,
            data=data,
            api_cache_id=int(row["id"]),
            from_cache=True,
            next_page=parse_next_page(headers.get("link")) if status_code == 200 else None,
            failure_status=None if status_code == 200 else failure_status(status_code, headers, data),
        )

    def save_listing_page(
        self,
        repo: dict[str, Any],
        kind: str,
        subject: str,
        page: int,
        result: PageResult,
    ) -> None:
        item_count = len(result.data) if isinstance(result.data, list) else 0
        with self.db_lock:
            with self.db:
                self.db.execute(
                    """
                    insert or replace into listing_pages (
                      repo_id, kind, subject, page, api_cache_id, status_code, item_count, next_page, fetched_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        repo["repo_id"],
                        kind,
                        subject,
                        page,
                        result.api_cache_id,
                        result.status_code,
                        item_count,
                        result.next_page,
                        now_utc(),
                    ),
                )

    def mark_repo_status(
        self,
        repo: dict[str, Any],
        status: str,
        message: str,
        request_count: int,
        token_no: int | None,
        attempted: bool,
    ) -> None:
        with self.db_lock:
            with self.db:
                old = self.db.execute(
                    "select attempt_count, request_count from repo_status where repo_id = ?",
                    (repo["repo_id"],),
                ).fetchone()
                attempt_count = int(attempted) if old is None else int(old["attempt_count"]) + int(attempted)
                total_requests = request_count if old is None else int(old["request_count"]) + request_count
                self.db.execute(
                    """
                    update repo_status
                    set status = ?, message = ?, request_count = ?, attempt_count = ?, token_no = ?, updated_at = ?
                    where repo_id = ?
                    """,
                    (status, message[:1000], total_requests, attempt_count, token_no, now_utc(), repo["repo_id"]),
                )

    def sync_candidates(self) -> dict[str, Any]:
        synced_at = now_utc()
        with closing(open_ro(CANDIDATES_DB_PATH)) as source:
            candidates = {
                int(row["repo_id"]): str(row["repo_full_name"])
                for row in source.execute("select repo_id, repo_full_name from candidate_repos")
            }
        with closing(open_ro(FILTER_DB_PATH)) as filter_db:
            qualified_ids = {int(row["repo_id"]) for row in filter_db.execute("select distinct repo_id from qualified_projects")}
        with closing(open_ro(STATS_DB_PATH)) as stats_db:
            shortlisted_ids = {
                int(row["repo_id"])
                for row in stats_db.execute(
                    """
                    select repo_id from repo_stats
                    where merged_pull_request_count >= 1
                       or (coalesce(commit_count, 0) >= 5 and issue_count >= 1)
                    """
                )
            }

        # Priority only orders the queue: shortlisted + qualified first, then qualified, then the rest.
        rows = []
        for repo_id, repo_full_name in sorted(candidates.items()):
            priority = (2 if repo_id in shortlisted_ids else 0) + (1 if repo_id in qualified_ids else 0)
            rows.append((repo_id, repo_full_name, priority, synced_at))
        with self.db_lock:
            with self.db:
                self.db.executemany(
                    """
                    insert into repo_status (
                      repo_id, repo_full_name, priority, status, message,
                      request_count, attempt_count, token_no, updated_at
                    )
                    values (?, ?, ?, 'pending', '', 0, 0, null, ?)
                    on conflict(repo_id) do update set
                      repo_full_name = excluded.repo_full_name,
                      priority = excluded.priority
                    """,
                    rows,
                )
                self.db.execute(
                    "insert or replace into meta (key, value) values ('last_candidates_synced_at', ?)",
                    (synced_at,),
                )
        return {
            "candidate_repo_count": len(candidates),
            "qualified_repo_count": len(qualified_ids & set(candidates)),
            "shortlisted_repo_count": len(shortlisted_ids & set(candidates)),
        }

    def load_project_dirs(self) -> dict[int, list[str]]:
        dirs: dict[int, list[str]] = {}
        with closing(open_ro(FILTER_DB_PATH)) as filter_db:
            for row in filter_db.execute("select repo_id, project_dir from qualified_projects order by repo_id, project_dir"):
                dirs.setdefault(int(row["repo_id"]), []).append(str(row["project_dir"]))
        return dirs

    def load_todo_repos(self) -> list[dict[str, Any]]:
        with self.db_lock:
            return [
                dict(row)
                for row in self.db.execute(
                    """
                    select repo_id, repo_full_name, priority
                    from repo_status
                    where status in ('pending', 'retryable_error')
                    order by priority desc, repo_id
                    """
                )
            ]

    def status_summary(self) -> dict[str, Any]:
        with self.db_lock:
            status_counts = {
                row["status"]: row["count"]
                for row in self.db.execute("select status, count(*) as count from repo_status group by status order by status")
            }
            kind_counts = {
                row["kind"]: {"pages": row["pages"], "items": row["items"]}
                for row in self.db.execute(
                    "select kind, count(*) as pages, sum(item_count) as items from listing_pages group by kind order by kind"
                )
            }
            api_cache_count = self.db.execute("select count(*) from api_cache").fetchone()[0]
            error_count = self.db.execute("select count(*) from errors").fetchone()[0]
        return {
            "api_cache_count": api_cache_count,
            "listing_pages_by_kind": kind_counts,
            "repo_status_counts": status_counts,
            "error_count": error_count,
        }


def check_tokens(tokens: list[str], store: Store) -> list[tuple[int, str]]:
    ready_tokens = []
    for token_no, token in enumerate(tokens, start=1):
        with httpx.Client(base_url=GITHUB_API_BASE, headers=github_headers(token), timeout=30) as client:
            response = client.get(GITHUB_RATE_LIMIT_ENDPOINT)
        data = response_data(response)
        if response.status_code >= 400:
            message = github_message_from_data(data)
            store.write_error(None, token_no, GITHUB_RATE_LIMIT_ENDPOINT, response.status_code, "StartupCheckError", message)
            print(f"token#{token_no} skipped startup HTTP {response.status_code}: {message[:160]}", flush=True)
            continue
        core = data.get("resources", {}).get("core", {})
        print(f"token#{token_no} ready core_remaining={core.get('remaining', '?')}/{core.get('limit', '?')}", flush=True)
        ready_tokens.append((token_no, token))
    if not ready_tokens:
        raise RuntimeError("no usable GitHub tokens after startup check")
    return ready_tokens


def fetch_github_repo_history() -> dict[str, Any]:
    with Store() as store:
        sync_summary = store.sync_candidates()
        project_dirs = store.load_project_dirs()
        todo_repos = store.load_todo_repos()
        token_count = 0
        ready_tokens: list[tuple[int, str]] = []
        if todo_repos:
            tokens = load_tokens()
            token_count = len(tokens)
            ready_tokens = check_tokens(tokens, store)

        work_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        for repo in todo_repos:
            work_queue.put(repo)
        for _ in ready_tokens:
            work_queue.put(None)
        stop_event = threading.Event()

        def sleep_or_stop(seconds: float) -> None:
            if stop_event.wait(seconds):
                raise StopRequested()

        def collect_with_token(token_no: int, token: str) -> None:
            last_started_at = 0.0

            def wait_before_request() -> None:
                nonlocal last_started_at
                elapsed = time.monotonic() - last_started_at
                if elapsed < REQUEST_INTERVAL_SECONDS:
                    sleep_or_stop(REQUEST_INTERVAL_SECONDS - elapsed)
                last_started_at = time.monotonic()

            def sleep_for_primary_rate_limit_if_needed(response: httpx.Response) -> None:
                remaining_text = response.headers.get("x-ratelimit-remaining")
                reset_text = response.headers.get("x-ratelimit-reset")
                if remaining_text is None or reset_text is None:
                    return
                remaining = int(remaining_text)
                reset = int(reset_text)
                if remaining > PRIMARY_RATE_LIMIT_SAFETY_REMAINING:
                    return
                sleep_seconds = max(5, reset - time.time() + 2)
                reset_utc = datetime.fromtimestamp(reset, timezone.utc).isoformat()
                print(f"token#{token_no} low core rate limit remaining={remaining}, sleep {sleep_seconds:.1f}s until {reset_utc}", flush=True)
                sleep_or_stop(sleep_seconds)

            def get_page(repo: dict[str, Any], endpoint: str, params: dict[str, Any], client: httpx.Client) -> PageResult:
                key = cache_key(endpoint, params)
                cached = store.cached_page(key)
                if cached is not None:
                    return cached

                server_errors = 0
                secondary_rate_limit_errors = 0
                while True:
                    wait_before_request()
                    try:
                        response = client.get(endpoint, params=params)
                    except httpx.TransportError as exc:
                        server_errors += 1
                        if server_errors <= MAX_SERVER_ERROR_RETRIES:
                            sleep_seconds = min(30, 2**server_errors)
                            print(f"token#{token_no} transport {type(exc).__name__}, retry {server_errors}, sleep {sleep_seconds}s: {repo['repo_full_name']}", flush=True)
                            sleep_or_stop(sleep_seconds)
                            continue
                        raise RuntimeError(
                            f"request failed after {MAX_SERVER_ERROR_RETRIES} retries: {repo['repo_full_name']} {endpoint}: {type(exc).__name__}: {exc}"
                        ) from exc

                    data = response_data(response)
                    headers = dict(response.headers)
                    message = github_message_from_data(data)
                    status_code = response.status_code

                    if status_code == 200:
                        break

                    if status_code in {403, 429} and response.headers.get("retry-after"):
                        secondary_rate_limit_errors += 1
                        if secondary_rate_limit_errors > MAX_SECONDARY_RATE_LIMIT_RETRIES:
                            break
                        sleep_seconds = int(float(response.headers["retry-after"])) + 1
                        print(f"token#{token_no} retry-after, retry {secondary_rate_limit_errors}, sleep {sleep_seconds}s: {repo['repo_full_name']}", flush=True)
                        sleep_or_stop(sleep_seconds)
                        continue

                    if is_rate_limit_response(status_code, headers, message):
                        if headers.get("x-ratelimit-remaining") == "0":
                            reset = int(headers.get("x-ratelimit-reset", "0") or "0")
                            sleep_seconds = max(5, reset - time.time() + 2)
                            print(f"token#{token_no} rate limit, sleep {sleep_seconds:.1f}s", flush=True)
                        else:
                            secondary_rate_limit_errors += 1
                            if secondary_rate_limit_errors > MAX_SECONDARY_RATE_LIMIT_RETRIES:
                                break
                            sleep_seconds = min(15 * 60, 60 * 2 ** (secondary_rate_limit_errors - 1))
                            print(f"token#{token_no} secondary rate limit, retry {secondary_rate_limit_errors}, sleep {sleep_seconds}s: {repo['repo_full_name']}", flush=True)
                        sleep_or_stop(sleep_seconds)
                        continue

                    if status_code >= 500:
                        server_errors += 1
                        if server_errors > MAX_SERVER_ERROR_RETRIES:
                            break
                        sleep_seconds = min(30, 2**server_errors)
                        print(f"token#{token_no} server {status_code}, retry {server_errors}, sleep {sleep_seconds}s: {repo['repo_full_name']}", flush=True)
                        sleep_or_stop(sleep_seconds)
                        continue

                    break

                api_cache_id = None
                failure = None if status_code == 200 else failure_status(status_code, headers, data)
                if should_cache(status_code, headers, data):
                    api_cache_id = store.write_api_cache(key, endpoint, params, response, data, token_no)
                sleep_for_primary_rate_limit_if_needed(response)
                return PageResult(
                    status_code=status_code,
                    data=data,
                    api_cache_id=api_cache_id,
                    from_cache=False,
                    next_page=parse_next_page(headers.get("link")) if status_code == 200 else None,
                    failure_status=failure,
                )

            def fetch_listing(
                repo: dict[str, Any],
                kind: str,
                subject: str,
                endpoint: str,
                base_params: dict[str, Any],
                client: httpx.Client,
                counters: dict[str, int],
            ) -> list[Any]:
                # Walks every page of one listing. Returns the concatenated items of 200 pages;
                # a terminal failure (404/409/451/blocked) is cached and recorded, then the listing ends.
                # A non-cacheable failure raises so the repo goes back to retryable_error.
                items: list[Any] = []
                page = 1
                while page is not None:
                    params = dict(base_params, per_page=GITHUB_PER_PAGE_MAX, page=page)
                    result = get_page(repo, endpoint, params, client)
                    counters["requests"] += int(not result.from_cache)
                    if result.status_code != 200:
                        if result.api_cache_id is None:
                            raise RuntimeError(
                                f"{kind} {subject!r} page {page}: HTTP {result.status_code} {github_message_from_data(result.data)}"
                            )
                        store.save_listing_page(repo, kind, subject, page, result)
                        store.write_error(repo, token_no, endpoint, result.status_code, result.failure_status or "error", github_message_from_data(result.data))
                        counters["terminal_failures"] += 1
                        return items
                    if not isinstance(result.data, list):
                        raise TypeError(f"{kind} {subject!r} page {page}: response is not a list")
                    store.save_listing_page(repo, kind, subject, page, result)
                    items.extend(result.data)
                    counters["pages"] += 1
                    page = result.next_page
                return items

            def fetch_repo(repo: dict[str, Any], client: httpx.Client) -> dict[str, int]:
                counters = {"requests": 0, "pages": 0, "terminal_failures": 0, "commits": 0, "pulls": 0, "pull_files": 0, "issues": 0}
                repo_id = repo["repo_id"]

                dirs = project_dirs.get(repo_id) or [""]
                for project_dir in dirs:
                    params = {"path": project_dir} if project_dir else {}
                    commits = fetch_listing(repo, "commits", project_dir, COMMITS_ENDPOINT_TEMPLATE.format(repo_id=repo_id), params, client, counters)
                    counters["commits"] += len(commits)

                pulls = fetch_listing(
                    repo, "pulls", "", PULLS_ENDPOINT_TEMPLATE.format(repo_id=repo_id),
                    {"state": "all", "sort": "created", "direction": "asc"}, client, counters,
                )
                counters["pulls"] += len(pulls)
                for pull in pulls:
                    number = int(pull["number"])
                    files = fetch_listing(
                        repo, "pull_files", str(number), PULL_FILES_ENDPOINT_TEMPLATE.format(repo_id=repo_id, number=number),
                        {}, client, counters,
                    )
                    counters["pull_files"] += len(files)

                issues = fetch_listing(
                    repo, "issues", "", ISSUES_ENDPOINT_TEMPLATE.format(repo_id=repo_id),
                    {"state": "all", "sort": "created", "direction": "asc"}, client, counters,
                )
                counters["issues"] += len(issues)
                return counters

            with httpx.Client(base_url=GITHUB_API_BASE, headers=github_headers(token), timeout=60, follow_redirects=True) as client:
                while not stop_event.is_set():
                    repo = work_queue.get()
                    if repo is None:
                        return
                    try:
                        counters = fetch_repo(repo, client)
                    except StopRequested:
                        raise
                    except (RuntimeError, TypeError) as exc:
                        store.mark_repo_status(repo, "retryable_error", str(exc), 0, token_no, attempted=True)
                        store.write_error(repo, token_no, "", None, type(exc).__name__, str(exc))
                        print(f"token#{token_no} retryable_error {repo['repo_full_name']}: {str(exc)[:200]}", flush=True)
                        continue
                    message = (
                        f"commits={counters['commits']} pulls={counters['pulls']} pull_files={counters['pull_files']} "
                        f"issues={counters['issues']} pages={counters['pages']} terminal_failures={counters['terminal_failures']}"
                    )
                    store.mark_repo_status(repo, "fetched", message, counters["requests"], token_no, attempted=counters["requests"] > 0)
                    print(f"token#{token_no} fetched p{repo['priority']} {repo['repo_full_name']} {message} requests={counters['requests']}", flush=True)

        print(f"start todo_repos={len(todo_repos)} tokens={len(ready_tokens)} db={DB_PATH}", flush=True)

        if ready_tokens:
            with ThreadPoolExecutor(max_workers=len(ready_tokens)) as pool:
                futures = [pool.submit(collect_with_token, token_no, token) for token_no, token in ready_tokens]
                done, pending = wait(futures, return_when=FIRST_EXCEPTION)
                failed = [future for future in done if future.exception() is not None]
                if failed:
                    stop_event.set()
                    for future in pending:
                        future.cancel()
                    failed[0].result()
                stop_event.set()

        summary = {
            "schema_version": SCHEMA_VERSION,
            "todo_repo_count_at_start": len(todo_repos),
            "token_count": token_count,
            "ready_token_count": len(ready_tokens),
            "db_path": str(DB_PATH),
        }
        summary.update(sync_summary)
        summary.update(store.status_summary())
        return summary


def main() -> None:
    summary = fetch_github_repo_history()
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
