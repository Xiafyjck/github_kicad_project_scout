from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import threading
import time
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import find_dotenv, load_dotenv


# GitHub API facts: GraphQL v4 endpoint, same auth scheme as the REST scripts.
GITHUB_API_BASE = "https://api.github.com"
GITHUB_GRAPHQL_ENDPOINT = "/graphql"
GITHUB_RATE_LIMIT_ENDPOINT = "/rate_limit"
GITHUB_ACCEPT_HEADER = "application/vnd.github+json"
GITHUB_API_VERSION = "2026-03-10"

# Collection policy: one GraphQL query batches many repos through aliases. GraphQL has its own
# 5000 points/hour budget per token. A batch of 25 repos with count-only connections costs 1 point and
# answers in ~5s; 50 per batch took ~9s and hit 502s.
BATCH_SIZE = 25
GRAPHQL_REQUEST_INTERVAL_SECONDS = 1.0
MAX_SERVER_ERROR_RETRIES = 4
MAX_SECONDARY_RATE_LIMIT_RETRIES = 6
GRAPHQL_RATE_LIMIT_SAFETY_REMAINING = 50

# Local state: stage 06 caches raw GraphQL responses and one row of stats per repo.
# Candidates come from the stage 02 merge DB (read-only).
CANDIDATES_DB_PATH = Path("data/cache/github_candidates/state.sqlite")
CACHE_DIR = Path("data/cache/github_repo_stats")
DB_PATH = CACHE_DIR / "state.sqlite"
SCHEMA_VERSION = 1

REPO_FRAGMENT = """
fragment RepoStats on Repository {
  databaseId
  nameWithOwner
  isFork
  isArchived
  isDisabled
  isEmpty
  createdAt
  pushedAt
  stargazerCount
  forkCount
  diskUsage
  hasIssuesEnabled
  parent { databaseId nameWithOwner }
  primaryLanguage { name }
  licenseInfo { spdxId }
  defaultBranchRef {
    name
    target { ... on Commit { oid committedDate history { totalCount } } }
  }
  pullRequests { totalCount }
  mergedPullRequests: pullRequests(states: MERGED) { totalCount }
  issues { totalCount }
}
"""

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
        "User-Agent": "pcb-project-scout-repo-stats-cache",
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
        if "message" in data:
            return str(data.get("message", ""))
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(error.get("message", "")) for error in errors if isinstance(error, dict))[:300]
        return ""
    return str(data)[:300]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def response_json_text(response: httpx.Response, data: Any) -> str:
    if isinstance(data, dict) and "_non_json_text" in data:
        return json.dumps(data, ensure_ascii=False)
    return response.text


def build_query(repos: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    # Aliases r0..rN each look up one repo by owner/name. Variables carry owner and name so the
    # query text only depends on the batch size; the variables carry the actual repos.
    declarations = []
    fields = []
    variables: dict[str, Any] = {}
    for index, repo in enumerate(repos):
        owner, name = repo["repo_full_name"].split("/", 1)
        declarations.append(f"$o{index}: String!, $n{index}: String!")
        fields.append(f"r{index}: repository(owner: $o{index}, name: $n{index}) {{ ...RepoStats }}")
        variables[f"o{index}"] = owner
        variables[f"n{index}"] = name
    query = (
        "query RepoStats(" + ", ".join(declarations) + ") {\n"
        "  rateLimit { cost remaining resetAt }\n"
        + "\n".join("  " + field for field in fields)
        + "\n}\n"
        + REPO_FRAGMENT
    )
    return query, variables


def graphql_cache_key(query: str, variables: dict[str, Any]) -> str:
    digest = hashlib.sha256((query + "\n" + canonical_json(variables)).encode("utf-8")).hexdigest()
    return f"graphql:{GITHUB_GRAPHQL_ENDPOINT}:{digest}"


def is_rate_limit_response(status_code: int, headers: dict[str, str], message: str) -> bool:
    if status_code not in {403, 429}:
        return False
    if headers.get("x-ratelimit-remaining") == "0":
        return True
    message_lower = message.lower()
    return "rate limit" in message_lower or "abuse detection" in message_lower


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

            -- api_cache: raw GraphQL responses, keyed by hash of query text + variables.
            -- Only HTTP 200 bodies are stored; a 200 may still carry per-alias errors (e.g. NOT_FOUND).
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

            -- repo_stats: one row per repo that GraphQL resolved. Counts are totals at fetch time.
            create table if not exists repo_stats (
              repo_id integer primary key,
              repo_full_name text not null,
              resolved_full_name text not null,
              api_cache_id integer not null,
              is_fork integer not null,
              is_archived integer not null,
              is_disabled integer not null,
              is_empty integer not null,
              parent_repo_id integer,
              parent_full_name text,
              created_at text,
              pushed_at text,
              stargazer_count integer not null,
              fork_count integer not null,
              disk_usage_kb integer,
              has_issues_enabled integer not null,
              primary_language text,
              license_spdx text,
              default_branch text,
              head_commit_sha text,
              head_committed_at text,
              commit_count integer,
              pull_request_count integer not null,
              merged_pull_request_count integer not null,
              issue_count integer not null,
              fetched_at text not null,
              foreign key (api_cache_id) references api_cache(id)
            );

            -- repo_status: resumable per-repo state; only pending/retryable_error are retried.
            -- fetched = resolved; not_found = GraphQL NOT_FOUND for the alias; id_mismatch = owner/name now
            -- points at a different databaseId than the candidate repo_id.
            create table if not exists repo_status (
              repo_id integer primary key,
              repo_full_name text not null,
              status text not null,
              api_cache_id integer,
              message text not null,
              attempt_count integer not null,
              token_no integer,
              updated_at text not null,
              foreign key (api_cache_id) references api_cache(id)
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
                ("graphql_endpoint", GITHUB_GRAPHQL_ENDPOINT),
                ("batch_size", str(BATCH_SIZE)),
                ("repo_fragment", REPO_FRAGMENT.strip()),
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
                        GITHUB_GRAPHQL_ENDPOINT,
                        status_code,
                        error,
                        message[:1000],
                        now_utc(),
                    ),
                )

    def write_api_cache(
        self,
        cache_key: str,
        query: str,
        variables: dict[str, Any],
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
                        cache_key,
                        GITHUB_GRAPHQL_ENDPOINT,
                        canonical_json({"query": query, "variables": variables}),
                        response.status_code,
                        response_json_text(response, data),
                        json.dumps(dict(response.headers), ensure_ascii=False),
                        token_no,
                        now_utc(),
                    ),
                )
                row = self.db.execute("select id from api_cache where cache_key = ?", (cache_key,)).fetchone()
        if row is None:
            raise RuntimeError(f"failed to write api_cache for {cache_key}")
        return int(row["id"])

    def cached_response(self, cache_key: str) -> tuple[int, Any] | None:
        with self.db_lock:
            row = self.db.execute(
                "select id, response_json from api_cache where cache_key = ? and status_code = 200",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        return int(row["id"]), json.loads(row["response_json"])

    def save_repo_stats(self, repo: dict[str, Any], api_cache_id: int, node: dict[str, Any]) -> None:
        parent = node.get("parent") or {}
        default_branch = node.get("defaultBranchRef") or {}
        target = default_branch.get("target") or {}
        history = target.get("history") or {}
        with self.db_lock:
            with self.db:
                self.db.execute(
                    """
                    insert or replace into repo_stats (
                      repo_id, repo_full_name, resolved_full_name, api_cache_id,
                      is_fork, is_archived, is_disabled, is_empty,
                      parent_repo_id, parent_full_name, created_at, pushed_at,
                      stargazer_count, fork_count, disk_usage_kb, has_issues_enabled,
                      primary_language, license_spdx, default_branch,
                      head_commit_sha, head_committed_at, commit_count,
                      pull_request_count, merged_pull_request_count, issue_count, fetched_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        repo["repo_id"],
                        repo["repo_full_name"],
                        node["nameWithOwner"],
                        api_cache_id,
                        int(bool(node["isFork"])),
                        int(bool(node["isArchived"])),
                        int(bool(node["isDisabled"])),
                        int(bool(node["isEmpty"])),
                        parent.get("databaseId"),
                        parent.get("nameWithOwner"),
                        node.get("createdAt"),
                        node.get("pushedAt"),
                        int(node["stargazerCount"]),
                        int(node["forkCount"]),
                        node.get("diskUsage"),
                        int(bool(node["hasIssuesEnabled"])),
                        (node.get("primaryLanguage") or {}).get("name"),
                        (node.get("licenseInfo") or {}).get("spdxId"),
                        default_branch.get("name"),
                        target.get("oid"),
                        target.get("committedDate"),
                        history.get("totalCount"),
                        int(node["pullRequests"]["totalCount"]),
                        int(node["mergedPullRequests"]["totalCount"]),
                        int(node["issues"]["totalCount"]),
                        now_utc(),
                    ),
                )

    def mark_repo_status(
        self,
        repo: dict[str, Any],
        status: str,
        api_cache_id: int | None,
        message: str,
        token_no: int | None,
        attempted: bool,
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
                      message, attempt_count, token_no, updated_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        repo["repo_id"],
                        repo["repo_full_name"],
                        status,
                        api_cache_id,
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
                      message, attempt_count, token_no, updated_at
                    )
                    values (?, ?, 'pending', null, '', 0, null, ?)
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
            stats_count = self.db.execute("select count(*) from repo_stats").fetchone()[0]
            api_cache_count = self.db.execute("select count(*) from api_cache").fetchone()[0]
            error_count = self.db.execute("select count(*) from errors").fetchone()[0]
        return {
            "repo_stats_count": stats_count,
            "api_cache_count": api_cache_count,
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
            store.write_error(None, token_no, response.status_code, "StartupCheckError", message)
            print(f"token#{token_no} skipped startup HTTP {response.status_code}: {message[:160]}", flush=True)
            continue
        graphql = data.get("resources", {}).get("graphql", {})
        print(
            f"token#{token_no} ready graphql_remaining={graphql.get('remaining', '?')}/{graphql.get('limit', '?')}",
            flush=True,
        )
        ready_tokens.append((token_no, token))
    if not ready_tokens:
        raise RuntimeError("no usable GitHub tokens after startup check")
    return ready_tokens


def alias_errors(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    # GraphQL reports per-alias failures in a top-level errors list whose path starts with the alias.
    by_alias: dict[str, list[dict[str, Any]]] = {}
    for error in data.get("errors") or []:
        if not isinstance(error, dict):
            continue
        path = error.get("path") or []
        if path and isinstance(path[0], str):
            by_alias.setdefault(path[0], []).append(error)
        else:
            by_alias.setdefault("", []).append(error)
    return by_alias


def apply_batch_result(
    store: Store,
    batch: list[dict[str, Any]],
    api_cache_id: int,
    data: dict[str, Any],
    token_no: int,
    attempted: bool,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    nodes = data.get("data") or {}
    errors = alias_errors(data)
    global_errors = errors.get("", [])
    if global_errors and not nodes:
        raise RuntimeError(f"GraphQL query rejected: {github_message_from_data(data)}")

    for index, repo in enumerate(batch):
        alias = f"r{index}"
        node = nodes.get(alias)
        repo_errors = errors.get(alias, [])
        if node is None:
            types = {str(error.get("type", "")) for error in repo_errors}
            message = "; ".join(str(error.get("message", "")) for error in repo_errors)[:300]
            if types == {"NOT_FOUND"}:
                status = "not_found"
            else:
                status = "retryable_error"
            store.mark_repo_status(repo, status, api_cache_id, message or "alias returned null", token_no, attempted)
            store.write_error(repo, token_no, 200, "GraphQLAliasError", message or "alias returned null")
            counts[status] = counts.get(status, 0) + 1
            continue

        if int(node["databaseId"]) != int(repo["repo_id"]):
            message = f"owner/name now resolves to databaseId={node['databaseId']} ({node['nameWithOwner']})"
            store.mark_repo_status(repo, "id_mismatch", api_cache_id, message, token_no, attempted)
            store.write_error(repo, token_no, 200, "IdMismatch", message)
            counts["id_mismatch"] = counts.get("id_mismatch", 0) + 1
            continue

        store.save_repo_stats(repo, api_cache_id, node)
        store.mark_repo_status(
            repo,
            "fetched",
            api_cache_id,
            f"commits={((node.get('defaultBranchRef') or {}).get('target') or {}).get('history', {}).get('totalCount')} "
            f"prs={node['pullRequests']['totalCount']} merged={node['mergedPullRequests']['totalCount']} "
            f"issues={node['issues']['totalCount']}",
            token_no,
            attempted,
        )
        counts["fetched"] = counts.get("fetched", 0) + 1
    return counts


def fetch_github_repo_stats() -> dict[str, Any]:
    with Store() as store:
        sync_summary = store.sync_candidates()
        todo_repos = store.load_todo_repos()
        batches = [todo_repos[i : i + BATCH_SIZE] for i in range(0, len(todo_repos), BATCH_SIZE)]
        token_count = 0
        ready_tokens: list[tuple[int, str]] = []
        if batches:
            tokens = load_tokens()
            token_count = len(tokens)
            ready_tokens = check_tokens(tokens, store)

        work_queue: queue.Queue[list[dict[str, Any]] | None] = queue.Queue()
        for batch in batches:
            work_queue.put(batch)
        for _ in ready_tokens:
            work_queue.put(None)
        stop_event = threading.Event()

        class StopRequested(Exception):
            pass

        def sleep_or_stop(seconds: float) -> None:
            if stop_event.wait(seconds):
                raise StopRequested()

        def collect_with_token(token_no: int, token: str) -> None:
            last_started_at = 0.0

            def wait_before_request() -> None:
                nonlocal last_started_at
                elapsed = time.monotonic() - last_started_at
                if elapsed < GRAPHQL_REQUEST_INTERVAL_SECONDS:
                    sleep_or_stop(GRAPHQL_REQUEST_INTERVAL_SECONDS - elapsed)
                last_started_at = time.monotonic()

            def sleep_for_rate_limit_if_needed(data: dict[str, Any]) -> None:
                rate = (data.get("data") or {}).get("rateLimit") or {}
                remaining = rate.get("remaining")
                reset_at = rate.get("resetAt")
                if remaining is None or reset_at is None or int(remaining) > GRAPHQL_RATE_LIMIT_SAFETY_REMAINING:
                    return
                reset = datetime.fromisoformat(reset_at.replace("Z", "+00:00")).timestamp()
                sleep_seconds = max(5, reset - time.time() + 2)
                print(f"token#{token_no} low graphql remaining={remaining}, sleep {sleep_seconds:.1f}s", flush=True)
                sleep_or_stop(sleep_seconds)

            def run_query(batch: list[dict[str, Any]], client: httpx.Client) -> tuple[int, dict[str, Any], bool]:
                query, variables = build_query(batch)
                cache_key = graphql_cache_key(query, variables)
                cached = store.cached_response(cache_key)
                if cached is not None:
                    return cached[0], cached[1], True

                server_errors = 0
                secondary_rate_limit_errors = 0
                while True:
                    wait_before_request()
                    try:
                        response = client.post(GITHUB_GRAPHQL_ENDPOINT, json={"query": query, "variables": variables})
                    except httpx.TransportError as exc:
                        server_errors += 1
                        if server_errors <= MAX_SERVER_ERROR_RETRIES:
                            sleep_seconds = min(30, 2**server_errors)
                            print(
                                f"token#{token_no} transport {type(exc).__name__}, retry {server_errors}, sleep {sleep_seconds}s",
                                flush=True,
                            )
                            sleep_or_stop(sleep_seconds)
                            continue
                        raise RuntimeError(
                            f"graphql request failed after {MAX_SERVER_ERROR_RETRIES} retries: {type(exc).__name__}: {exc}"
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
                            print(
                                f"token#{token_no} secondary rate limit, retry {secondary_rate_limit_errors}, sleep {sleep_seconds}s",
                                flush=True,
                            )
                        sleep_or_stop(sleep_seconds)
                        continue

                    if status_code >= 500 or status_code == 502:
                        server_errors += 1
                        if server_errors > MAX_SERVER_ERROR_RETRIES:
                            break
                        sleep_seconds = min(30, 2**server_errors)
                        print(f"token#{token_no} server {status_code}, retry {server_errors}, sleep {sleep_seconds}s", flush=True)
                        sleep_or_stop(sleep_seconds)
                        continue

                    break

                if status_code != 200:
                    raise RuntimeError(f"graphql HTTP {status_code}: {message}")

                api_cache_id = store.write_api_cache(cache_key, query, variables, response, data, token_no)
                sleep_for_rate_limit_if_needed(data)
                return api_cache_id, data, False

            with httpx.Client(
                base_url=GITHUB_API_BASE,
                headers=github_headers(token),
                timeout=60,
                follow_redirects=True,
            ) as client:
                while not stop_event.is_set():
                    batch = work_queue.get()
                    if batch is None:
                        return
                    api_cache_id, data, from_cache = run_query(batch, client)
                    counts = apply_batch_result(store, batch, api_cache_id, data, token_no, attempted=not from_cache)
                    first, last = batch[0]["repo_id"], batch[-1]["repo_id"]
                    print(f"token#{token_no} batch repo_id {first}..{last} {counts}", flush=True)

        print(
            f"start todo_repos={len(todo_repos)} batches={len(batches)} tokens={len(ready_tokens)} db={DB_PATH}",
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
            "candidates_db": str(CANDIDATES_DB_PATH),
            "todo_repo_count_at_start": len(todo_repos),
            "batch_count_at_start": len(batches),
            "token_count": token_count,
            "ready_token_count": len(ready_tokens),
            "db_path": str(DB_PATH),
        }
        summary.update(sync_summary)
        summary.update(store.status_summary())
        return summary


def main() -> None:
    summary = fetch_github_repo_stats()
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
