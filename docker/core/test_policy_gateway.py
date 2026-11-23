from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from policy_gateway import (
    PolicyError,
    RpcPolicy,
    safe_backend_error,
    source_handler,
    source_upstream_request,
    validate_source_query,
)


FORK_BLOCK = 25_541_000
FUTURE_BLOCK = 25_541_217
FUTURE_HASH = "0x" + "ab" * 32
LOCAL_HASH = "0x" + "cd" * 32
OLD_HASH = "0x" + "ef" * 32


class FakeBackend:
    def __init__(self):
        self.current = FORK_BLOCK
        self.calls: list[tuple[str, object]] = []
        self.transactions = {
            FUTURE_HASH: {
                "hash": FUTURE_HASH,
                "blockNumber": hex(FUTURE_BLOCK),
                "blockHash": "0x" + "11" * 32,
            },
            OLD_HASH: {
                "hash": OLD_HASH,
                "blockNumber": hex(FORK_BLOCK - 1),
                "blockHash": "0x" + "22" * 32,
            },
            LOCAL_HASH: {
                "hash": LOCAL_HASH,
                "blockNumber": hex(FORK_BLOCK + 1),
                "blockHash": "0x" + "33" * 32,
            },
        }

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "eth_blockNumber":
            return hex(self.current)
        if method == "eth_getTransactionByHash":
            return self.transactions.get(params[0])
        if method == "eth_getTransactionReceipt":
            return self.transactions.get(params[0])
        if method == "eth_getRawTransactionByHash":
            return "0x1234"
        if method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            if number > self.current:
                return None
            block_hash = "0x" + "33" * 32 if number == FORK_BLOCK + 1 else "0x" + "22" * 32
            return {"number": hex(number), "hash": block_hash}
        if method == "eth_getBlockByHash":
            for number, block_hash in (
                (FORK_BLOCK - 1, "0x" + "22" * 32),
                (FORK_BLOCK + 1, "0x" + "33" * 32),
            ):
                if params[0].lower() == block_hash:
                    return {"number": hex(number), "hash": block_hash}
            return None
        if method in {"eth_sendRawTransaction", "eth_sendTransaction"}:
            self.current = FORK_BLOCK + 1
            return LOCAL_HASH
        if method == "eth_getLogs":
            return []
        return "ok"


def request(method, params=None, request_id=1):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": [] if params is None else params,
    }


class SourcePolicyTests(unittest.TestCase):
    def test_backend_errors_redact_credential_urls(self):
        message = (
            "execution reverted while fetching "
            "https://node.invalid/v2/archive-secret?apikey=key-secret"
        )
        result = safe_backend_error(message)
        self.assertIn("execution reverted", result)
        self.assertNotIn("archive-secret", result)
        self.assertNotIn("key-secret", result)

    def test_only_getsourcecode_is_accepted(self):
        address = "0x" + "12" * 20
        self.assertEqual(
            validate_source_query(
                f"/api?module=contract&action=getsourcecode&address={address}&apikey=dummy"
            ),
            address,
        )

    def test_transaction_history_is_rejected(self):
        address = "0x" + "12" * 20
        with self.assertRaises(PolicyError):
            validate_source_query(
                f"/api?module=account&action=tokentx&address={address}&apikey=dummy"
            )

    def test_proxy_receipt_is_rejected(self):
        with self.assertRaises(PolicyError):
            validate_source_query(
                f"/api?module=proxy&action=eth_getTransactionReceipt&txhash={FUTURE_HASH}"
            )

    def test_chain_specific_source_query(self):
        address = "0x" + "12" * 20
        self.assertEqual(
            validate_source_query(
                f"/api?chainid=8453&module=contract&action=getsourcecode&address={address}",
                "8453",
            ),
            address,
        )
        with self.assertRaises(PolicyError):
            validate_source_query(
                f"/api?chainid=1&module=contract&action=getsourcecode&address={address}",
                "8453",
            )

    def test_upstream_request_targets_shared_cache(self):
        address = "0x" + "AB" * 20
        request = source_upstream_request("http://rl-source-cache:8080/", "8453", address, "tok")
        self.assertEqual(
            request.full_url, f"http://rl-source-cache:8080/v1/source/8453/{address.lower()}"
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")
        self.assertIsNone(
            source_upstream_request("http://cache", "1", address).get_header("Authorization")
        )


VERIFIED_BODY = json.dumps(
    {"status": "1", "message": "OK", "result": [{"SourceCode": "contract A {}", "ContractName": "A"}]}
).encode()
BUSY_BODY = json.dumps(
    {"status": "0", "message": "NOTOK", "result": "source cache upstream busy; retry"}
).encode()
BUSY_ADDRESS = "0x" + "bb" * 20
BROKEN_ADDRESS = "0x" + "ee" * 20


class FakeSourceCache(BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None]] = []

    def log_message(self, *_args):
        return

    def do_GET(self):
        FakeSourceCache.seen.append((self.path, self.headers.get("Authorization")))
        if self.path.endswith(BUSY_ADDRESS):
            status, body, kind = 503, BUSY_BODY, "application/json"
        elif self.path.endswith(BROKEN_ADDRESS):
            status, body, kind = 500, b"<html>boom</html>", "text/html"
        else:
            status, body, kind = 200, VERIFIED_BODY, "application/json"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SourceForwardingTests(unittest.TestCase):
    def setUp(self):
        FakeSourceCache.seen = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeSourceCache)
        upstream_url = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        self.gateway = ThreadingHTTPServer(
            ("127.0.0.1", 0), source_handler(upstream_url, "1", "cache-token")
        )
        self.servers = [self.upstream, self.gateway]
        for server in self.servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def get(self, address, server=None):
        server = server or self.gateway
        url = (
            f"http://127.0.0.1:{server.server_address[1]}/api?chainid=1&module=contract"
            f"&action=getsourcecode&address={address}&apikey=source-only"
        )
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read()

    def test_lookup_is_forwarded_with_token(self):
        address = "0x" + "Ab" * 20
        status, body = self.get(address)
        self.assertEqual((status, body), (200, VERIFIED_BODY))
        self.assertEqual(
            FakeSourceCache.seen, [(f"/v1/source/1/{address.lower()}", "Bearer cache-token")]
        )

    def test_explorer_shaped_errors_pass_through(self):
        status, body = self.get(BUSY_ADDRESS)
        self.assertEqual((status, body), (503, BUSY_BODY))

    def test_other_upstream_errors_are_masked(self):
        status, body = self.get(BROKEN_ADDRESS)
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["result"], "source gateway upstream returned HTTP 500")

    def test_rejected_queries_never_reach_upstream(self):
        url = f"http://127.0.0.1:{self.gateway.server_address[1]}/api?module=account&action=txlist"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url, timeout=5)
        caught.exception.close()
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(FakeSourceCache.seen, [])

    def test_budget_counts_distinct_addresses_only(self):
        first, second, third = ("0x" + c * 40 for c in "123")
        with mock.patch("policy_gateway.MAX_SOURCE_REQUESTS", 2):
            self.assertEqual(self.get(first)[0], 200)
            self.assertEqual(self.get(second)[0], 200)
            self.assertEqual(self.get(first)[0], 200)
            status, body = self.get(third)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["result"], "source request budget exhausted")

    def test_unreachable_upstream_is_502(self):
        dead = ThreadingHTTPServer(("127.0.0.1", 0), FakeSourceCache)
        dead_url = f"http://127.0.0.1:{dead.server_address[1]}"
        dead.server_close()
        gateway = ThreadingHTTPServer(("127.0.0.1", 0), source_handler(dead_url, "1"))
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        self.servers.append(gateway)
        status, body = self.get("0x" + "12" * 20, server=gateway)
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["result"], "source gateway unavailable")


class RpcPolicyTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.policy = RpcPolicy(self.backend, FORK_BLOCK)

    def test_future_transaction_lookup_returns_null(self):
        response = self.policy.handle(request("eth_getTransactionByHash", [FUTURE_HASH]))
        self.assertIsNone(response["result"])

    def test_prefork_transaction_lookup_is_allowed(self):
        response = self.policy.handle(request("eth_getTransactionByHash", [OLD_HASH]))
        self.assertEqual(response["result"]["hash"], OLD_HASH)

    def test_future_raw_transaction_lookup_returns_null(self):
        response = self.policy.handle(request("eth_getRawTransactionByHash", [FUTURE_HASH]))
        self.assertIsNone(response["result"])

    def test_locally_submitted_transaction_remains_visible(self):
        sent = self.policy.handle(request("eth_sendRawTransaction", ["0x1234"]))
        self.assertEqual(sent["result"], LOCAL_HASH)
        looked_up = self.policy.handle(request("eth_getTransactionByHash", [LOCAL_HASH]))
        self.assertEqual(looked_up["result"]["hash"], LOCAL_HASH)

    def test_future_block_is_not_forwarded(self):
        response = self.policy.handle(
            request("eth_getBlockByNumber", [hex(FUTURE_BLOCK), False])
        )
        self.assertIsNone(response["result"])

    def test_consensus_head_aliases_are_not_forwarded(self):
        for alias in ("safe", "finalized"):
            with self.subTest(alias=alias):
                before = len(self.backend.calls)
                response = self.policy.handle(
                    request("eth_getBlockByNumber", [alias, False])
                )
                self.assertIsNone(response["result"])
                self.assertEqual(len(self.backend.calls), before)

    def test_future_transaction_trace_is_denied(self):
        response = self.policy.handle(
            request("debug_traceTransaction", [FUTURE_HASH, {}])
        )
        self.assertEqual(response["error"]["code"], -32004)

    def test_prefork_block_receipts_by_hash_are_allowed(self):
        response = self.policy.handle(
            request("eth_getBlockReceipts", ["0x" + "22" * 32])
        )
        self.assertEqual(response["result"], "ok")

    def test_anvil_reset_is_denied(self):
        response = self.policy.handle(request("anvil_reset", []))
        self.assertEqual(response["error"]["code"], -32004)

    def test_log_filter_at_fork_is_allowed(self):
        response = self.policy.handle(
            request(
                "eth_getLogs",
                [{"fromBlock": hex(FORK_BLOCK - 10), "toBlock": "latest"}],
            )
        )
        self.assertEqual(response["result"], [])

    def test_unbounded_log_scan_is_denied(self):
        response = self.policy.handle(
            request(
                "eth_getLogs",
                [{"fromBlock": "earliest", "toBlock": "latest"}],
            )
        )
        self.assertEqual(response["error"]["code"], -32602)

    def test_large_fee_history_is_denied(self):
        response = self.policy.handle(
            request("eth_feeHistory", [hex(8_193), "latest", []])
        )
        self.assertEqual(response["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
