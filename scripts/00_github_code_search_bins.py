from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import find_dotenv, load_dotenv


# GitHub API facts: these values come from the GitHub REST / Search docs.
GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_RATE_LIMIT_ENDPOINT = "/rate_limit"
GITHUB_CODE_SEARCH_ENDPOINT = "/search/code"
GITHUB_ACCEPT_HEADER = "application/vnd.github+json"
GITHUB_CODE_SEARCH_MAX_PER_PAGE = 100
GITHUB_CODE_SEARCH_MAX_FILE_SIZE_BYTES = 384 * 1024

# Collection policy: these values are this script's current choices.
# File suffixes to search, run one after another, each with its own cache. Add a suffix here.
SUFFIXES = ("kicad_pro",)
FIRST_SPLIT_SIZE = 17398
SEARCH_REQUEST_INTERVAL_SECONDS = 6.2
MAX_SERVER_ERROR_RETRIES = 4
MAX_SECONDARY_RATE_LIMIT_RETRIES = 6
MAX_INCOMPLETE_RESULT_RETRIES = 2

# Local state: SQLite is the only resume checkpoint.
CACHE_DIR_PREFIX = "data/cache/github_code_search"
SCHEMA_VERSION = 1

ENV_PATH = find_dotenv(usecwd=True)
if not ENV_PATH:
    raise RuntimeError("create .env in the repo root")
load_dotenv(ENV_PATH, override=False)


def normalize_suffix(raw_suffix: str) -> str:
    suffix = raw_suffix.strip().removeprefix(".").lower()
    if not suffix or any(not (char.isalnum() or char == "_") for char in suffix):
        raise ValueError(f"invalid file suffix: {raw_suffix!r}")
    return suffix


def cache_dir_for_suffix(suffix: str) -> Path:
    return Path(f"{CACHE_DIR_PREFIX}_{suffix}")


def collect_kicad_repos(raw_suffix: str) -> dict[str, Any]:
    suffix = normalize_suffix(raw_suffix)
    cache_dir = cache_dir_for_suffix(suffix)
    db_path = cache_dir / "state.sqlite"

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

    def initial_size_bins() -> list[dict[str, Any]]:
        # Initial bins: split once at an offset boundary.
        # Example:
        #   left bin:  0 <= x < 17398  -> size:0..17397
        #   right bin: x >= 17398     -> size:17398..393215
        # Every later bisection starts from these two bins, so the whole size-bin tree depends on this boundary.
        # The right bin's upper bound is the GitHub Code Search searchable-file-size limit.
        left_end = FIRST_SPLIT_SIZE - 1
        right_end = GITHUB_CODE_SEARCH_MAX_FILE_SIZE_BYTES - 1
        return [
            {"start": 0, "end": left_end, "size": f"size:0..{left_end}"},
            {"start": FIRST_SPLIT_SIZE, "end": right_end, "size": f"size:{FIRST_SPLIT_SIZE}..{right_end}"},
        ]

    def github_headers(token: str) -> dict[str, str]:
        return {
            "Accept": GITHUB_ACCEPT_HEADER,
            "Authorization": f"Bearer {token}",
            "User-Agent": f"pcb-project-scout-{suffix}-cache",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

    def github_error_message(response: httpx.Response) -> str:
        try:
            return response.json().get("message", "")
        except json.JSONDecodeError:
            return response.text[:300]

    def setup_db() -> sqlite3.Connection:
        cache_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        with db:
            # pages keeps the raw GitHub response JSON.
            # New repo fields or export formats can be regenerated from response_json later.
            db.executescript(
                """
                create table if not exists meta (
                  key text primary key,
                  value text not null
                );

                create table if not exists pages (
                  query text not null,
                  page integer not null,
                  response_json text not null,
                  fetched_at text not null,
                  primary key (query, page)
                );

                create table if not exists query_status (
                  query text primary key,
                  suffix text not null,
                  size_start integer not null,
                  size_end integer not null,
                  total_file_count integer not null,
                  incomplete_results integer not null,
                  page_count integer not null,
                  status text not null,
                  needs_split integer not null,
                  split_into_json text not null,
                  token_no integer not null,
                  updated_at text not null
                );

                create table if not exists repos (
                  repo_id integer primary key,
                  repo_full_name text not null,
                  repo_url text not null,
                  matched_file text not null,
                  matched_file_url text not null,
                  matched_file_sha text not null,
                  query text not null,
                  page integer not null,
                  seen_at text not null
                );

                create table if not exists errors (
                  id integer primary key autoincrement,
                  token_no integer not null,
                  query text not null,
                  error text not null,
                  message text not null,
                  seen_at text not null
                );
                """
            )
            db.executemany(
                "insert or replace into meta (key, value) values (?, ?)",
                [
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("suffix", suffix),
                    ("github_api_version", GITHUB_API_VERSION),
                    ("first_split_size_bytes", str(FIRST_SPLIT_SIZE)),
                    ("github_code_search_max_file_size_bytes", str(GITHUB_CODE_SEARCH_MAX_FILE_SIZE_BYTES)),
                ],
            )
            # Older versions also flagged status='split' rows with needs_split=1.
            # Fix the semantics here: split means the parent bin is already expanded; the real todo is any missing child bin.
            db.execute(
                """
                update query_status
                set needs_split = 0
                where status in ('split', 'split_incomplete')
                  and split_into_json not in ('', '[]')
                """
            )
        return db

    def check_tokens(tokens: list[str]) -> None:
        # Startup probe: stop immediately if the API version, a token, or the network is broken.
        # This avoids writing the same failure row once per size query.
        for token_no, token in enumerate(tokens, start=1):
            with httpx.Client(base_url=GITHUB_API_BASE, headers=github_headers(token), timeout=30) as client:
                response = client.get(GITHUB_RATE_LIMIT_ENDPOINT)
                if response.status_code >= 400:
                    message = github_error_message(response)
                    raise RuntimeError(f"token#{token_no} startup check failed: HTTP {response.status_code} {message}")

                data = response.json()
                code_search = data.get("resources", {}).get("code_search", {})
                remaining = code_search.get("remaining", "?")
                limit = code_search.get("limit", "?")
                print(f"token#{token_no} ready code_search_remaining={remaining}/{limit}", flush=True)

    def load_start_bins() -> list[dict[str, Any]]:
        def size_bin_from_query(query: str) -> dict[str, Any]:
            # Query example:
            #   extension:kicad_pro size:1024..2047
            # The queue only carries size ranges; extension is fixed by the current suffix.
            size = query.split(" ", 1)[1]
            start_text, end_text = size.removeprefix("size:").split("..", 1)
            return {"start": int(start_text), "end": int(end_text), "size": size}

        with db_lock:
            rows = [dict(row) for row in db.execute("select * from query_status")]
            page_counts = {
                row["query"]: row["page_count"]
                for row in db.execute("select query, count(*) as page_count from pages group by query")
            }

        if not rows:
            return initial_size_bins()

        status_by_query = {row["query"]: row for row in rows}
        todo: dict[str, dict[str, Any]] = {}

        def add_todo(query: str) -> None:
            todo[query] = size_bin_from_query(query)

        for row in rows:
            query = row["query"]
            status = row["status"]

            if status in {"fetching_pages", "imported_split_candidate", "imported_incomplete"}:
                add_todo(query)
                continue

            if status in {"done", "imported"} and page_counts.get(query, 0) < row["page_count"]:
                add_todo(query)
                continue

            try:
                split_into = json.loads(row["split_into_json"] or "[]")
            except json.JSONDecodeError:
                split_into = []

            # split marks a parent node that has already been expanded.
            # Checkpoint example:
            #   parent: extension:kicad_pro size:0..1023, status=split
            #   child:  extension:kicad_pro size:0..511
            #   child:  extension:kicad_pro size:512..1023
            # If a child has no query_status yet, a rerun only fills in that child.
            if status in {"split", "split_incomplete"}:
                for size in split_into:
                    child_query = f"extension:{suffix} {size}"
                    if child_query not in status_by_query:
                        add_todo(child_query)

        return sorted(todo.values(), key=lambda size_bin: (size_bin["start"], size_bin["end"]))

    def status_summary() -> dict[str, Any]:
        with db_lock:
            rows = [dict(row) for row in db.execute("select * from query_status")]
            status_counts = {
                row["status"]: row["count"]
                for row in db.execute("select status, count(*) as count from query_status group by status")
            }
            page_counts = {
                row["query"]: row["page_count"]
                for row in db.execute("select query, count(*) as page_count from pages group by query")
            }

        missing_split_children = 0
        missing_page_queries = 0
        pending_query_count = 0
        split_parent_count = 0
        done_leaf_count = 0

        status_by_query = {row["query"]: row for row in rows}
        for row in rows:
            status = row["status"]
            if status in {"fetching_pages", "imported_split_candidate", "imported_incomplete"}:
                pending_query_count += 1
            if status == "done":
                done_leaf_count += 1
            if status in {"done", "imported"} and page_counts.get(row["query"], 0) < row["page_count"]:
                missing_page_queries += 1
            if status not in {"split", "split_incomplete"}:
                continue

            split_parent_count += 1
            try:
                split_into = json.loads(row["split_into_json"] or "[]")
            except json.JSONDecodeError:
                split_into = []
            for size in split_into:
                child_query = f"extension:{suffix} {size}"
                missing_split_children += int(child_query not in status_by_query)

        return {
            "status_counts": status_counts,
            "split_parent_count": split_parent_count,
            "done_leaf_count": done_leaf_count,
            "pending_query_count": pending_query_count,
            "missing_split_child_count": missing_split_children,
            "missing_page_query_count": missing_page_queries,
            "is_locally_complete": (
                pending_query_count == 0
                and missing_split_children == 0
                and missing_page_queries == 0
            ),
        }

    db = setup_db()
    db_lock = threading.Lock()

    try:
        tokens = load_tokens()
        bins = load_start_bins()
        if bins:
            check_tokens(tokens)
        work_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        for size_bin in bins:
            work_queue.put(size_bin)
        stop_event = threading.Event()

        class StopRequested(Exception):
            pass

        def sleep_or_stop(seconds: float) -> None:
            if stop_event.wait(seconds):
                raise StopRequested()

        def collect_with_token(token_no: int, token: str) -> None:
            last_search_at = 0.0

            def wait_before_search() -> None:
                nonlocal last_search_at
                elapsed = time.monotonic() - last_search_at
                if elapsed < SEARCH_REQUEST_INTERVAL_SECONDS:
                    sleep_or_stop(SEARCH_REQUEST_INTERVAL_SECONDS - elapsed)

            def mark_search_done() -> None:
                nonlocal last_search_at
                last_search_at = time.monotonic()

            def split_size_bin(size_bin: dict[str, Any]) -> list[dict[str, Any]]:
                # Dynamic binning checkpoint:
                # Only page 1 items are trusted for this decision.
                # If page 1 already returns 100 items the size range is still too dense; split it into two narrower size queries.
                # Once start == end there is nothing left to split; accept the bin as an extreme residual.
                start = size_bin["start"]
                end = size_bin["end"]
                if start >= end:
                    return []

                mid = (start + end) // 2
                return [
                    {"start": start, "end": mid, "size": f"size:{start}..{mid}"},
                    {"start": mid + 1, "end": end, "size": f"size:{mid + 1}..{end}"},
                ]

            def save_repos_from_page(query: str, page: int, page_json: dict[str, Any]) -> int:
                # repos is a derived index extracted from the raw pages.
                # repo_id is the primary key, so a repo hit by several files is de-duplicated by SQLite.
                rows = []
                for item in page_json["items"]:
                    repo = item["repository"]
                    rows.append(
                        (
                            repo["id"],
                            repo["full_name"],
                            repo["html_url"],
                            item["path"],
                            item["html_url"],
                            item["sha"],
                            query,
                            page,
                            now_utc(),
                        )
                    )

                new_count = 0
                with db_lock:
                    with db:
                        for row in rows:
                            cursor = db.execute(
                                """
                                insert or ignore into repos (
                                  repo_id, repo_full_name, repo_url, matched_file,
                                  matched_file_url, matched_file_sha,
                                  query, page, seen_at
                                )
                                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                row,
                            )
                            new_count += cursor.rowcount

                return new_count

            def write_query_status(
                query: str,
                size_bin: dict[str, Any],
                first_page: dict[str, Any],
                page_count: int,
                status: str,
                split_into: list[str],
            ) -> None:
                total_count = first_page["total_count"]
                with db_lock:
                    with db:
                        db.execute(
                            """
                            insert or replace into query_status (
                              query, suffix, size_start, size_end,
                              total_file_count, incomplete_results,
                              page_count, status, needs_split,
                              split_into_json, token_no, updated_at
                            )
                            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                query,
                                suffix,
                                size_bin["start"],
                                size_bin["end"],
                                total_count,
                                int(first_page["incomplete_results"]),
                                page_count,
                                status,
                                0,
                                json.dumps(split_into, ensure_ascii=False),
                                token_no,
                                now_utc(),
                            ),
                        )

            headers = github_headers(token)

            with httpx.Client(base_url=GITHUB_API_BASE, headers=headers, timeout=30) as client:
                def get_page(query: str, page: int) -> dict[str, Any]:
                    # Resume checkpoint:
                    # An existing pages(query, page) row means this GitHub API page is done.
                    # Reuse response_json straight from SQLite and skip the GitHub API request.
                    with db_lock:
                        row = db.execute(
                            "select response_json from pages where query = ? and page = ?",
                            (query, page),
                        ).fetchone()

                    if row is not None:
                        try:
                            return json.loads(row["response_json"])
                        except json.JSONDecodeError:
                            with db_lock:
                                with db:
                                    db.execute(
                                        "delete from pages where query = ? and page = ?",
                                        (query, page),
                                    )

                    server_errors = 0
                    secondary_rate_limit_errors = 0
                    incomplete_result_errors = 0

                    while True:
                        wait_before_search()
                        try:
                            response = client.get(
                                GITHUB_CODE_SEARCH_ENDPOINT,
                                params={"q": query, "page": page, "per_page": GITHUB_CODE_SEARCH_MAX_PER_PAGE},
                            )
                        except httpx.TransportError as exc:
                            server_errors += 1
                            if server_errors <= MAX_SERVER_ERROR_RETRIES:
                                sleep_seconds = min(30, 2**server_errors)
                                print(
                                    f"token#{token_no} transport {type(exc).__name__}, retry {server_errors}, "
                                    f"sleep {sleep_seconds}s: {query} page={page}",
                                    flush=True,
                                )
                                sleep_or_stop(sleep_seconds)
                                continue
                            raise
                        finally:
                            mark_search_done()

                        if response.status_code == 200:
                            data = response.json()
                            if (
                                page == 1
                                and data.get("incomplete_results") is True
                                and incomplete_result_errors < MAX_INCOMPLETE_RESULT_RETRIES
                            ):
                                incomplete_result_errors += 1
                                sleep_seconds = 2**incomplete_result_errors
                                print(
                                    f"token#{token_no} incomplete result, retry {incomplete_result_errors}, "
                                    f"sleep {sleep_seconds}s: {query}",
                                    flush=True,
                                )
                                sleep_or_stop(sleep_seconds)
                                continue

                            with db_lock:
                                with db:
                                    db.execute(
                                        """
                                        insert or replace into pages (query, page, response_json, fetched_at)
                                        values (?, ?, ?, ?)
                                        """,
                                        (query, page, json.dumps(data, ensure_ascii=False), now_utc()),
                                    )
                            return data

                        if response.status_code >= 500:
                            server_errors += 1
                            if server_errors <= MAX_SERVER_ERROR_RETRIES:
                                sleep_seconds = min(30, 2**server_errors)
                                print(
                                    f"token#{token_no} server {response.status_code}, retry {server_errors}, "
                                    f"sleep {sleep_seconds}s: {query} page={page}",
                                    flush=True,
                                )
                                sleep_or_stop(sleep_seconds)
                                continue
                            response.raise_for_status()

                        if response.status_code in {403, 429}:
                            retry_after = response.headers.get("retry-after")
                            if retry_after:
                                secondary_rate_limit_errors += 1
                                if secondary_rate_limit_errors > MAX_SECONDARY_RATE_LIMIT_RETRIES:
                                    response.raise_for_status()
                                sleep_seconds = int(float(retry_after)) + 1
                                print(
                                    f"token#{token_no} retry-after, retry {secondary_rate_limit_errors}, "
                                    f"sleep {sleep_seconds}s: {query} page={page}",
                                    flush=True,
                                )
                                sleep_or_stop(sleep_seconds)
                                continue

                            if response.headers.get("x-ratelimit-remaining") == "0":
                                reset = int(response.headers.get("x-ratelimit-reset", "0") or "0")
                                sleep_seconds = max(5, reset - time.time() + 2)
                                reset_utc = datetime.fromtimestamp(reset, timezone.utc).isoformat()
                                print(f"token#{token_no} rate limit, sleep {sleep_seconds:.1f}s until {reset_utc}", flush=True)
                                sleep_or_stop(sleep_seconds)
                                continue

                            message = github_error_message(response).lower()
                            if "secondary rate limit" in message or "rate limit" in message:
                                secondary_rate_limit_errors += 1
                                if secondary_rate_limit_errors > MAX_SECONDARY_RATE_LIMIT_RETRIES:
                                    response.raise_for_status()
                                sleep_seconds = min(15 * 60, 60 * 2 ** (secondary_rate_limit_errors - 1))
                                print(
                                    f"token#{token_no} secondary rate limit, retry {secondary_rate_limit_errors}, "
                                    f"sleep {sleep_seconds}s: {query} page={page}",
                                    flush=True,
                                )
                                sleep_or_stop(sleep_seconds)
                                continue

                        response.raise_for_status()

                while not stop_event.is_set():
                    try:
                        size_bin = work_queue.get(timeout=1)
                    except queue.Empty:
                        continue

                    try:
                        if size_bin is None:
                            return

                        query = f"extension:{suffix} {size_bin['size']}"
                        first_page = get_page(query, page=1)
                        new_repo_count = save_repos_from_page(query, 1, first_page)
                        total_count = first_page["total_count"]
                        incomplete_results = bool(first_page["incomplete_results"])
                        page_item_count = len(first_page.get("items", []))
                        child_bins = (
                            split_size_bin(size_bin)
                            if page_item_count >= GITHUB_CODE_SEARCH_MAX_PER_PAGE
                            else []
                        )

                        if child_bins:
                            split_into = [child["size"] for child in child_bins]
                            status = "split"
                            write_query_status(query, size_bin, first_page, 1, status, split_into)
                            for child in child_bins:
                                work_queue.put(child)
                            print(
                                f"token#{token_no} split {query} page1_items={page_item_count} total={total_count} "
                                f"into {split_into}",
                                flush=True,
                            )
                            continue

                        if page_item_count >= GITHUB_CODE_SEARCH_MAX_PER_PAGE:
                            status = "capped_exact_size"
                        elif incomplete_results:
                            status = "incomplete"
                        else:
                            status = "done"
                        write_query_status(query, size_bin, first_page, 1, status, [])
                        print(
                            f"token#{token_no} {query} status={status} page1_items={page_item_count} "
                            f"new_repos={new_repo_count}",
                            flush=True,
                        )

                    except StopRequested:
                        raise
                    except Exception as exc:
                        failed_query = ""
                        if isinstance(size_bin, dict):
                            failed_query = f"extension:{suffix} {size_bin['size']}"
                        with db_lock:
                            with db:
                                db.execute(
                                    """
                                    insert into errors (token_no, query, error, message, seen_at)
                                    values (?, ?, ?, ?, ?)
                                    """,
                                    (token_no, failed_query, type(exc).__name__, str(exc), now_utc()),
                                )
                        print(f"token#{token_no} error {failed_query}: {type(exc).__name__}", flush=True)
                    finally:
                        work_queue.task_done()

        print(f"start suffix={suffix} todo_bins={len(bins)} tokens={len(tokens)} db={db_path}", flush=True)

        with ThreadPoolExecutor(max_workers=len(tokens)) as pool:
            futures = [pool.submit(collect_with_token, token_no, token) for token_no, token in enumerate(tokens, start=1)]
            try:
                while work_queue.unfinished_tasks > 0:
                    for future in futures:
                        if future.done():
                            exception = future.exception()
                            if exception is not None:
                                raise exception
                    stop_event.wait(1)
            except BaseException:
                stop_event.set()
                raise

            stop_event.set()
            for future in futures:
                future.result()

        summary = {
            "suffix": suffix,
            "schema_version": SCHEMA_VERSION,
            "first_split_size_bytes": FIRST_SPLIT_SIZE,
            "github_code_search_max_file_size_bytes": GITHUB_CODE_SEARCH_MAX_FILE_SIZE_BYTES,
            "todo_bin_count_at_start": len(bins),
            "token_count": len(tokens),
            "page_count": db.execute("select count(*) from pages").fetchone()[0],
            "query_count": db.execute("select count(*) from query_status").fetchone()[0],
            "repo_count": db.execute("select count(*) from repos").fetchone()[0],
            "error_count": db.execute("select count(*) from errors").fetchone()[0],
            "db_path": str(db_path),
        }
        summary.update(status_summary())
        return summary

    finally:
        db.close()


def collect_kicad_pro_repos() -> dict[str, Any]:
    return collect_kicad_repos("kicad_pro")


def main() -> None:
    summaries = [collect_kicad_repos(suffix) for suffix in SUFFIXES]
    print(json.dumps(summaries, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
