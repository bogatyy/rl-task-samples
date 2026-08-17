#!/usr/bin/env python3
"""Credential-hiding localhost relay for Anvil's archive backend."""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def upstream_secrets(upstream_url: str) -> tuple[bytes, ...]:
    parsed = urllib.parse.urlsplit(upstream_url)
    secret_parts = [upstream_url.encode()]
    path_secret = parsed.path.rsplit("/", 1)[-1]
    if len(path_secret) >= 16:
        secret_parts.append(path_secret.encode())
    return tuple(secret_parts)


def forward_json_rpc(upstream_url: str, payload: bytes, opener) -> tuple[int, bytes]:
    request = urllib.request.Request(
        upstream_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=120) as response:
            status = response.status
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        status = error.code
        body = error.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("archive response too large")
    for secret in upstream_secrets(upstream_url):
        body = body.replace(secret, b"<redacted>")
    return status, body


def relay_handler(upstream_url: str):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    class ArchiveRelayHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/health":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400, "invalid content length")
                return
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self.send_error(413, "request too large")
                return

            try:
                status, body = forward_json_rpc(
                    upstream_url, self.rfile.read(length), opener
                )
            except Exception:
                self.send_error(502, "archive backend unavailable")
                return
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return ArchiveRelayHandler


def serve(upstream_url: str, host: str, port: int, port_file: str = "") -> None:
    server = ThreadingHTTPServer((host, port), relay_handler(upstream_url))
    server.daemon_threads = True
    if port_file:
        Path(port_file).write_text(str(server.server_port), encoding="ascii")
    server.serve_forever()


if __name__ == "__main__":
    upstream_url = os.environ.get("ARCHIVE_RPC_URL")
    if upstream_url is None:
        upstream_url = sys.stdin.readline().rstrip("\r\n")
    if not upstream_url:
        raise SystemExit("archive RPC URL is required")
    serve(
        upstream_url,
        os.environ.get("ARCHIVE_RELAY_HOST", "127.0.0.1"),
        int(os.environ.get("ARCHIVE_RELAY_PORT", "8546")),
        os.environ.get("ARCHIVE_RELAY_PORT_FILE", ""),
    )
