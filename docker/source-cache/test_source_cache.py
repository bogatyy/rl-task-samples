from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from source_cache import (
    SourceCache,
    TokenBucket,
    expand_sources,
    is_rate_limited,
    make_handler,
    safe_relative_path,
    upstream_url,
)


ADDRESS = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
KEY = "real-etherscan-key"


def verified(source: str = "contract A {}", name: str = "A") -> bytes:
    return json.dumps(
        {
            "status": "1",
            "message": "OK",
            "result": [
                {
                    "SourceCode": source,
                    "ABI": '[{"type":"function","name":"f"}]',
                    "ContractName": name,
                    "CompilerVersion": "v0.8.28+commit.7893614a",
                }
            ],
        }
    ).encode()


UNVERIFIED = json.dumps(
    {
        "status": "1",
        "message": "OK",
        "result": [{"SourceCode": "", "ABI": "Contract source code not verified", "ContractName": ""}],
    }
).encode()
RATE_LIMITED = json.dumps(
    {"status": "0", "message": "NOTOK", "result": "Max calls per sec rate limit reached (3/sec)"}
).encode()
INVALID_KEY = json.dumps(
    {"status": "0", "message": "NOTOK", "result": "Invalid API Key (#err2)"}
).encode()


class FakeTime:
    def __init__(self) -> None:
        self.now = time.time()

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class ScriptedFetch:
    """Returns the scripted replies in order, repeating the last one."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, chain_id: str, address: str, timeout: float):
        self.calls.append((chain_id, address))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


class CacheTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = FakeTime()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cache(self, fetch, **kwargs) -> SourceCache:
        kwargs.setdefault("deadline_seconds", 10.0)
        return SourceCache(
            self.root,
            fetch,
            api_key=KEY,
            clock=self.clock.time,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
            **kwargs,
        )

    def entry(self, chain_id: str = "1", address: str = ADDRESS) -> Path:
        return self.root / "v1" / f"chain-{chain_id}" / address


class StorageTests(CacheTestCase):
    def test_verified_source_is_fetched_once_and_expanded(self):
        fetch = ScriptedFetch((200, verified()))
        cache = self.cache(fetch)
        first = cache.lookup("1", ADDRESS.upper().replace("0X", "0x"))
        second = cache.lookup("1", ADDRESS)
        self.assertEqual((first.status, first.cache), (200, "miss"))
        self.assertEqual((second.status, second.cache), (200, "hit"))
        self.assertEqual(len(fetch.calls), 1)
        self.assertEqual((self.entry() / "src/A.sol").read_text(), "contract A {}")
        meta = json.loads((self.entry() / "meta.json").read_text())
        self.assertTrue(meta["verified"])
        self.assertEqual(meta["files"], ["A.sol"])
        self.assertEqual(json.loads((self.entry() / "abi.json").read_text())[0]["name"], "f")

    def test_verified_source_never_expires(self):
        fetch = ScriptedFetch((200, verified()))
        cache = self.cache(fetch)
        cache.lookup("1", ADDRESS)
        old = self.clock.now - 400 * 86400
        os.utime(self.entry() / "response.json", (old, old))
        self.assertEqual(cache.lookup("1", ADDRESS).cache, "hit")
        self.assertEqual(len(fetch.calls), 1)

    def test_standard_json_sources_expand_and_stay_inside_src(self):
        standard = {
            "language": "Solidity",
            "sources": {
                "contracts/Pool.sol": {"content": "contract Pool {}"},
                "@openzeppelin/token/ERC20.sol": {"content": "contract ERC20 {}"},
                "../../escape.sol": {"content": "contract Escape {}"},
            },
        }
        fetch = ScriptedFetch((200, verified("{" + json.dumps(standard) + "}", "Pool")))
        self.cache(fetch).lookup("1", ADDRESS)
        src = self.entry() / "src"
        self.assertEqual((src / "contracts/Pool.sol").read_text(), "contract Pool {}")
        self.assertTrue((src / "@openzeppelin/token/ERC20.sol").exists())
        self.assertEqual((src / "_/_/escape.sol").read_text(), "contract Escape {}")
        self.assertFalse((self.root / "escape.sol").exists())

    def test_not_verified_is_cached_until_ttl_then_refreshed(self):
        fetch = ScriptedFetch((200, UNVERIFIED), (200, verified()))
        cache = self.cache(fetch, not_verified_ttl_seconds=30 * 86400)
        self.assertEqual(cache.lookup("1", ADDRESS).cache, "miss")
        self.assertEqual(cache.lookup("1", ADDRESS).cache, "hit")
        self.assertFalse((self.entry() / "src").exists())
        old = self.clock.now - 31 * 86400
        os.utime(self.entry() / "response.json", (old, old))
        refreshed = cache.lookup("1", ADDRESS)
        self.assertEqual((refreshed.status, refreshed.cache), (200, "refresh"))
        self.assertEqual(len(fetch.calls), 2)
        self.assertTrue((self.entry() / "src/A.sol").exists())

    def test_stale_not_verified_is_served_when_refresh_fails(self):
        fetch = ScriptedFetch((200, UNVERIFIED), (200, RATE_LIMITED))
        cache = self.cache(fetch, deadline_seconds=3.0)
        cache.lookup("1", ADDRESS)
        old = self.clock.now - 31 * 86400
        os.utime(self.entry() / "response.json", (old, old))
        outcome = cache.lookup("1", ADDRESS)
        self.assertEqual((outcome.status, outcome.cache), (200, "stale"))
        self.assertEqual(json.loads(outcome.body)["status"], "1")

    def test_api_key_is_redacted_before_storage(self):
        leaky = verified(f"// fetched with apikey={KEY}\ncontract A {{}}")
        self.cache(ScriptedFetch((200, leaky))).lookup("1", ADDRESS)
        stored = (self.entry() / "response.json").read_text()
        self.assertNotIn(KEY, stored)
        self.assertNotIn(KEY, (self.entry() / "src/A.sol").read_text())

    def test_chains_are_stored_separately(self):
        fetch = ScriptedFetch((200, verified()))
        cache = self.cache(fetch)
        cache.lookup("1", ADDRESS)
        cache.lookup("56", ADDRESS)
        self.assertEqual(fetch.calls, [("1", ADDRESS), ("56", ADDRESS)])
        self.assertTrue(self.entry("56").exists())


class UpstreamFailureTests(CacheTestCase):
    def test_rate_limit_is_retried_and_never_stored(self):
        fetch = ScriptedFetch((200, RATE_LIMITED), (200, RATE_LIMITED), (200, verified()))
        outcome = self.cache(fetch).lookup("1", ADDRESS)
        self.assertEqual((outcome.status, outcome.cache), (200, "miss"))
        self.assertEqual(len(fetch.calls), 3)
        self.assertEqual(json.loads((self.entry() / "response.json").read_bytes())["status"], "1")

    def test_persistent_rate_limit_returns_503_and_writes_nothing(self):
        fetch = ScriptedFetch((200, RATE_LIMITED))
        cache = self.cache(fetch, deadline_seconds=5.0)
        outcome = cache.lookup("1", ADDRESS)
        self.assertEqual(outcome.status, 503)
        self.assertEqual(json.loads(outcome.body)["status"], "0")
        self.assertFalse(self.entry().exists())
        self.assertGreater(len(fetch.calls), 1)
        cache.lookup("1", ADDRESS)  # nothing was cached, so this asks again
        self.assertGreater(cache.snapshot()["upstream_calls"], len(fetch.calls) - 1)

    def test_http_429_5xx_and_network_errors_are_retried(self):
        fetch = ScriptedFetch((429, b"slow down"), (502, b"bad gateway"), OSError("reset"), (200, verified()))
        outcome = self.cache(fetch).lookup("1", ADDRESS)
        self.assertEqual(outcome.status, 200)
        self.assertEqual(len(fetch.calls), 4)

    def test_definitive_upstream_error_is_passed_through_not_stored(self):
        fetch = ScriptedFetch((200, INVALID_KEY))
        cache = self.cache(fetch)
        outcome = cache.lookup("1", ADDRESS)
        self.assertEqual(outcome.status, 502)
        self.assertIn(b"Invalid API Key", outcome.body)
        self.assertFalse(self.entry().exists())
        cache.lookup("1", ADDRESS)
        self.assertEqual(len(fetch.calls), 2)

    def test_daily_limit_is_not_treated_as_retryable(self):
        self.assertTrue(is_rate_limited("NOTOK Max calls per sec rate limit reached (3/sec)"))
        self.assertFalse(is_rate_limited("NOTOK Max daily rate limit reached. 100000 calls per day"))


class ConcurrencyTests(CacheTestCase):
    def test_concurrent_requests_share_one_upstream_call(self):
        release = threading.Event()
        calls: list[str] = []

        def slow_fetch(chain_id, address, timeout):
            calls.append(address)
            release.wait(5)
            return 200, verified()

        cache = SourceCache(self.root, slow_fetch, api_key=KEY, rate_per_second=100)
        outcomes = []
        threads = [
            threading.Thread(target=lambda: outcomes.append(cache.lookup("1", ADDRESS)))
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        time.sleep(0.2)
        release.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(outcome.status for outcome in outcomes), [200] * 8)
        self.assertEqual(cache.snapshot()["shared_waits"], 7)

    def test_token_bucket_paces_calls(self):
        clock = FakeTime()
        bucket = TokenBucket(2.0, monotonic=clock.monotonic, sleep=clock.sleep)
        start = clock.now
        for _ in range(5):
            self.assertTrue(bucket.acquire(start + 100))
        self.assertAlmostEqual(clock.now - start, 2.0, places=6)
        self.assertFalse(bucket.acquire(clock.now + 0.1))


class FormatTests(unittest.TestCase):
    def test_metis_goes_to_public_explorer_without_key(self):
        url = upstream_url("1088", ADDRESS, KEY)
        self.assertTrue(url.startswith("https://andromeda-explorer.metis.io/api?"))
        self.assertNotIn(KEY, url)

    def test_etherscan_v2_keeps_chain_and_key(self):
        url = upstream_url("8453", ADDRESS, KEY)
        self.assertTrue(url.startswith("https://api.etherscan.io/v2/api?"))
        self.assertIn("chainid=8453", url)
        self.assertIn(f"apikey={KEY}", url)

    def test_legacy_multi_file_and_vyper_sources(self):
        legacy = json.dumps({"A.sol": {"content": "a"}, "lib/B.sol": {"content": "b"}})
        files = expand_sources([{"SourceCode": legacy, "ContractName": "A"}])
        self.assertEqual(files, {"A.sol": "a", "lib/B.sol": "b"})
        vyper = expand_sources([{"SourceCode": "# @version 0.3", "ContractName": "Pool", "CompilerVersion": "vyper:0.3.10"}])
        self.assertEqual(list(vyper), ["Pool.vy"])

    def test_safe_relative_path(self):
        self.assertEqual(safe_relative_path("/abs/../x.sol"), "abs/_/x.sol")
        self.assertEqual(safe_relative_path("a b/c?.sol"), "a_b/c_.sol")


class HttpTests(CacheTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fetch = ScriptedFetch((200, verified()))
        handler = make_handler(self.cache(self.fetch), "secret-token")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path: str, token: str | None = "secret-token"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        request = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers.get("X-Source-Cache"), response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.headers.get("X-Source-Cache"), error.read()

    def test_health_and_stats(self):
        self.assertEqual(self.get("/health", token=None)[0], 200)
        self.assertEqual(json.loads(self.get("/v1/stats", token=None)[2])["hit"], 0)

    def test_source_requires_token_when_configured(self):
        self.assertEqual(self.get(f"/v1/source/1/{ADDRESS}", token=None)[0], 401)
        self.assertEqual(self.get(f"/v1/source/1/{ADDRESS}", token="wrong")[0], 401)
        self.assertEqual(self.fetch.calls, [])

    def test_source_miss_then_hit(self):
        status, state, body = self.get(f"/v1/source/1/{ADDRESS}")
        self.assertEqual((status, state), (200, "miss"))
        self.assertEqual(json.loads(body)["result"][0]["ContractName"], "A")
        self.assertEqual(self.get(f"/v1/source/1/{ADDRESS}")[:2], (200, "hit"))

    def test_bad_paths_are_rejected(self):
        for path in ("/v1/source/1/0x1234", "/v1/source/0/" + ADDRESS, "/v1/source/1/" + ADDRESS + "/x", "/api"):
            self.assertEqual(self.get(path)[0], 404, path)


if __name__ == "__main__":
    unittest.main()
