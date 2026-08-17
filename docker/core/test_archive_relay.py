from __future__ import annotations

import unittest

from archive_relay import forward_json_rpc


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self, _limit):
        return b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'


class FakeOpener:
    request = None
    timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return FakeResponse()


class ArchiveRelayTests(unittest.TestCase):
    def test_forwards_json_rpc(self):
        opener = FakeOpener()
        upstream_url = "https://node.invalid/v2/secret-token-value"
        payload = b'{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}'
        status, body = forward_json_rpc(upstream_url, payload, opener)
        self.assertEqual(status, 200)
        self.assertIn(b'"result":"0x1"', body)
        self.assertEqual(opener.request.full_url, upstream_url)
        self.assertEqual(opener.request.data, payload)
        self.assertEqual(opener.timeout, 120)

    def test_redacts_upstream_credential_from_response(self):
        class EchoOpener(FakeOpener):
            def open(self, request, timeout):
                class EchoResponse(FakeResponse):
                    def read(self, _limit):
                        return request.full_url.encode()

                return EchoResponse()

        upstream_url = "https://node.invalid/v2/secret-token-value"
        _status, body = forward_json_rpc(upstream_url, b"{}", EchoOpener())
        self.assertNotIn(b"secret-token-value", body)
        self.assertEqual(body, b"<redacted>")


if __name__ == "__main__":
    unittest.main()
