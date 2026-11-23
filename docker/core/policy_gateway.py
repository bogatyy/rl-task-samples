#!/usr/bin/env python3
"""Source-only explorer and fork-bounded JSON-RPC gateway.

This process runs in a per-trial sidecar that the agent cannot inspect. The
agent can reach its two HTTP listeners, but not the archive-backed Anvil
service behind it and not the source upstream directly.

Source lookups are validated here and forwarded to SOURCE_UPSTREAM_URL, the
shared source cache (docker/source-cache), which owns the explorer credential,
caching and upstream rate limiting. This sidecar keeps no source cache.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}\Z")
HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}\Z")
URL_RE = re.compile(r"(?i)\b(?:https?|wss?)://[^\s\"'<>]+")
API_KEY_RE = re.compile(r"(?i)(apikey=)[^&\s\"'<>]+")
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RPC_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_BATCH_REQUESTS = 32
MAX_RPC_CALLS = 25_000
# Distinct contract addresses one trial may look up. Repeats and retries of an
# address already counted are free.
MAX_SOURCE_REQUESTS = 1024
# Longer than the source cache's own deadline, so its 503 reaches the agent.
SOURCE_UPSTREAM_TIMEOUT_SECONDS = 60
MAX_BLOCK_RANGE = 100_000
MAX_FEE_HISTORY_BLOCKS = 8_192
MAX_BACKEND_CONCURRENCY = 16
MAX_SOURCE_CONCURRENCY = 4


class PolicyError(Exception):
    def __init__(self, message: str, code: int = -32004):
        super().__init__(message)
        self.code = code
        self.message = message


def safe_backend_error(message: Any) -> str:
    """Keep useful revert text while removing any upstream credential URL."""
    text = str(message)[:4_096]
    text = URL_RE.sub("<redacted-url>", text)
    return API_KEY_RE.sub(r"\1<redacted>", text)


def _hex_number(value: Any) -> int | None:
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def validate_source_query(path: str, chain_id: str = "1") -> str:
    parsed = urllib.parse.urlsplit(path)
    if parsed.path not in {"/api", "/v2/api"}:
        raise PolicyError("only the Etherscan source endpoint is available")

    query = urllib.parse.parse_qs(
        parsed.query, keep_blank_values=True, strict_parsing=True
    )
    allowed = {"module", "action", "address", "apikey", "chainid"}
    if set(query) - allowed or any(len(values) != 1 for values in query.values()):
        raise PolicyError("unsupported explorer query")
    if query.get("module") != ["contract"] or query.get("action") != [
        "getsourcecode"
    ]:
        raise PolicyError("only contract/getsourcecode is available")
    if "chainid" in query and query["chainid"] != [chain_id]:
        raise PolicyError("source query is for the wrong chain")
    address = query.get("address", [""])[0]
    if not ADDRESS_RE.fullmatch(address):
        raise PolicyError("invalid contract address")
    return address


def source_upstream_request(
    upstream_url: str, chain_id: str, address: str, token: str = ""
) -> urllib.request.Request:
    """Build the shared source cache request for one validated address."""
    headers = {"User-Agent": "rl-task-source-gateway/2"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(
        f"{upstream_url.rstrip('/')}/v1/source/{chain_id}/{address.lower()}",
        headers=headers,
    )


def explorer_error_body(message: str) -> bytes:
    return json.dumps(
        {"status": "0", "message": "NOTOK", "result": message},
        separators=(",", ":"),
    ).encode()


class Backend:
    def __init__(self, url: str):
        self.url = url
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._semaphore = threading.BoundedSemaphore(MAX_BACKEND_CONCURRENCY)

    def _id(self) -> int:
        with self._id_lock:
            value = self._next_id
            self._next_id += 1
            return value

    def request(self, method: str, params: list[Any] | dict[str, Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": self._id(),
            "method": method,
            "params": params,
        }
        data = json.dumps(payload, separators=(",", ":")).encode()
        request = urllib.request.Request(
            self.url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._semaphore:
            with self._opener.open(request, timeout=60) as response:
                body = response.read(MAX_RPC_RESPONSE_BYTES + 1)
        if len(body) > MAX_RPC_RESPONSE_BYTES:
            raise PolicyError("backend response too large")
        decoded = json.loads(body)
        if not isinstance(decoded, dict):
            raise PolicyError("invalid backend response")
        if "error" in decoded:
            error = decoded["error"] if isinstance(decoded["error"], dict) else {}
            raise PolicyError(
                safe_backend_error(error.get("message", "backend RPC error")),
                int(error.get("code", -32000)),
            )
        return decoded.get("result")


class RpcPolicy:
    """Allow normal fork work while removing all upstream-history escape hatches."""

    SIMPLE_METHODS = {
        "eth_accounts",
        "eth_blobBaseFee",
        "eth_blockNumber",
        "eth_chainId",
        "eth_coinbase",
        "eth_gasPrice",
        "eth_getFilterChanges",
        "eth_getFilterLogs",
        "eth_getWork",
        "eth_hashrate",
        "eth_maxPriorityFeePerGas",
        "eth_mining",
        "eth_newBlockFilter",
        "eth_newPendingTransactionFilter",
        "eth_protocolVersion",
        "eth_sign",
        "eth_signTransaction",
        "eth_signTypedData",
        "eth_signTypedData_v3",
        "eth_signTypedData_v4",
        "eth_syncing",
        "eth_uninstallFilter",
        "net_listening",
        "net_peerCount",
        "net_version",
        "rpc_modules",
        "web3_clientVersion",
        "web3_sha3",
    }

    BLOCK_REF_METHODS = {
        "debug_traceCall": 1,
        "eth_call": 1,
        "eth_createAccessList": 1,
        "eth_estimateGas": 1,
        "eth_getBalance": 1,
        "eth_getCode": 1,
        "eth_getProof": 2,
        "eth_getStorageAt": 2,
        "eth_getTransactionCount": 1,
        "trace_call": 2,
    }

    BLOCK_NUMBER_METHODS = {
        "debug_traceBlockByNumber": 0,
        "eth_getBlockTransactionCountByNumber": 0,
        "eth_getRawTransactionByBlockNumberAndIndex": 0,
        "eth_getTransactionByBlockNumberAndIndex": 0,
        "eth_getUncleByBlockNumberAndIndex": 0,
        "eth_getUncleCountByBlockNumber": 0,
        "trace_block": 0,
        "trace_replayBlockTransactions": 0,
    }

    BLOCK_HASH_METHODS = {
        "debug_traceBlockByHash": 0,
        "eth_getBlockTransactionCountByHash": 0,
        "eth_getRawTransactionByBlockHashAndIndex": 0,
        "eth_getTransactionByBlockHashAndIndex": 0,
        "eth_getUncleByBlockHashAndIndex": 0,
        "eth_getUncleCountByBlockHash": 0,
    }

    TX_LOOKUP_METHODS = {
        "eth_getTransactionByHash",
        "eth_getTransactionReceipt",
    }
    RAW_TX_LOOKUP_METHODS = {"eth_getRawTransactionByHash"}
    TX_TRACE_METHODS = {
        "debug_traceTransaction",
        "trace_replayTransaction",
        "trace_transaction",
    }
    SEND_METHODS = {"eth_sendRawTransaction", "eth_sendTransaction"}

    def __init__(self, backend: Backend, fork_block: int):
        self.backend = backend
        self.fork_block = fork_block
        self.local_transactions: set[str] = set()
        self._local_lock = threading.Lock()
        self._request_count = 0
        self._request_lock = threading.Lock()

    def _current_block(self) -> int:
        value = _hex_number(self.backend.request("eth_blockNumber", []))
        if value is None:
            raise PolicyError("invalid fork block number from backend")
        return value

    def _canonical_block_hash(self, number: int) -> str | None:
        block = self.backend.request("eth_getBlockByNumber", [hex(number), False])
        if not isinstance(block, dict):
            return None
        value = block.get("hash")
        return value.lower() if isinstance(value, str) else None

    def _block_hash_allowed(self, value: Any) -> bool:
        if not isinstance(value, str) or not HASH_RE.fullmatch(value):
            return False
        block = self.backend.request("eth_getBlockByHash", [value, False])
        if not isinstance(block, dict):
            return False
        number = _hex_number(block.get("number"))
        if number is None:
            return False
        if number <= self.fork_block:
            return True
        if number > self._current_block():
            return False
        return self._canonical_block_hash(number) == value.lower()

    def _block_ref_allowed(self, value: Any) -> bool:
        if value is None or value in {"earliest", "latest", "pending"}:
            return True
        if isinstance(value, dict):
            if "blockHash" in value:
                return self._block_hash_allowed(value["blockHash"])
            value = value.get("blockNumber")
        number = _hex_number(value)
        return number is not None and number <= self._current_block()

    def _require_block_ref(self, value: Any) -> None:
        if not self._block_ref_allowed(value):
            raise PolicyError("block is outside the pinned fork")

    def _block_ref_number(self, value: Any) -> int:
        if value is None or value in {"latest", "pending"}:
            return self._current_block()
        if value == "earliest":
            return 0
        if isinstance(value, dict):
            if "blockHash" in value:
                block = self.backend.request(
                    "eth_getBlockByHash", [value["blockHash"], False]
                )
                number = _hex_number(block.get("number")) if isinstance(block, dict) else None
                if number is None:
                    raise PolicyError("invalid block reference", -32602)
                return number
            value = value.get("blockNumber")
        number = _hex_number(value)
        if number is None:
            raise PolicyError("invalid block reference", -32602)
        return number

    def _require_bounded_range(self, first: Any, last: Any) -> None:
        self._require_block_ref(first)
        self._require_block_ref(last)
        first_number = self._block_ref_number(first)
        last_number = self._block_ref_number(last)
        if last_number < first_number or last_number - first_number > MAX_BLOCK_RANGE:
            raise PolicyError("block range is too large", -32602)

    def _transaction_allowed(self, tx_hash: str, result: Any | None = None) -> bool:
        if not isinstance(tx_hash, str) or not HASH_RE.fullmatch(tx_hash):
            return False
        lowered = tx_hash.lower()
        with self._local_lock:
            if lowered in self.local_transactions:
                return True
        tx = (
            result
            if isinstance(result, dict) and "blockNumber" in result
            else self.backend.request("eth_getTransactionByHash", [tx_hash])
        )
        if not isinstance(tx, dict):
            return False
        number = _hex_number(tx.get("blockNumber"))
        if number is None:
            return False
        if number <= self.fork_block:
            return True
        block_hash = tx.get("blockHash")
        if not isinstance(block_hash, str) or number > self._current_block():
            return False
        return self._canonical_block_hash(number) == block_hash.lower()

    def _filter_transaction_result(self, tx_hash: Any, result: Any) -> Any:
        if result is None:
            return None
        return result if self._transaction_allowed(str(tx_hash), result) else None

    @staticmethod
    def _params(request: dict[str, Any]) -> list[Any] | dict[str, Any]:
        params = request.get("params", [])
        if not isinstance(params, (list, dict)):
            raise PolicyError("invalid JSON-RPC params", -32602)
        return params

    @staticmethod
    def _at(params: list[Any] | dict[str, Any], index: int) -> Any:
        if not isinstance(params, list):
            raise PolicyError("positional JSON-RPC params required", -32602)
        return params[index] if len(params) > index else None

    def _forward(self, method: str, params: list[Any] | dict[str, Any]) -> Any:
        return self.backend.request(method, params)

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        method = request.get("method")
        try:
            with self._request_lock:
                self._request_count += 1
                if self._request_count > MAX_RPC_CALLS:
                    raise PolicyError("fork RPC request budget exhausted")
            if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
                raise PolicyError("invalid JSON-RPC request", -32600)
            params = self._params(request)

            if method.startswith(("anvil_", "evm_", "hardhat_")):
                raise PolicyError("state-mutation RPC methods are unavailable")

            if method in self.TX_LOOKUP_METHODS:
                tx_hash = self._at(params, 0)
                result = self._forward(method, params)
                result = self._filter_transaction_result(tx_hash, result)
            elif method in self.RAW_TX_LOOKUP_METHODS:
                tx_hash = self._at(params, 0)
                result = (
                    self._forward(method, params)
                    if self._transaction_allowed(str(tx_hash))
                    else None
                )
            elif method in self.TX_TRACE_METHODS:
                tx_hash = self._at(params, 0)
                if not self._transaction_allowed(str(tx_hash)):
                    raise PolicyError("transaction is outside the pinned fork")
                result = self._forward(method, params)
            elif method in self.SEND_METHODS:
                result = self._forward(method, params)
                if isinstance(result, str) and HASH_RE.fullmatch(result):
                    with self._local_lock:
                        self.local_transactions.add(result.lower())
            elif method == "eth_getBlockByHash":
                block_hash = self._at(params, 0)
                result = (
                    self._forward(method, params)
                    if self._block_hash_allowed(block_hash)
                    else None
                )
            elif method == "eth_getBlockByNumber":
                block_ref = self._at(params, 0)
                if not self._block_ref_allowed(block_ref):
                    result = None
                else:
                    result = self._forward(method, params)
            elif method == "eth_getBlockReceipts":
                block_ref = self._at(params, 0)
                if isinstance(block_ref, str) and HASH_RE.fullmatch(block_ref):
                    if not self._block_hash_allowed(block_ref):
                        raise PolicyError("block is outside the pinned fork")
                else:
                    self._require_block_ref(block_ref)
                result = self._forward(method, params)
            elif method in self.BLOCK_REF_METHODS:
                index = self.BLOCK_REF_METHODS[method]
                if isinstance(params, list) and len(params) > index:
                    self._require_block_ref(params[index])
                result = self._forward(method, params)
            elif method in self.BLOCK_NUMBER_METHODS:
                self._require_block_ref(self._at(params, self.BLOCK_NUMBER_METHODS[method]))
                result = self._forward(method, params)
            elif method in self.BLOCK_HASH_METHODS:
                block_hash = self._at(params, self.BLOCK_HASH_METHODS[method])
                if not self._block_hash_allowed(block_hash):
                    raise PolicyError("block is outside the pinned fork")
                result = self._forward(method, params)
            elif method in {"eth_getLogs", "eth_newFilter"}:
                query = self._at(params, 0)
                if not isinstance(query, dict):
                    raise PolicyError("invalid log filter", -32602)
                if "blockHash" in query:
                    if not self._block_hash_allowed(query["blockHash"]):
                        raise PolicyError("block is outside the pinned fork")
                else:
                    self._require_bounded_range(
                        query.get("fromBlock", "latest"),
                        query.get("toBlock", "latest"),
                    )
                result = self._forward(method, params)
            elif method == "eth_feeHistory":
                block_count = _hex_number(self._at(params, 0))
                if block_count is None or block_count > MAX_FEE_HISTORY_BLOCKS:
                    raise PolicyError("fee history range is too large", -32602)
                self._require_block_ref(self._at(params, 1))
                result = self._forward(method, params)
            elif method == "trace_filter":
                query = self._at(params, 0)
                if not isinstance(query, dict):
                    raise PolicyError("invalid trace filter", -32602)
                self._require_bounded_range(
                    query.get("fromBlock", "latest"),
                    query.get("toBlock", "latest"),
                )
                result = self._forward(method, params)
            elif method in self.SIMPLE_METHODS:
                result = self._forward(method, params)
            else:
                raise PolicyError("method unavailable on pinned fork gateway")

            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except PolicyError as error:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": error.code, "message": error.message},
            }
        except (OSError, ValueError, json.JSONDecodeError):
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": "fork gateway unavailable"},
            }


class QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def send_body(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def health(self) -> None:
        self.send_body(200, b"ok\n", "text/plain")


def rpc_handler(policy: RpcPolicy) -> type[QuietHandler]:
    class RpcHandler(QuietHandler):
        def do_GET(self) -> None:  # noqa: N802
            if urllib.parse.urlsplit(self.path).path == "/health":
                self.health()
            else:
                self.send_body(405, b"method not allowed\n", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if urllib.parse.urlsplit(self.path).path not in {"", "/"}:
                self.send_body(404, b"not found\n", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_REQUEST_BYTES:
                self.send_body(413, b"request too large\n", "text/plain")
                return
            try:
                payload = json.loads(self.rfile.read(length))
                if isinstance(payload, list):
                    if (
                        not payload
                        or len(payload) > MAX_BATCH_REQUESTS
                        or not all(isinstance(item, dict) for item in payload)
                    ):
                        raise ValueError("invalid batch")
                    response: Any = [policy.handle(item) for item in payload]
                elif isinstance(payload, dict):
                    response = policy.handle(payload)
                else:
                    raise ValueError("invalid request")
                body = json.dumps(response, separators=(",", ":")).encode()
                if len(body) > MAX_RPC_RESPONSE_BYTES:
                    self.send_body(413, b"response too large\n", "text/plain")
                    return
                self.send_body(200, body, "application/json")
            except (json.JSONDecodeError, ValueError):
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "parse error"},
                    },
                    separators=(",", ":"),
                ).encode()
                self.send_body(400, body, "application/json")

    return RpcHandler


def upstream_error_body(error: urllib.error.HTTPError) -> bytes:
    """Pass the shared cache's explorer-shaped error through; mask anything else."""
    with error:
        body = error.read(MAX_SOURCE_BYTES + 1)
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and "status" in parsed and len(body) <= MAX_SOURCE_BYTES:
        return body
    return explorer_error_body(f"source gateway upstream returned HTTP {error.code}")


def source_handler(
    upstream_url: str, chain_id: str, token: str = ""
) -> type[QuietHandler]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    seen_lock = threading.Lock()
    seen: set[str] = set()
    source_semaphore = threading.BoundedSemaphore(MAX_SOURCE_CONCURRENCY)

    class SourceHandler(QuietHandler):
        def do_GET(self) -> None:  # noqa: N802
            if urllib.parse.urlsplit(self.path).path == "/health":
                self.health()
                return
            try:
                address = validate_source_query(self.path, chain_id).lower()
                with seen_lock:
                    if address not in seen:
                        if len(seen) >= MAX_SOURCE_REQUESTS:
                            raise PolicyError("source request budget exhausted")
                        seen.add(address)
                request = source_upstream_request(upstream_url, chain_id, address, token)
                with source_semaphore:
                    with opener.open(
                        request, timeout=SOURCE_UPSTREAM_TIMEOUT_SECONDS
                    ) as response:
                        body = response.read(MAX_SOURCE_BYTES + 1)
                if len(body) > MAX_SOURCE_BYTES:
                    raise PolicyError("source response too large")
                self.send_body(200, body, "application/json")
            except PolicyError as error:
                self.send_body(403, explorer_error_body(error.message), "application/json")
            except urllib.error.HTTPError as error:
                self.send_body(error.code, upstream_error_body(error), "application/json")
            except (OSError, ValueError):
                self.send_body(
                    502,
                    explorer_error_body("source gateway unavailable"),
                    "application/json",
                )

        def do_POST(self) -> None:  # noqa: N802
            self.send_body(405, b"method not allowed\n", "text/plain")

        def do_CONNECT(self) -> None:  # noqa: N802
            self.send_body(405, b"method not allowed\n", "text/plain")

    return SourceHandler


def main() -> None:
    backend_url = os.environ.get("RPC_BACKEND_URL", "http://pier-anvil:8545")
    fork_block = int(os.environ["FORK_BLOCK_NUMBER"])
    source_upstream_url = os.environ["SOURCE_UPSTREAM_URL"]
    if not source_upstream_url.startswith(("http://", "https://")):
        raise ValueError("SOURCE_UPSTREAM_URL must be an http(s) URL")
    source_upstream_token = os.environ.get("SOURCE_UPSTREAM_TOKEN", "")
    explorer_chain_id = os.environ.get("EXPLORER_CHAIN_ID", "1")
    if not explorer_chain_id.isdigit():
        raise ValueError("EXPLORER_CHAIN_ID must be a decimal integer")
    rpc_port = int(os.environ.get("RPC_GATEWAY_PORT", "8545"))
    source_port = int(os.environ.get("SOURCE_GATEWAY_PORT", "8081"))

    policy = RpcPolicy(Backend(backend_url), fork_block)
    servers = [
        ThreadingHTTPServer(("0.0.0.0", rpc_port), rpc_handler(policy)),
        ThreadingHTTPServer(
            ("0.0.0.0", source_port),
            source_handler(
                source_upstream_url, explorer_chain_id, source_upstream_token
            ),
        ),
    ]
    threads = [threading.Thread(target=server.serve_forever) for server in servers]
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join()
    finally:
        for server in servers:
            server.shutdown()


if __name__ == "__main__":
    main()
