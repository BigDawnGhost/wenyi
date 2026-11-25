"""Retry and error-classification behavior of the BabelDOC bridge client."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import call, patch

import httpx
from wenyi_core.pdf_bridge import BabeldocBridgeClient
from wenyi_core.pdf_bridge.client import BabeldocBridgeError, BabeldocBridgeUnavailableError

SLEEP_TARGET = "wenyi_core.pdf_bridge.client._sleep"
BRIDGE_URL = "http://bridge.test"
HEALTH_URL = BRIDGE_URL + "/health"
EXTRACT_URL = BRIDGE_URL + "/extract"


def _with_request(response: httpx.Response, method: str, url: str) -> httpx.Response:
    response.request = httpx.Request(method, url)
    return response


def _health_response() -> httpx.Response:
    return _with_request(httpx.Response(200, json={"capabilities": []}), "GET", HEALTH_URL)


def _json_response(payload: dict[str, Any]) -> httpx.Response:
    return _with_request(httpx.Response(200, json=payload), "POST", EXTRACT_URL)


def _plain_response(status: int, body: bytes, method: str, url: str) -> httpx.Response:
    return _with_request(httpx.Response(status, content=body), method, url)


def _stream_response(
    chunks: list[bytes],
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    class ChunkStream(httpx.SyncByteStream):
        def __iter__(self):
            yield from chunks

    return httpx.Response(status, headers=headers or {}, stream=ChunkStream())


@contextmanager
def _stream_context(response: httpx.Response):
    try:
        yield response
    finally:
        response.close()


class BridgeRetryTests(unittest.TestCase):
    def _client(self, **kwargs: Any) -> BabeldocBridgeClient:
        return BabeldocBridgeClient(BRIDGE_URL, **kwargs)

    def _pdf(self, directory: str) -> Path:
        pdf = Path(directory) / "book.pdf"
        pdf.write_bytes(b"%PDF-1.4 synthetic")
        return pdf

    def test_health_retries_transient_failures_then_succeeds(self):
        client = self._client()
        with (
            patch("wenyi_core.pdf_bridge.client.httpx.get") as get,
            patch(SLEEP_TARGET) as sleep,
        ):
            get.side_effect = [
                httpx.ConnectError("refused"),
                httpx.ReadTimeout("slow"),
                _health_response(),
            ]
            payload = client.health()
        self.assertEqual(payload, {"capabilities": []})
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(0.5), call(1.0)])

    def test_health_server_error_is_retryable(self):
        client = self._client()
        with (
            patch("wenyi_core.pdf_bridge.client.httpx.get") as get,
            patch(SLEEP_TARGET) as sleep,
        ):
            get.side_effect = [
                _plain_response(503, b"unavailable", "GET", HEALTH_URL),
                _health_response(),
            ]
            payload = client.health()
        self.assertEqual(payload, {"capabilities": []})
        self.assertEqual(get.call_count, 2)
        self.assertEqual(sleep.call_args_list, [call(0.5)])

    def test_health_client_error_is_terminal(self):
        client = self._client()
        with (
            patch("wenyi_core.pdf_bridge.client.httpx.get") as get,
            patch(SLEEP_TARGET) as sleep,
        ):
            get.return_value = _plain_response(404, b"missing", "GET", HEALTH_URL)
            with self.assertRaises(BabeldocBridgeError) as raised:
                client.health()
        self.assertNotIsInstance(raised.exception, BabeldocBridgeUnavailableError)
        self.assertIn("Cannot connect to BabelDOC bridge", str(raised.exception))
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    def test_health_retries_exhausted_raises_unavailable(self):
        client = self._client()
        with (
            patch("wenyi_core.pdf_bridge.client.httpx.get") as get,
            patch(SLEEP_TARGET) as sleep,
        ):
            get.side_effect = httpx.ConnectError("down")
            with self.assertRaises(BabeldocBridgeUnavailableError) as raised:
                client.health()
        self.assertIn("Cannot connect to BabelDOC bridge", str(raised.exception))
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(0.5), call(1.0)])

    def test_extract_retries_connection_setup_then_succeeds(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as directory:
            pdf = self._pdf(directory)
            with (
                patch("wenyi_core.pdf_bridge.client.httpx.get", return_value=_health_response()),
                patch("wenyi_core.pdf_bridge.client.httpx.post") as post,
                patch(SLEEP_TARGET),
            ):
                post.side_effect = [
                    httpx.ConnectError("connection refused"),
                    httpx.ConnectTimeout("connection not established"),
                    _json_response({"session_id": "session-1", "paragraphs": {"paragraphs": []}}),
                ]
                payload = client.extract(pdf)
        self.assertEqual(payload["session_id"], "session-1")
        self.assertEqual(post.call_count, 3)

    def test_extract_client_error_is_terminal(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as directory:
            pdf = self._pdf(directory)
            with (
                patch("wenyi_core.pdf_bridge.client.httpx.get", return_value=_health_response()),
                patch("wenyi_core.pdf_bridge.client.httpx.post") as post,
                patch(SLEEP_TARGET) as sleep,
            ):
                post.return_value = _plain_response(400, b"bad request", "POST", EXTRACT_URL)
                with self.assertRaises(BabeldocBridgeError) as raised:
                    client.extract(pdf)
        self.assertNotIsInstance(raised.exception, BabeldocBridgeUnavailableError)
        self.assertIn("extract failed with HTTP 400", str(raised.exception))
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_extract_invalid_json_is_terminal_protocol_error(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as directory:
            pdf = self._pdf(directory)
            with (
                patch("wenyi_core.pdf_bridge.client.httpx.get", return_value=_health_response()),
                patch("wenyi_core.pdf_bridge.client.httpx.post") as post,
                patch(SLEEP_TARGET) as sleep,
            ):
                post.return_value = _plain_response(
                    200, b"<html>not json</html>", "POST", EXTRACT_URL
                )
                with self.assertRaises(BabeldocBridgeError) as raised:
                    client.extract(pdf)
        self.assertIn("extract request failed", str(raised.exception))
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_extract_does_not_replay_when_the_server_may_have_created_a_session(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as directory:
            pdf = self._pdf(directory)
            with (
                patch("wenyi_core.pdf_bridge.client.httpx.get", return_value=_health_response()),
                patch("wenyi_core.pdf_bridge.client.httpx.post") as post,
                patch(SLEEP_TARGET) as sleep,
            ):
                for failure in (
                    httpx.ReadTimeout("response lost"),
                    httpx.ReadError("disconnected"),
                    httpx.WriteError("upload interrupted"),
                    _plain_response(503, b"service failure", "POST", EXTRACT_URL),
                ):
                    with self.subTest(failure=failure):
                        post.reset_mock()
                        post.side_effect = [failure]
                        with self.assertRaises(BabeldocBridgeError) as raised:
                            client.extract(pdf)
                        self.assertFalse(raised.exception.retryable)
                        post.assert_called_once()
                        sleep.assert_not_called()

    def test_extract_network_exhaustion_raises_unavailable(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as directory:
            pdf = self._pdf(directory)
            with (
                patch("wenyi_core.pdf_bridge.client.httpx.get", return_value=_health_response()),
                patch("wenyi_core.pdf_bridge.client.httpx.post") as post,
                patch(SLEEP_TARGET),
            ):
                post.side_effect = httpx.ConnectError("down")
                with self.assertRaises(BabeldocBridgeUnavailableError) as raised:
                    client.extract(pdf)
        self.assertIn("extract request failed", str(raised.exception))
        self.assertEqual(post.call_count, 3)

    def test_fillback_retries_dropped_stream_then_writes_pdf(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.pdf"
            with (
                patch.object(client, "health", return_value={"capabilities": []}),
                patch("wenyi_core.pdf_bridge.client.httpx.stream") as stream,
                patch(SLEEP_TARGET) as sleep,
            ):
                stream.side_effect = [
                    httpx.ConnectError("refused"),
                    _stream_context(
                        _stream_response(
                            [b"%PDF-", b"body"],
                            headers={"X-Wenyi-PDF-Annotation-Stats": json.dumps({"annotated": 1})},
                        )
                    ),
                ]
                result = client.fillback("session", {"0:0": "translated"}, out_path=output)
            self.assertEqual(result, str(output))
            self.assertEqual(output.read_bytes(), b"%PDF-body")
            self.assertEqual(stream.call_count, 2)
            self.assertEqual(sleep.call_args_list, [call(0.5)])

    def test_fillback_client_error_is_terminal_and_keeps_previous_export(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.pdf"
            output.write_bytes(b"previous")
            with (
                patch.object(client, "health", return_value={"capabilities": []}),
                patch("wenyi_core.pdf_bridge.client.httpx.stream") as stream,
                patch(SLEEP_TARGET) as sleep,
            ):
                stream.return_value = _stream_context(_stream_response([], status=422))
                with self.assertRaises(BabeldocBridgeError) as raised:
                    client.fillback("session", {"0:0": "translated"}, out_path=output)
            self.assertNotIsInstance(raised.exception, BabeldocBridgeUnavailableError)
            self.assertIn("fillback failed with HTTP 422", str(raised.exception))
            self.assertEqual(stream.call_count, 1)
            sleep.assert_not_called()
            self.assertEqual(output.read_bytes(), b"previous")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_fillback_network_exhaustion_raises_unavailable(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.pdf"
            output.write_bytes(b"previous")
            with (
                patch.object(client, "health", return_value={"capabilities": []}),
                patch("wenyi_core.pdf_bridge.client.httpx.stream") as stream,
                patch(SLEEP_TARGET),
            ):
                stream.side_effect = httpx.ReadError("dropped")
                with self.assertRaises(BabeldocBridgeUnavailableError) as raised:
                    client.fillback("session", {"0:0": "translated"}, out_path=output)
            self.assertIn("fillback request failed", str(raised.exception))
            self.assertEqual(stream.call_count, 3)
            self.assertEqual(output.read_bytes(), b"previous")

    def test_max_attempts_is_honored(self):
        client = self._client(max_attempts=2)
        with (
            patch("wenyi_core.pdf_bridge.client.httpx.get") as get,
            patch(SLEEP_TARGET) as sleep,
        ):
            get.side_effect = httpx.ConnectError("down")
            with self.assertRaises(BabeldocBridgeUnavailableError):
                client.health()
        self.assertEqual(get.call_count, 2)
        self.assertEqual(sleep.call_args_list, [call(0.5)])

    def test_single_attempt_disables_retry(self):
        client = self._client(max_attempts=1)
        with (
            patch("wenyi_core.pdf_bridge.client.httpx.get") as get,
            patch(SLEEP_TARGET) as sleep,
        ):
            get.side_effect = httpx.ConnectError("down")
            with self.assertRaises(BabeldocBridgeUnavailableError):
                client.health()
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    def test_error_classification_flags(self):
        self.assertFalse(BabeldocBridgeError("terminal").retryable)
        self.assertTrue(BabeldocBridgeUnavailableError("transient").retryable)

    def test_invalid_max_attempts_rejected(self):
        with self.assertRaises(ValueError):
            BabeldocBridgeClient(BRIDGE_URL, max_attempts=0)


if __name__ == "__main__":
    unittest.main()
