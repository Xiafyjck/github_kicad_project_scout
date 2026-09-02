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


# GitHub API facts: same pinned version and auth scheme as the code search cache script.
GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_RATE_LIMIT_ENDPOINT = "/rate_limit"
GITHUB_ACCEPT_HEADER = "application/vnd.github+json"
GITHUB_TREE_ENDPOINT_TEMPLATE = "/repositories/{repo_id}/git/trees/HEAD"

# Collection policy: repo trees use the core rate limit. The interval lowers secondary rate limit risk.
TREE_REQUEST_INTERVAL_SECONDS = 0.8
MAX_SERVER_ERROR_RETRIES = 4
MAX_SECONDARY_RATE_LIMIT_RETRIES = 6
PRIMARY_RATE_LIMIT_SAFETY_REMAINING = 25

# Local state: stage 02 only caches raw tree responses, no business logic. Candidates come from the stage 01 merge DB (read-only).
CANDIDATES_DB_PATH = Path("data/cache/github_candidates/state.sqlite")
CACHE_DIR = Path("data/cache/github_trees")
DB_PATH = CACHE_DIR / "state.sqlite"
SCHEMA_VERSION = 1

TREE_CACHEABLE_FAILURE_STATUSES = {
    "not_found",
    "empty_repo",
    "unavailable_legal",
    "access_blocked",
}
TREE_STATUS_ERROR_LABELS = {
    "not_found": "NotFound",
    "empty_repo": "Conflict",
    "unavailable_legal": "UnavailableForLegalReasons",
    "access_blocked": "AccessBlocked",
    "retryable_error": "RetryableHTTPError",
    "error": "HTTPError",
}


@dataclass(frozen=True)
class TreeResult:
    status_code: int
    data: Any
    api_cache_id: int | None
    from_cache: bool
    failure_status: str | None = None


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
        "User-Agent": "pcb-project-scout-tree-cache",
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


def tree_endpoint(repo_id: int) -> str:
    return GITHUB_TREE_ENDPOINT_TEMPLATE.format(repo_id=repo_id)


def tree_cache_key(endpoint: str, params: dict[str, Any]) -> str:
    return f"tree:{endpoint}:{canonical_json(params)}"


def is_rate_limit_response(status_code: int, headers: dict[str, str], message: str) -> bool:
    if status_code not in {403, 429}:
        return False
    if headers.get("x-ratelimit-remaining") == "0":
        return True
    message_lower = message.lower()
    return "rate limit" in message_lower or "abuse detection" in message_lower


def tree_failure_status(status_code: int, headers: dict[str, str], data: Any) -> str:
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


def should_cache_tree_response(status_code: int, headers: dict[str, str], data: Any) -> bool:
    if status_code == 200:
        return True
    return tree_failure_status(status_code, headers, data) in TREE_CACHEABLE_FAILURE_STATUSES


def tree_error_label(status: str) -> str:
    return TREE_STATUS_ERROR_LABELS.get(status, "HTTPError")


def setup_db() -> sqlite3.Connection:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    with db:
        db.execute("pragma journal_mode = wal")
        db.execute("pragma synchronous = normal")
        db.executescript(
            """
            -- meta: script/schema/API facts needed to interpret the cache later.
            create table if not exists meta (
              key text primary key,
              value text not null
            );

            -- api_cache: reusable GitHub tree responses.
            -- Tree endpoints cache only 200 plus terminal failures: 404, 409, 451, and blocked/DMCA 403.
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

            -- repo_trees: one recursive HEAD tree per repo when GitHub returns 200.
            create table if not exists repo_trees (
              repo_id integer primary key,
              api_cache_id integer not null,
              tree_sha text,
              truncated integer not null,
              tree_entry_count integer not null,
              fetched_at text not null,
              foreign key (api_cache_id) references api_cache(id)
            );

            -- repo_status: resumable per-repo state; only pending/retryable_error are retried.
            -- fetched = 200 and complete; truncated = 200 but GitHub cut the recursive listing.
            create table if not exists repo_status (
              repo_id integer primary key,
              repo_full_name text not null,
              status text not null,
              api_cache_id integer,
              http_status_code integer,
              truncated integer,
              message text not null,
              attempt_count integer not null,
              token_no integer,
              updated_at text not null,
              foreign key (api_cache_id) references api_cache(id)
            );

            -- errors: API failures kept for manual audit and rerun decisions.
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
              on repo_status(status);
            """
        )
        db.executemany(
            "insert or replace into meta (key, value) values (?, ?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("github_api_version", GITHUB_API_VERSION),
                ("candidates_db", str(CANDIDATES_DB_PATH)),
                ("tree_endpoint", GITHUB_TREE_ENDPOINT_TEMPLATE),
                ("tree_recursive", "1"),
            ],
        )
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
        cache_key: str,
        endpoint: str,
        params: dict[str, Any],
        response: httpx.Response,
        data: Any,
        token_no: int,
    ) -> int:
        params_json = canonical_json(params)
        headers_json = json.dumps(dict(response.headers), ensure_ascii=False)
        fetched_at = now_utc()
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
                      endpoint = excluded.endpoint,
                      params_json = excluded.params_json,
                      status_code = excluded.status_code,
                      response_json = excluded.response_json,
                      headers_json = excluded.headers_json,
                      token_no = excluded.token_no,
                      fetched_at = excluded.fetched_at
                    """,
                    (
                        cache_key,
                        endpoint,
                        params_json,
                        response.status_code,
                        response_json_text(response, data),
                        headers_json,
                        token_no,
                        fetched_at,
                    ),
                )
                row = self.db.execute("select id from api_cache where cache_key = ?", (cache_key,)).fetchone()

        if row is None:
            raise RuntimeError(f"failed to write api_cache for {cache_key}")
        return int(row["id"])

    def cached_tree_response(self, repo: dict[str, Any]) -> TreeResult | None:
        endpoint = tree_endpoint(repo["repo_id"])
        params = {"recursive": "1"}
        cache_key = tree_cache_key(endpoint, params)
        with self.db_lock:
            row = self.db.execute(
                "select id, status_code, response_json from api_cache where cache_key = ?",
                (cache_key,),
            ).fetchone()

        if row is None:
            return None

        data = json.loads(row["response_json"])
        status_code = int(row["status_code"])
        if not should_cache_tree_response(status_code, {}, data):
            return None

        return TreeResult(
            status_code=status_code,
            data=data,
            api_cache_id=int(row["id"]),
            from_cache=True,
            failure_status=None if status_code == 200 else tree_failure_status(status_code, {}, data),
        )

    def save_repo_tree(self, repo: dict[str, Any], api_cache_id: int, data: dict[str, Any]) -> bool:
        tree = data.get("tree", [])
        if not isinstance(tree, list):
            raise ValueError("tree response JSON does not contain a list field named 'tree'")

        truncated = bool(data.get("truncated", False))
        with self.db_lock:
            with self.db:
                self.db.execute(
                    """
                    insert or replace into repo_trees (
                      repo_id, api_cache_id, tree_sha,
                      truncated, tree_entry_count, fetched_at
                    )
                    values (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        repo["repo_id"],
                        api_cache_id,
                        data.get("sha"),
                        int(truncated),
                        len(tree),
                        now_utc(),
                    ),
                )
        return truncated

    def mark_repo_status(
        self,
        repo: dict[str, Any],
        status: str,
        api_cache_id: int | None,
        http_status_code: int | None,
        truncated: bool | None,
        message: str,
        token_no: int | None,
        attempted: bool = False,
    ) -> None:
        with self.db_lock:
            with self.db:
                old = self.db.execute(
                    "select attempt_count from repo_status where repo_id = ?",
                    (repo["repo_id"],),
                ).fetchone()
                attempt_count = int(attempted) if old is None else int(old["attempt_count"]) + int(attempted)
                self.db.execute(
                    """
                    insert or replace into repo_status (
                      repo_id, repo_full_name, status, api_cache_id,
                      http_status_code, truncated, message,
                      attempt_count, token_no, updated_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        repo["repo_id"],
                        repo["repo_full_name"],
                        status,
                        api_cache_id,
                        http_status_code,
                        None if truncated is None else int(truncated),
                        message[:1000],
                        attempt_count,
                        token_no,
                        now_utc(),
                    ),
                )

    def sync_candidates(self) -> dict[str, Any]:
        synced_at = now_utc()
        with closing(sqlite3.connect(f"file:{CANDIDATES_DB_PATH}?mode=ro", uri=True, timeout=30)) as source:
            source.row_factory = sqlite3.Row
            candidates = [
                (int(row["repo_id"]), str(row["repo_full_name"]))
                for row in source.execute("select repo_id, repo_full_name from candidate_repos order by repo_id")
            ]
        with self.db_lock:
            with self.db:
                self.db.executemany(
                    """
                    insert into repo_status (
                      repo_id, repo_full_name, status, api_cache_id,
                      http_status_code, truncated, message,
                      attempt_count, token_no, updated_at
                    )
                    values (?, ?, 'pending', null, null, null, '', 0, null, ?)
                    on conflict(repo_id) do update set
                      repo_full_name = excluded.repo_full_name
                    """,
                    [(repo_id, repo_full_name, synced_at) for repo_id, repo_full_name in candidates],
                )
                self.db.execute(
                    "insert or replace into meta (key, value) values ('last_candidates_synced_at', ?)",
                    (synced_at,),
                )
        return {"candidate_repo_count": len(candidates)}

    def load_todo_repos(self) -> list[dict[str, Any]]:
        with self.db_lock:
            return [
                dict(row)
                for row in self.db.execute(
                    """
                    select repo_id, repo_full_name
                    from repo_status
                    where status in ('pending', 'retryable_error')
                    order by repo_id
                    """
                )
            ]

    def status_summary(self) -> dict[str, Any]:
        with self.db_lock:
            status_counts = {
                row["status"]: row["count"]
                for row in self.db.execute(
                    "select status, count(*) as count from repo_status group by status order by status"
                )
            }
            tree_count = self.db.execute("select count(*) from repo_trees").fetchone()[0]
            api_cache_count = self.db.execute("select count(*) from api_cache").fetchone()[0]
            truncated_count = self.db.execute("select count(*) from repo_trees where truncated = 1").fetchone()[0]
            error_count = self.db.execute("select count(*) from errors").fetchone()[0]

        return {
            "repo_tree_count": tree_count,
            "api_cache_count": api_cache_count,
            "truncated_tree_count": truncated_count,
            "repo_status_counts": status_counts,
            "error_count": error_count,
        }


def check_tokens(tokens: list[str], store: Store) -> list[tuple[int, str]]:
    startup_checks = []
    ready_tokens = []
    for token_no, token in enumerate(tokens, start=1):
        endpoint = GITHUB_RATE_LIMIT_ENDPOINT
        with httpx.Client(
            base_url=GITHUB_API_BASE,
            headers=github_headers(token),
            timeout=30,
            follow_redirects=True,
        ) as client:
            response = client.get(endpoint)

        data = response_data(response)
        if response.status_code >= 400:
            message = github_message_from_data(data)
            store.write_error(None, token_no, endpoint, response.status_code, "StartupCheckError", message)
            print(
                f"token#{token_no} skipped startup HTTP {response.status_code}: {message[:160]}",
                flush=True,
            )
            continue

        core = data.get("resources", {}).get("core", {})
        startup_check = {
            "token_no": token_no,
            "http_status_code": response.status_code,
            "core_remaining": core.get("remaining", "?"),
            "core_limit": core.get("limit", "?"),
            "core_reset": core.get("reset"),
        }
        startup_checks.append(startup_check)
        ready_tokens.append((token_no, token))
        print(
            f"token#{token_no} ready "
            f"core_remaining={startup_check['core_remaining']}/{startup_check['core_limit']}",
            flush=True,
        )

    if not ready_tokens:
        raise RuntimeError("no usable GitHub tokens after startup check")

    with store.db_lock:
        with store.db:
            store.db.executemany(
                "insert or replace into meta (key, value) values (?, ?)",
                [
                    ("startup_checks_json", json.dumps(startup_checks, ensure_ascii=False)),
                    ("startup_checked_at", now_utc()),
                ],
            )

    return ready_tokens


def fetch_github_trees() -> dict[str, Any]:
    with Store() as store:
        sync_summary = store.sync_candidates()
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
        for _token_no, _token in ready_tokens:
            work_queue.put(None)
        stop_event = threading.Event()

        class StopRequested(Exception):
            pass

        def sleep_or_stop(seconds: float) -> None:
            if stop_event.wait(seconds):
                raise StopRequested()

        def collect_with_token(token_no: int, token: str) -> None:
            last_tree_started_at = 0.0

            def wait_before_tree() -> None:
                nonlocal last_tree_started_at
                elapsed = time.monotonic() - last_tree_started_at
                if elapsed < TREE_REQUEST_INTERVAL_SECONDS:
                    sleep_or_stop(TREE_REQUEST_INTERVAL_SECONDS - elapsed)
                last_tree_started_at = time.monotonic()

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
                print(
                    f"token#{token_no} low core rate limit remaining={remaining}, "
                    f"sleep {sleep_seconds:.1f}s until {reset_utc}",
                    flush=True,
                )
                sleep_or_stop(sleep_seconds)

            def get_tree(repo: dict[str, Any], client: httpx.Client) -> TreeResult:
                cached = store.cached_tree_response(repo)
                if cached is not None:
                    return cached

                endpoint = tree_endpoint(repo["repo_id"])
                params = {"recursive": "1"}
                cache_key = tree_cache_key(endpoint, params)
                server_errors = 0
                secondary_rate_limit_errors = 0

                while True:
                    wait_before_tree()
                    try:
                        response = client.get(endpoint, params=params)
                    except httpx.TransportError as exc:
                        server_errors += 1
                        if server_errors <= MAX_SERVER_ERROR_RETRIES:
                            sleep_seconds = min(30, 2**server_errors)
                            print(
                                f"token#{token_no} transport {type(exc).__name__}, retry {server_errors}, "
                                f"sleep {sleep_seconds}s: {repo['repo_full_name']}",
                                flush=True,
                            )
                            sleep_or_stop(sleep_seconds)
                            continue
                        raise RuntimeError(
                            f"tree request failed after {MAX_SERVER_ERROR_RETRIES} retries: "
                            f"{repo['repo_full_name']} {endpoint}: {type(exc).__name__}: {exc}"
                        ) from exc

                    data = response_data(response)
                    response_headers = dict(response.headers)
                    message = github_message_from_data(data)
                    status_code = response.status_code

                    if status_code == 200:
                        break

                    if status_code in {403, 429}:
                        retry_after = response.headers.get("retry-after")
                        if retry_after:
                            secondary_rate_limit_errors += 1
                            if secondary_rate_limit_errors > MAX_SECONDARY_RATE_LIMIT_RETRIES:
                                break
                            sleep_seconds = int(float(retry_after)) + 1
                            print(
                                f"token#{token_no} retry-after, retry {secondary_rate_limit_errors}, "
                                f"sleep {sleep_seconds}s: {repo['repo_full_name']}",
                                flush=True,
                            )
                            sleep_or_stop(sleep_seconds)
                            continue

                    if is_rate_limit_response(status_code, response_headers, message):
                        if response_headers.get("x-ratelimit-remaining") == "0":
                            reset = int(response_headers.get("x-ratelimit-reset", "0") or "0")
                            sleep_seconds = max(5, reset - time.time() + 2)
                            reset_utc = datetime.fromtimestamp(reset, timezone.utc).isoformat()
                            print(
                                f"token#{token_no} rate limit, sleep {sleep_seconds:.1f}s until {reset_utc}",
                                flush=True,
                            )
                        else:
                            secondary_rate_limit_errors += 1
                            if secondary_rate_limit_errors > MAX_SECONDARY_RATE_LIMIT_RETRIES:
                                break
                            sleep_seconds = min(15 * 60, 60 * 2 ** (secondary_rate_limit_errors - 1))
                            print(
                                f"token#{token_no} secondary rate limit, retry {secondary_rate_limit_errors}, "
                                f"sleep {sleep_seconds}s: {repo['repo_full_name']}",
                                flush=True,
                            )
                        sleep_or_stop(sleep_seconds)
                        continue

                    if status_code >= 500:
                        server_errors += 1
                        if server_errors > MAX_SERVER_ERROR_RETRIES:
                            break
                        sleep_seconds = min(30, 2**server_errors)
                        print(
                            f"token#{token_no} server {status_code}, retry {server_errors}, "
                            f"sleep {sleep_seconds}s: {repo['repo_full_name']}",
                            flush=True,
                        )
                        sleep_or_stop(sleep_seconds)
                        continue

                    break

                api_cache_id = None
                failure_status = None
                if status_code != 200:
                    failure_status = tree_failure_status(status_code, response_headers, data)
                if should_cache_tree_response(status_code, response_headers, data):
                    api_cache_id = store.write_api_cache(cache_key, endpoint, params, response, data, token_no)
                sleep_for_primary_rate_limit_if_needed(response)
                return TreeResult(
                    status_code=status_code,
                    data=data,
                    api_cache_id=api_cache_id,
                    from_cache=False,
                    failure_status=failure_status,
                )

            with httpx.Client(
                base_url=GITHUB_API_BASE,
                headers=github_headers(token),
                timeout=30,
                follow_redirects=True,
            ) as client:
                while not stop_event.is_set():
                    repo = work_queue.get()
                    if repo is None:
                        return

                    result = get_tree(repo, client)
                    attempted = not result.from_cache
                    if result.status_code == 200:
                        if result.api_cache_id is None:
                            raise RuntimeError("successful tree response was not cached")
                        truncated = store.save_repo_tree(repo, result.api_cache_id, result.data)
                        status = "truncated" if truncated else "fetched"
                        message = (
                            "recursive tree response was truncated"
                            if truncated
                            else f"tree_entries={len(result.data.get('tree', []))}"
                        )
                        store.mark_repo_status(
                            repo,
                            status,
                            result.api_cache_id,
                            result.status_code,
                            truncated,
                            message,
                            token_no,
                            attempted=attempted,
                        )
                        print(f"token#{token_no} {status} {repo['repo_full_name']} {message}", flush=True)
                        continue

                    message = github_message_from_data(result.data)
                    status = result.failure_status or tree_failure_status(result.status_code, {}, result.data)
                    store.mark_repo_status(
                        repo,
                        status,
                        result.api_cache_id,
                        result.status_code,
                        None,
                        message,
                        token_no,
                        attempted=attempted,
                    )
                    store.write_error(
                        repo,
                        token_no,
                        tree_endpoint(repo["repo_id"]),
                        result.status_code,
                        tree_error_label(status),
                        message,
                    )
                    print(
                        f"token#{token_no} {status} {repo['repo_full_name']}: HTTP {result.status_code}",
                        flush=True,
                    )
                    continue

        print(
            f"start todo_repos={len(todo_repos)} tokens={len(ready_tokens)} db={DB_PATH}",
            flush=True,
        )

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
            "github_api_version": GITHUB_API_VERSION,
            "candidates_db": str(CANDIDATES_DB_PATH),
            "todo_repo_count_at_start": len(todo_repos),
            "token_count": token_count,
            "ready_token_count": len(ready_tokens),
            "db_path": str(DB_PATH),
        }
        summary.update(sync_summary)
        summary.update(store.status_summary())
        return summary


def main() -> None:
    summary = fetch_github_trees()
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
