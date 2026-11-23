#!/usr/bin/env python3
"""Shared, persistent cache for Etherscan-style ``getsourcecode`` lookups.

One long-running instance serves every trial on the host. Each trial's policy
gateway forwards ``GET /v1/source/<chain_id>/<address>`` here. The response body
is the upstream explorer's JSON, unchanged apart from API-key redaction, so
``cast source`` and ``curl`` behave exactly as they would against Etherscan.

Storage lives under CACHE_DIR, a host bind mount, so it survives container
restarts, image rebuilds and reboots:

    v1/chain-<chain_id>/<address>/response.json  upstream body (written last, atomically)
    v1/chain-<chain_id>/<address>/meta.json      fetch time, upstream host, verified flag
    v1/chain-<chain_id>/<address>/src/...        expanded source files (verified only)
    v1/chain-<chain_id>/<address>/abi.json       formatted ABI (verified only)

Only definitive answers are stored: explorer status "1" with a result list.
Verified source is kept forever. A "not verified" answer is refetched once its
response.json is older than NOT_VERIFIED_TTL_DAYS (by file mtime). Rate-limit
replies, other status "0" replies, HTTP errors and timeouts are never stored.

Concurrent requests for one address share a single upstream call, and every
upstream call passes one token bucket. Rate-limited calls are retried with
backoff until the request deadline; after that the caller gets a 503 it may
retry, and nothing is written.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Callable


LAYOUT_VERSION = "v1"
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
# Chains Etherscan V2 does not cover, served by a compatible public explorer
# that takes no API key.
UPSTREAM_OVERRIDES = {
    "1088": "https://andromeda-explorer.metis.io/api",
}
SOURCE_PATH_RE = re.compile(r"/v1/source/([1-9][0-9]{0,9})/(0x[0-9a-fA-F]{40})/?\Z")
API_KEY_RE = re.compile(r"(?i)(apikey=)[^&\s\"'<>]+")
MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_BACKOFF_SECONDS = 4.0

Fetch = Callable[[str, str, float], "tuple[int, bytes]"]


def explorer_error(message: str) -> bytes:
    """An Etherscan-shaped error body that cast and curl users already understand."""
    return json.dumps(
        {"status": "0", "message": "NOTOK", "result": message}, separators=(",", ":")
    ).encode()


def is_rate_limited(text: str) -> bool:
    lowered = text.lower()
    return ("rate limit" in lowered or "too many" in lowered) and "daily" not in lowered


def upstream_url(chain_id: str, address: str, api_key: str) -> str:
    override = UPSTREAM_OVERRIDES.get(chain_id)
    if override is not None:
        query = urllib.parse.urlencode(
            {"module": "contract", "action": "getsourcecode", "address": address}
        )
        return f"{override}?{query}"
    query = urllib.parse.urlencode(
        {
            "chainid": chain_id,
            "module": "contract",
            "action": "getsourcecode",
            "address": address,
            "apikey": api_key,
        }
    )
    return f"{ETHERSCAN_V2_URL}?{query}"


def http_fetcher(api_key: str) -> Fetch:
    opener = urllib.request.build_opener()

    def fetch(chain_id: str, address: str, timeout: float) -> tuple[int, bytes]:
        request = urllib.request.Request(
            upstream_url(chain_id, address, api_key),
            headers={"User-Agent": "rl-source-cache/1"},
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read(MAX_BODY_BYTES + 1)
                status = response.status
        except urllib.error.HTTPError as error:
            body = error.read(MAX_BODY_BYTES + 1)
            status = error.code
        if len(body) > MAX_BODY_BYTES:
            raise ValueError("upstream response too large")
        return status, body

    return fetch


def redact(body: bytes, api_key: str) -> bytes:
    if api_key:
        body = body.replace(api_key.encode(), b"<redacted>")
    text = body.decode("utf-8", errors="surrogateescape")
    return API_KEY_RE.sub(r"\1<redacted>", text).encode("utf-8", errors="surrogateescape")


def is_verified(result: list[Any]) -> bool:
    return any(
        isinstance(entry, dict) and str(entry.get("SourceCode") or "").strip()
        for entry in result
    )


def safe_relative_path(path: str) -> str:
    """Map an explorer-supplied source path to a harmless relative path."""
    parts = []
    for part in PurePosixPath(path.replace("\\", "/")).parts:
        if part in ("", "/", "."):
            continue
        if part == "..":
            part = "_"
        part = re.sub(r"[^A-Za-z0-9._@+=,-]", "_", part)[:200]
        parts.append(part or "_")
    return "/".join(parts) or "unnamed"


def parse_source_code(code: str) -> dict[str, str] | None:
    """Return {path: content} for multi-file formats, or None for a single file."""
    text = code.strip()
    if text.startswith("{{") and text.endswith("}}"):
        text = text[1:-1]
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    if isinstance(parsed.get("sources"), dict):
        parsed = parsed["sources"]
    files: dict[str, str] = {}
    for path, item in parsed.items():
        content = item.get("content") if isinstance(item, dict) else item
        if isinstance(content, str):
            files[str(path)] = content
    return files or None


def expand_sources(result: list[Any]) -> dict[str, str]:
    files: dict[str, str] = {}
    entries = [entry for entry in result if isinstance(entry, dict)]
    for index, entry in enumerate(entries):
        code = str(entry.get("SourceCode") or "")
        if not code.strip():
            continue
        prefix = f"{index}/" if len(entries) > 1 else ""
        sources = parse_source_code(code)
        if sources is None:
            name = safe_relative_path(str(entry.get("ContractName") or "Contract"))
            vyper = str(entry.get("CompilerVersion") or "").lower().startswith("vyper")
            files[prefix + name + (".vy" if vyper else ".sol")] = code
        else:
            for path, content in sources.items():
                files[prefix + safe_relative_path(path)] = content
    return files


def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(temporary, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class TransientError(Exception):
    """Upstream is busy or unreachable; the caller may retry. Never cached."""


class UpstreamError(Exception):
    """Upstream gave a definitive non-cacheable error (bad key, unsupported chain)."""

    def __init__(self, body: bytes):
        super().__init__("upstream error")
        self.body = body


class TokenBucket:
    def __init__(
        self,
        rate: float,
        burst: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.updated = monotonic()
        self.monotonic = monotonic
        self.sleep = sleep
        self.lock = threading.Lock()

    def acquire(self, deadline: float) -> bool:
        while True:
            with self.lock:
                now = self.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
                wait = (1.0 - self.tokens) / self.rate
            if now + wait > deadline:
                return False
            self.sleep(wait)


@dataclass
class Outcome:
    status: int
    body: bytes
    cache: str  # hit, miss, refresh, stale, error


@dataclass
class CachedEntry:
    body: bytes
    verified: bool
    age_seconds: float


class _Inflight:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.outcome: Outcome | None = None


class SourceCache:
    def __init__(
        self,
        root: Path,
        fetch: Fetch,
        *,
        api_key: str = "",
        rate_per_second: float = 2.5,
        deadline_seconds: float = 25.0,
        not_verified_ttl_seconds: float = 30 * 86400,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.root = root
        self.fetch = fetch
        self.api_key = api_key
        self.deadline_seconds = deadline_seconds
        self.not_verified_ttl_seconds = not_verified_ttl_seconds
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.bucket = TokenBucket(rate_per_second, monotonic=monotonic, sleep=sleep)
        self.inflight: dict[tuple[str, str], _Inflight] = {}
        self.inflight_lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.stats: dict[str, int] = {
            "hit": 0,
            "miss": 0,
            "refresh": 0,
            "stale": 0,
            "error": 0,
            "upstream_calls": 0,
            "upstream_retries": 0,
            "shared_waits": 0,
        }

    def count(self, name: str, amount: int = 1) -> None:
        with self.stats_lock:
            self.stats[name] = self.stats.get(name, 0) + amount

    def snapshot(self) -> dict[str, int]:
        with self.stats_lock:
            return dict(self.stats)

    def entry_dir(self, chain_id: str, address: str) -> Path:
        return self.root / LAYOUT_VERSION / f"chain-{chain_id}" / address.lower()

    def read(self, chain_id: str, address: str) -> CachedEntry | None:
        path = self.entry_dir(chain_id, address) / "response.json"
        try:
            body = path.read_bytes()
            age = self.clock() - path.stat().st_mtime
            parsed = json.loads(body)
        except (OSError, ValueError):
            return None
        result = parsed.get("result") if isinstance(parsed, dict) else None
        if not isinstance(result, list):
            return None
        return CachedEntry(body=body, verified=is_verified(result), age_seconds=age)

    def lookup(self, chain_id: str, address: str) -> Outcome:
        address = address.lower()
        cached = self.read(chain_id, address)
        if cached is not None and (
            cached.verified or cached.age_seconds < self.not_verified_ttl_seconds
        ):
            self.count("hit")
            return Outcome(200, cached.body, "hit")

        key = (chain_id, address)
        with self.inflight_lock:
            shared = self.inflight.get(key)
            leader = shared is None
            if leader:
                shared = self.inflight[key] = _Inflight()
        assert shared is not None
        if not leader:
            self.count("shared_waits")
            if shared.done.wait(self.deadline_seconds + 5.0) and shared.outcome is not None:
                return shared.outcome
            self.count("error")
            return Outcome(503, explorer_error("source cache busy; retry"), "error")

        try:
            outcome = self._fill(chain_id, address, cached)
        except Exception as error:  # Never leave followers waiting on a crashed leader.
            detail = API_KEY_RE.sub(r"\1<redacted>", repr(error))
            if self.api_key:
                detail = detail.replace(self.api_key, "<redacted>")
            print(f"[source-cache] internal error {chain_id}/{address}: {detail}", file=sys.stderr)
            outcome = Outcome(500, explorer_error("source cache internal error"), "error")
        finally:
            with self.inflight_lock:
                self.inflight.pop(key, None)
        shared.outcome = outcome
        shared.done.set()
        self.count(outcome.cache)
        return outcome

    def _fill(self, chain_id: str, address: str, cached: CachedEntry | None) -> Outcome:
        try:
            body = self._fetch_definitive(chain_id, address)
        except (TransientError, UpstreamError) as error:
            if cached is not None:
                # A stale "not verified" answer beats an error.
                return Outcome(200, cached.body, "stale")
            if isinstance(error, UpstreamError):
                return Outcome(502, error.body, "error")
            return Outcome(503, explorer_error(f"source cache upstream busy: {error}; retry"), "error")
        self._store(chain_id, address, body)
        return Outcome(200, body, "refresh" if cached is not None else "miss")

    def _fetch_definitive(self, chain_id: str, address: str) -> bytes:
        deadline = self.monotonic() + self.deadline_seconds
        attempt = 0
        reason = "upstream unavailable"
        while True:
            if not self.bucket.acquire(deadline):
                raise TransientError("rate budget exhausted before deadline")
            self.count("upstream_calls")
            try:
                status, raw = self.fetch(chain_id, address, max(1.0, deadline - self.monotonic()))
            except (OSError, ValueError) as error:
                reason = f"upstream unreachable ({type(error).__name__})"
            else:
                body = redact(raw, self.api_key)
                if status == 200:
                    try:
                        parsed = json.loads(body)
                    except ValueError:
                        parsed = None
                    if isinstance(parsed, dict):
                        result = parsed.get("result")
                        if str(parsed.get("status")) == "1" and isinstance(result, list) and result:
                            return body
                        detail = f"{parsed.get('message', '')} {result if isinstance(result, str) else ''}"
                        if not is_rate_limited(detail):
                            raise UpstreamError(body)
                        reason = "upstream rate limited"
                    else:
                        reason = "upstream returned non-JSON"
                elif status == 429 or status >= 500:
                    reason = f"upstream HTTP {status}"
                else:
                    raise UpstreamError(body or explorer_error(f"upstream HTTP {status}"))
            attempt += 1
            self.count("upstream_retries")
            backoff = min(MAX_BACKOFF_SECONDS, 0.5 * 2 ** (attempt - 1)) * (0.5 + random.random())
            if self.monotonic() + backoff >= deadline:
                raise TransientError(reason)
            self.sleep(backoff)

    def _store(self, chain_id: str, address: str, body: bytes) -> None:
        parsed = json.loads(body)
        result = parsed["result"]
        verified = is_verified(result)
        directory = self.entry_dir(chain_id, address)
        directory.mkdir(parents=True, exist_ok=True)
        first = next((entry for entry in result if isinstance(entry, dict)), {})
        meta: dict[str, Any] = {
            "chain_id": int(chain_id),
            "address": address,
            "fetched_at": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
            "upstream": urllib.parse.urlsplit(upstream_url(chain_id, address, "")).netloc,
            "verified": verified,
            "contract_name": first.get("ContractName", ""),
            "compiler_version": first.get("CompilerVersion", ""),
            "proxy": first.get("Proxy", ""),
            "implementation": first.get("Implementation", ""),
            "response_sha256": hashlib.sha256(body).hexdigest(),
        }
        if verified:
            try:
                meta["files"] = self._write_sources(directory, result)
                abi = first.get("ABI")
                if isinstance(abi, str) and abi.startswith("["):
                    atomic_write(
                        directory / "abi.json",
                        (json.dumps(json.loads(abi), indent=2) + "\n").encode(),
                    )
            except (OSError, ValueError) as error:
                # The raw response is the source of truth; expansion is a convenience.
                meta["expand_error"] = repr(error)
        atomic_write(directory / "meta.json", (json.dumps(meta, indent=2) + "\n").encode())
        atomic_write(directory / "response.json", body)

    def _write_sources(self, directory: Path, result: list[Any]) -> list[str]:
        files = expand_sources(result)
        staging = directory / f".src-{os.getpid()}-{threading.get_ident()}.tmp"
        if staging.exists():
            shutil.rmtree(staging)
        staging_root = staging.resolve()
        for relative, content in files.items():
            target = (staging / relative).resolve()
            if not target.is_relative_to(staging_root):
                raise ValueError(f"unsafe source path {relative!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", errors="surrogateescape")
        staging.mkdir(exist_ok=True)
        final = directory / "src"
        if final.exists():
            shutil.rmtree(final)
        os.replace(staging, final)
        return sorted(files)


README = """\
# Etherscan source cache

Written by the rl-source-cache service (rl-tasks-full/docker/source-cache).
Every trial's policy gateway reads verified contract source through it.

Layout, one directory per contract:

    v1/chain-<chain_id>/<address>/response.json  exact explorer reply (API key redacted)
    v1/chain-<chain_id>/<address>/meta.json      fetch time, upstream, verified flag, file list
    v1/chain-<chain_id>/<address>/src/...        expanded source files (verified contracts)
    v1/chain-<chain_id>/<address>/abi.json       formatted ABI (verified contracts)

Verified source is kept forever. "Not verified" answers are refetched after
NOT_VERIFIED_TTL_DAYS (default 30), judged by response.json's modification time.
Rate-limit replies and other errors are never stored. Deleting a directory
forces a refetch; deleting everything is safe.
"""


def make_handler(cache: SourceCache, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "rl-source-cache/1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def send(self, status: int, body: bytes, cache_state: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if cache_state is not None:
                self.send_header("X-Source-Cache", cache_state)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            if path == "/health":
                self.send(200, b'{"status":"ok"}')
                return
            if path == "/v1/stats":
                self.send(200, json.dumps(cache.snapshot()).encode())
                return
            match = SOURCE_PATH_RE.match(path)
            if match is None:
                self.send(404, explorer_error("unknown endpoint"))
                return
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                self.send(401, explorer_error("missing or invalid source cache token"))
                return
            chain_id, address = match.group(1), match.group(2).lower()
            started = time.monotonic()
            outcome = cache.lookup(chain_id, address)
            if outcome.cache != "hit":
                elapsed = time.monotonic() - started
                print(
                    f"[source-cache] {outcome.cache:7} {outcome.status} chain={chain_id} "
                    f"{address} {elapsed:.2f}s",
                    flush=True,
                )
            self.send(outcome.status, outcome.body, outcome.cache)

    return Handler


def main() -> None:
    root = Path(os.environ.get("CACHE_DIR", "/cache"))
    api_key = os.environ.get("ETHERSCAN_API_KEY", "")
    if not api_key:
        print("[source-cache] warning: ETHERSCAN_API_KEY is empty; Etherscan lookups will fail", file=sys.stderr)
    root.mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(README)
    cache = SourceCache(
        root,
        http_fetcher(api_key),
        api_key=api_key,
        rate_per_second=float(os.environ.get("UPSTREAM_RATE_PER_SECOND", "2.5")),
        deadline_seconds=float(os.environ.get("REQUEST_DEADLINE_SECONDS", "25")),
        not_verified_ttl_seconds=float(os.environ.get("NOT_VERIFIED_TTL_DAYS", "30")) * 86400,
    )
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(cache, os.environ.get("SOURCE_CACHE_TOKEN", "")))
    server.daemon_threads = True
    print(f"[source-cache] serving {root} on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
