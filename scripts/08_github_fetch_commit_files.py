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


# GitHub API facts: GET /repositories/{id}/commits/{sha} returns the commit plus its changed files
# (with patch text). Files are paginated with page/per_page like list endpoints.
GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_RATE_LIMIT_ENDPOINT = "/rate_limit"
GITHUB_ACCEPT_HEADER = "application/vnd.github+json"
GITHUB_PER_PAGE_MAX = 100
COMMIT_ENDPOINT_TEMPLATE = "/repositories/{repo_id}/commits/{sha}"

# Collection policy: every commit that stage 06 listed for a qualified project dir (commit_touches joined
# with qualified_projects), de-duplicated by (repo, sha). ONLY_EXPLAINED_COMMITS narrows that to commits
# whose message has a body or an #N reference.
ONLY_EXPLAINED_COMMITS = False
REQUEST_INTERVAL_SECONDS = 0.75
MAX_SERVER_ERROR_RETRIES = 4
# Observed: a short local network outage (DNS ConnectError) killed a 10-hour run after 4 retries.
# Transport errors now back off up to 60s and retry for ~30 minutes before giving up.
MAX_TRANSPORT_RETRIES = 30
MAX_TRANSPORT_BACKOFF_SECONDS = 60
MAX_SECONDARY_RATE_LIMIT_RETRIES = 6
PRIMARY_RATE_LIMIT_SAFETY_REMAINING = 25

# Local state: stage 08 caches raw commit responses and an index page per (repo, sha, page).
# Upstream DBs are read-only: filter (project dirs) and stage 07 (commit_touches, commits).
FILTER_DB_PATH = Path("data/cache/filter_kicad_projects/state.sqlite")
EVENTS_DB_PATH = Path("data/cache/improvement_events/state.sqlite")
CACHE_DIR = Path("data/cache/github_commit_files")
DB_PATH = CACHE_DIR / "state.sqlite"
SCHEMA_VERSION = 1

# Terminal failures observed on stage 06, same family of endpoints:
CACHEABLE_FAILURE_STATUSES = {
    "not_found",
    "empty_repo",
    "unavailable_legal",
    "access_blocked",
    "diff_too_large",
    "diff_unavailable",
}


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
        "User-Agent": "pcb-project-scout-commit-files-cache",
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
    if status_code == 422 and "diff is taking too long" in message:
        return "diff_too_large"
    if status_code == 422 and "problem generating this diff" in message:
        return "diff_unavailable"
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

            -- api_cache: raw commit responses (files with patch). 200 plus terminal failures.
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

            -- commit_pages: index of cached pages per (repo, sha); page 1 carries the commit, every page
            -- carries a slice of files.
            create table if not exists commit_pages (
              repo_id integer not null,
              sha text not null,
              page integer not null,
              api_cache_id integer not null,
              status_code integer not null,
              file_count integer not null,
              next_page integer,
              fetched_at text not null,
              primary key (repo_id, sha, page),
              foreign key (api_cache_id) references api_cache(id)
            );

            -- commit_status: resumable per-commit state; only pending/retryable_error are retried.
            create table if not exists commit_status (
              repo_id integer not null,
              sha text not null,
              status text not null,
              message text not null,
              attempt_count integer not null,
              token_no integer,
              updated_at text not null,
              primary key (repo_id, sha)
            );

            create table if not exists errors (
              id integer primary key autoincrement,
              repo_id integer,
              sha text,
              token_no integer,
              endpoint text not null,
              status_code integer,
              error text not null,
              message text not null,
              seen_at text not null
            );

            create index if not exists idx_commit_status_status on commit_status(status);
            """
        )
        db.executemany(
            "insert or replace into meta (key, value) values (?, ?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("github_api_version", GITHUB_API_VERSION),
                ("filter_db", str(FILTER_DB_PATH)),
                ("events_db", str(EVENTS_DB_PATH)),
                ("commit_endpoint", COMMIT_ENDPOINT_TEMPLATE),
                ("only_explained_commits", str(int(ONLY_EXPLAINED_COMMITS))),
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

    def write_error(self, repo_id: int | None, sha: str | None, token_no: int | None, endpoint: str, status_code: int | None, error: str, message: str) -> None:
        with self.db_lock:
            with self.db:
                self.db.execute(
                    "insert into errors (repo_id, sha, token_no, endpoint, status_code, error, message, seen_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
                    (repo_id, sha, token_no, endpoint, status_code, error, message[:1000], now_utc()),
                )

    def write_api_cache(self, key: str, endpoint: str, params: dict[str, Any], response: httpx.Response, data: Any, token_no: int) -> int:
        with self.db_lock:
            with self.db:
                self.db.execute(
                    """
                    insert into api_cache (cache_key, endpoint, params_json, status_code, response_json, headers_json, token_no, fetched_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(cache_key) do update set
                      status_code = excluded.status_code, response_json = excluded.response_json,
                      headers_json = excluded.headers_json, token_no = excluded.token_no, fetched_at = excluded.fetched_at
                    """,
                    (key, endpoint, canonical_json(params), response.status_code, response_json_text(response, data), json.dumps(dict(response.headers), ensure_ascii=False), token_no, now_utc()),
                )
                row = self.db.execute("select id from api_cache where cache_key = ?", (key,)).fetchone()
        if row is None:
            raise RuntimeError(f"failed to write api_cache for {key}")
        return int(row["id"])

    def cached_page(self, key: str) -> PageResult | None:
        with self.db_lock:
            row = self.db.execute("select id, status_code, response_json, headers_json from api_cache where cache_key = ?", (key,)).fetchone()
        if row is None:
            return None
        data = json.loads(row["response_json"])
        headers = json.loads(row["headers_json"])
        status_code = int(row["status_code"])
        if not should_cache(status_code, headers, data):
            return None
        return PageResult(
            status_code=status_code, data=data, api_cache_id=int(row["id"]), from_cache=True,
            next_page=parse_next_page(headers.get("link")) if status_code == 200 else None,
            failure_status=None if status_code == 200 else failure_status(status_code, headers, data),
        )

    def save_commit_page(self, repo_id: int, sha: str, page: int, result: PageResult) -> None:
        files = result.data.get("files") if isinstance(result.data, dict) else None
        with self.db_lock:
            with self.db:
                self.db.execute(
                    "insert or replace into commit_pages (repo_id, sha, page, api_cache_id, status_code, file_count, next_page, fetched_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
                    (repo_id, sha, page, result.api_cache_id, result.status_code, len(files) if isinstance(files, list) else 0, result.next_page, now_utc()),
                )

    def mark_commit_status(self, repo_id: int, sha: str, status: str, message: str, token_no: int | None, attempted: bool) -> None:
        with self.db_lock:
            with self.db:
                old = self.db.execute("select attempt_count from commit_status where repo_id = ? and sha = ?", (repo_id, sha)).fetchone()
                attempt_count = int(attempted) if old is None else int(old["attempt_count"]) + int(attempted)
                self.db.execute(
                    "insert or replace into commit_status (repo_id, sha, status, message, attempt_count, token_no, updated_at) values (?, ?, ?, ?, ?, ?, ?)",
                    (repo_id, sha, status, message[:1000], attempt_count, token_no, now_utc()),
                )

    def sync_targets(self) -> dict[str, Any]:
        synced_at = now_utc()
        with closing(open_ro(FILTER_DB_PATH)) as filter_db:
            qualified: dict[int, set[str]] = {}
            for row in filter_db.execute("select repo_id, project_dir from qualified_projects"):
                qualified.setdefault(int(row["repo_id"]), set()).add(str(row["project_dir"]))
        targets: set[tuple[int, str]] = set()
        with closing(open_ro(EVENTS_DB_PATH)) as events_db:
            for row in events_db.execute("select repo_id, sha, project_dir from commit_touches"):
                repo_id = int(row["repo_id"])
                if row["project_dir"] in qualified.get(repo_id, set()):
                    targets.add((repo_id, str(row["sha"])))
            explained_skipped = 0
            if ONLY_EXPLAINED_COMMITS:
                keep: set[tuple[int, str]] = set()
                for row in events_db.execute("select repo_id, sha, message from commits"):
                    key = (int(row["repo_id"]), str(row["sha"]))
                    if key not in targets:
                        continue
                    message = row["message"] or ""
                    if "\n" in message.strip() or "#" in message:
                        keep.add(key)
                explained_skipped = len(targets) - len(keep)
                targets = keep
        with self.db_lock:
            with self.db:
                self.db.executemany(
                    "insert or ignore into commit_status (repo_id, sha, status, message, attempt_count, token_no, updated_at) values (?, ?, 'pending', '', 0, null, ?)",
                    [(repo_id, sha, synced_at) for repo_id, sha in sorted(targets)],
                )
                self.db.execute("insert or replace into meta (key, value) values ('last_targets_synced_at', ?)", (synced_at,))
        return {"target_commit_count": len(targets), "explained_filter_skipped": explained_skipped}

    def load_todo(self) -> list[tuple[int, str]]:
        with self.db_lock:
            return [(int(r["repo_id"]), str(r["sha"])) for r in self.db.execute("select repo_id, sha from commit_status where status in ('pending', 'retryable_error') order by repo_id, sha")]

    def status_summary(self) -> dict[str, Any]:
        with self.db_lock:
            status_counts = {r["status"]: r["count"] for r in self.db.execute("select status, count(*) as count from commit_status group by status order by status")}
            pages = self.db.execute("select count(*), sum(file_count) from commit_pages where status_code = 200").fetchone()
            api_cache_count = self.db.execute("select count(*) from api_cache").fetchone()[0]
            error_count = self.db.execute("select count(*) from errors").fetchone()[0]
        return {"api_cache_count": api_cache_count, "commit_pages_200": pages[0], "files_indexed": pages[1], "commit_status_counts": status_counts, "error_count": error_count}


def check_tokens(tokens: list[str], store: Store) -> list[tuple[int, str]]:
    ready_tokens = []
    for token_no, token in enumerate(tokens, start=1):
        with httpx.Client(base_url=GITHUB_API_BASE, headers=github_headers(token), timeout=30) as client:
            response = client.get(GITHUB_RATE_LIMIT_ENDPOINT)
        data = response_data(response)
        if response.status_code >= 400:
            message = github_message_from_data(data)
            store.write_error(None, None, token_no, GITHUB_RATE_LIMIT_ENDPOINT, response.status_code, "StartupCheckError", message)
            print(f"token#{token_no} skipped startup HTTP {response.status_code}: {message[:160]}", flush=True)
            continue
        core = data.get("resources", {}).get("core", {})
        print(f"token#{token_no} ready core_remaining={core.get('remaining', '?')}/{core.get('limit', '?')}", flush=True)
        ready_tokens.append((token_no, token))
    if not ready_tokens:
        raise RuntimeError("no usable GitHub tokens after startup check")
    return ready_tokens


def fetch_github_commit_files() -> dict[str, Any]:
    with Store() as store:
        sync_summary = store.sync_targets()
        todo = store.load_todo()
        token_count = 0
        ready_tokens: list[tuple[int, str]] = []
        if todo:
            tokens = load_tokens()
            token_count = len(tokens)
            ready_tokens = check_tokens(tokens, store)

        work_queue: queue.Queue[tuple[int, str] | None] = queue.Queue()
        for item in todo:
            work_queue.put(item)
        for _ in ready_tokens:
            work_queue.put(None)
        stop_event = threading.Event()
        done_counter = {"n": 0}
        counter_lock = threading.Lock()

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
                print(f"token#{token_no} low core rate limit remaining={remaining}, sleep {sleep_seconds:.1f}s", flush=True)
                sleep_or_stop(sleep_seconds)

            def get_page(repo_id: int, sha: str, endpoint: str, params: dict[str, Any], client: httpx.Client) -> PageResult:
                key = cache_key(endpoint, params)
                cached = store.cached_page(key)
                if cached is not None:
                    return cached
                server_errors = 0
                transport_errors = 0
                secondary_rate_limit_errors = 0
                while True:
                    wait_before_request()
                    try:
                        response = client.get(endpoint, params=params)
                    except httpx.TransportError as exc:
                        transport_errors += 1
                        if transport_errors <= MAX_TRANSPORT_RETRIES:
                            sleep_seconds = min(MAX_TRANSPORT_BACKOFF_SECONDS, 2**transport_errors)
                            print(f"token#{token_no} transport {type(exc).__name__}, retry {transport_errors}, sleep {sleep_seconds}s", flush=True)
                            sleep_or_stop(sleep_seconds)
                            continue
                        raise RuntimeError(f"request failed after {MAX_TRANSPORT_RETRIES} retries: {endpoint}: {type(exc).__name__}: {exc}") from exc

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
                        print(f"token#{token_no} retry-after, retry {secondary_rate_limit_errors}, sleep {sleep_seconds}s", flush=True)
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
                            print(f"token#{token_no} secondary rate limit, retry {secondary_rate_limit_errors}, sleep {sleep_seconds}s", flush=True)
                        sleep_or_stop(sleep_seconds)
                        continue
                    if status_code >= 500:
                        server_errors += 1
                        if server_errors > MAX_SERVER_ERROR_RETRIES:
                            break
                        sleep_seconds = min(30, 2**server_errors)
                        print(f"token#{token_no} server {status_code}, retry {server_errors}, sleep {sleep_seconds}s", flush=True)
                        sleep_or_stop(sleep_seconds)
                        continue
                    break

                api_cache_id = None
                failure = None if status_code == 200 else failure_status(status_code, headers, data)
                if should_cache(status_code, headers, data):
                    api_cache_id = store.write_api_cache(key, endpoint, params, response, data, token_no)
                sleep_for_primary_rate_limit_if_needed(response)
                return PageResult(status_code=status_code, data=data, api_cache_id=api_cache_id, from_cache=False, next_page=parse_next_page(headers.get("link")) if status_code == 200 else None, failure_status=failure)

            def fetch_commit(repo_id: int, sha: str, client: httpx.Client) -> tuple[str, str, int]:
                endpoint = COMMIT_ENDPOINT_TEMPLATE.format(repo_id=repo_id, sha=sha)
                page: int | None = 1
                requests = 0
                files = 0
                while page is not None:
                    result = get_page(repo_id, sha, endpoint, {"per_page": GITHUB_PER_PAGE_MAX, "page": page}, client)
                    requests += int(not result.from_cache)
                    if result.status_code != 200:
                        if result.api_cache_id is None:
                            return "retryable_error", f"page {page}: HTTP {result.status_code} {github_message_from_data(result.data)}", requests
                        store.save_commit_page(repo_id, sha, page, result)
                        store.write_error(repo_id, sha, token_no, endpoint, result.status_code, result.failure_status or "error", github_message_from_data(result.data))
                        return result.failure_status or "error", f"page {page}: HTTP {result.status_code}", requests
                    if not isinstance(result.data, dict):
                        raise TypeError(f"{endpoint} page {page}: response is not an object")
                    store.save_commit_page(repo_id, sha, page, result)
                    files += len(result.data.get("files") or [])
                    page = result.next_page
                return "fetched", f"files={files}", requests

            with httpx.Client(base_url=GITHUB_API_BASE, headers=github_headers(token), timeout=60, follow_redirects=True) as client:
                while not stop_event.is_set():
                    item = work_queue.get()
                    if item is None:
                        return
                    repo_id, sha = item
                    status, message, requests = fetch_commit(repo_id, sha, client)
                    store.mark_commit_status(repo_id, sha, status, message, token_no, attempted=requests > 0)
                    with counter_lock:
                        done_counter["n"] += 1
                        n = done_counter["n"]
                    if status != "fetched" or n % 500 == 0:
                        print(f"token#{token_no} {status} {repo_id}@{sha[:10]} {message} done={n}/{len(todo)}", flush=True)

        print(f"start todo_commits={len(todo)} tokens={len(ready_tokens)} db={DB_PATH}", flush=True)

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

        summary = {"schema_version": SCHEMA_VERSION, "todo_commit_count_at_start": len(todo), "token_count": token_count, "ready_token_count": len(ready_tokens), "db_path": str(DB_PATH)}
        summary.update(sync_summary)
        summary.update(store.status_summary())
        return summary


def main() -> None:
    summary = fetch_github_commit_files()
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
