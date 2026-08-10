from __future__ import annotations

import unittest

from policy_gateway import (
    PolicyError,
    RpcPolicy,
    safe_backend_error,
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
