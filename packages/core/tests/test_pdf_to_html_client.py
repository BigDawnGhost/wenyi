"""MinerU client retry and validation tests (worklist-r2 X-3)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from wenyi_core.ingest import pdf_to_html
from wenyi_core.ingest.pdf_to_html import MinerUError, _request_with_retry


class FakeClient(httpx.Client):
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = list(responses)
        self.default = responses[-1]  # repeat the last response when exhausted
        self.calls = 0
        super().__init__(transport=httpx.MockTransport(self._respond))

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        item = self.responses.pop(0) if self.responses else self.default
        if isinstance(item, Exception):
            raise item
        return item


class TestRequestWithRetry(unittest.TestCase):
    def test_transient_5xx_retries_then_succeeds(self):
        sleeps: list[float] = []
        client = FakeClient(
            [
                httpx.Response(503, request=httpx.Request("GET", "https://x")),
                httpx.Response(500, request=httpx.Request("GET", "https://x")),
                httpx.Response(200, request=httpx.Request("GET", "https://x")),
            ]
        )
        with patch.object(pdf_to_html, "_sleep", sleeps.append):
            resp = _request_with_retry(client, "GET", "https://x")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(client.calls, 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_transport_error_retries_then_succeeds(self):
        client = FakeClient(
            [
                httpx.ConnectError("boom"),
                httpx.Response(200, request=httpx.Request("GET", "https://x")),
            ]
        )
        with patch.object(pdf_to_html, "_sleep", lambda s: None):
            resp = _request_with_retry(client, "GET", "https://x")
        self.assertEqual(resp.status_code, 200)

    def test_permanent_4xx_returns_without_retry(self):
        sleeps: list[float] = []
        client = FakeClient([httpx.Response(404, request=httpx.Request("GET", "https://x"))])
        with patch.object(pdf_to_html, "_sleep", sleeps.append):
            resp = _request_with_retry(client, "GET", "https://x")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(sleeps, [])

    def test_exhausted_retries_raise_readable_error(self):
        client = FakeClient([httpx.Response(503, request=httpx.Request("GET", "https://x"))])
        with patch.object(pdf_to_html, "_sleep", lambda s: None):
            with self.assertRaisesRegex(MinerUError, "after 3 attempts") as caught:
                _request_with_retry(client, "GET", "https://x")
        self.assertIn("transient HTTP 503", str(caught.exception.__cause__))

    def test_429_is_treated_as_transient(self):
        sleeps: list[float] = []
        client = FakeClient(
            [
                httpx.Response(429, request=httpx.Request("GET", "https://x")),
                httpx.Response(200, request=httpx.Request("GET", "https://x")),
            ]
        )
        with patch.object(pdf_to_html, "_sleep", sleeps.append):
            resp = _request_with_retry(client, "GET", "https://x")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(sleeps), 1)


if __name__ == "__main__":
    unittest.main()
