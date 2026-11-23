"""MinerU client retry and validation tests (worklist-r2 X-3)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
import pytest
from wenyi_core.ingest import mineru_api
from wenyi_core.ingest.mineru_api import MinerUApi, MinerUError, _request_with_retry


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
        with patch.object(mineru_api, "_sleep", sleeps.append):
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
        with patch.object(mineru_api, "_sleep", lambda s: None):
            resp = _request_with_retry(client, "GET", "https://x")
        self.assertEqual(resp.status_code, 200)

    def test_permanent_4xx_returns_without_retry(self):
        sleeps: list[float] = []
        client = FakeClient([httpx.Response(404, request=httpx.Request("GET", "https://x"))])
        with patch.object(mineru_api, "_sleep", sleeps.append):
            resp = _request_with_retry(client, "GET", "https://x")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(sleeps, [])

    def test_exhausted_retries_raise_readable_error(self):
        client = FakeClient([httpx.Response(503, request=httpx.Request("GET", "https://x"))])
        with patch.object(mineru_api, "_sleep", lambda s: None):
            with pytest.raises(MinerUError, match="after 3 attempts") as caught:
                _request_with_retry(client, "GET", "https://x")
        self.assertIn("transient HTTP 503", str(caught.value.__cause__))

    def test_429_is_treated_as_transient(self):
        sleeps: list[float] = []
        client = FakeClient(
            [
                httpx.Response(429, request=httpx.Request("GET", "https://x")),
                httpx.Response(200, request=httpx.Request("GET", "https://x")),
            ]
        )
        with patch.object(mineru_api, "_sleep", sleeps.append):
            resp = _request_with_retry(client, "GET", "https://x")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(sleeps), 1)


def test_batch_requests_standard_result_without_html(tmp_path, monkeypatch):
    source = tmp_path / "test.pdf"
    source.write_bytes(b"pdf bytes")
    result_url = "https://result.example.test/result.zip"
    upload_url = "https://upload.example.test/object"
    got = []

    def request(_client, method, url, **kwargs):
        got.append((method, url))
        request = httpx.Request(
            method, url if url.startswith("https:") else mineru_api.API_BASE + url
        )
        if method == "POST":
            assert kwargs["json"] == {
                "files": [{"name": "test.pdf"}],
                "model_version": "vlm",
            }
            body = {"code": 0, "data": {"file_urls": [upload_url], "batch_id": "batch"}}
        elif url == upload_url:
            assert kwargs["content"] == source.read_bytes()
            return httpx.Response(200, request=request)
        elif url == result_url:
            return httpx.Response(200, content=b"standard result ZIP", request=request)
        else:
            body = {
                "data": {
                    "extract_result": [
                        {"file_name": "test.pdf", "state": "done", "full_zip_url": result_url}
                    ]
                }
            }
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(mineru_api, "_request_with_retry", request)
    client = MinerUApi("test-token")
    try:
        assert client.submit_batch([str(source)]) == [b"standard result ZIP"]
    finally:
        client.close()
    assert got == [
        ("POST", "/file-urls/batch"),
        ("PUT", upload_url),
        ("GET", "/extract-results/batch/batch"),
        ("GET", result_url),
    ]


def _script_batch(monkeypatch, tmp_path, poll_body, upload_body=None):
    """Script one submit_batch run; every poll response returns poll_body."""
    source = tmp_path / "test.pdf"
    source.write_bytes(b"pdf bytes")
    upload = upload_body or {
        "code": 0,
        "data": {"batch_id": "batch", "file_urls": ["https://upload.example.test/object"]},
    }

    def request(_client, method, url, **kwargs):
        http_request = httpx.Request(
            method, url if url.startswith("https:") else mineru_api.API_BASE + url
        )
        if method == "POST":
            return httpx.Response(200, json=upload, request=http_request)
        if method == "PUT":
            return httpx.Response(200, request=http_request)
        if isinstance(poll_body, httpx.Response):
            poll_body.request = http_request
            return poll_body
        return httpx.Response(200, json=poll_body, request=http_request)

    monkeypatch.setattr(mineru_api, "_request_with_retry", request)
    return MinerUApi("test-token")


def test_poll_missing_extract_result_reports_server_reason(monkeypatch, tmp_path):
    client = _script_batch(monkeypatch, tmp_path, {"code": 1, "msg": "task expired"})
    try:
        with pytest.raises(MinerUError) as caught:
            client.submit_batch([str(tmp_path / "test.pdf")])
    finally:
        client.close()
    message = str(caught.value)
    assert "missing 'data.extract_result'" in message
    assert "task expired" in message


def test_poll_non_object_body_reports_snippet(monkeypatch, tmp_path):
    client = _script_batch(monkeypatch, tmp_path, ["unexpected"])
    try:
        with pytest.raises(MinerUError) as caught:
            client.submit_batch([str(tmp_path / "test.pdf")])
    finally:
        client.close()
    message = str(caught.value)
    assert "missing 'data.extract_result'" in message
    assert "unexpected" in message


def test_poll_non_json_body_is_reported(monkeypatch, tmp_path):
    client = _script_batch(
        monkeypatch, tmp_path, httpx.Response(200, text="<html>gateway timeout</html>")
    )
    try:
        with pytest.raises(MinerUError, match="non-JSON body") as caught:
            client.submit_batch([str(tmp_path / "test.pdf")])
    finally:
        client.close()
    assert "gateway timeout" in str(caught.value)


def test_poll_result_missing_state_is_reported(monkeypatch, tmp_path):
    poll = {"data": {"extract_result": [{"file_name": "test.pdf"}]}}
    client = _script_batch(monkeypatch, tmp_path, poll)
    try:
        with pytest.raises(MinerUError) as caught:
            client.submit_batch([str(tmp_path / "test.pdf")])
    finally:
        client.close()
    message = str(caught.value)
    assert "test.pdf" in message
    assert "missing 'state'" in message


def test_done_result_missing_full_zip_url_is_reported(monkeypatch, tmp_path):
    poll = {"data": {"extract_result": [{"file_name": "test.pdf", "state": "done"}]}}
    client = _script_batch(monkeypatch, tmp_path, poll)
    try:
        with pytest.raises(MinerUError) as caught:
            client.submit_batch([str(tmp_path / "test.pdf")])
    finally:
        client.close()
    message = str(caught.value)
    assert "test.pdf" in message
    assert "missing 'full_zip_url'" in message


def test_failed_result_carries_server_reason(monkeypatch, tmp_path):
    poll = {
        "data": {
            "extract_result": [
                {"file_name": "test.pdf", "state": "failed", "err_msg": "OCR backend crashed"}
            ]
        }
    }
    client = _script_batch(monkeypatch, tmp_path, poll)
    try:
        with pytest.raises(
            MinerUError, match=r"Extraction failed for test\.pdf: OCR backend crashed"
        ):
            client.submit_batch([str(tmp_path / "test.pdf")])
    finally:
        client.close()


def test_upload_response_missing_batch_id_is_reported(monkeypatch, tmp_path):
    upload = {"code": 0, "data": {"file_urls": ["https://upload.example.test/object"]}}
    client = _script_batch(monkeypatch, tmp_path, {}, upload_body=upload)
    try:
        with pytest.raises(MinerUError, match="missing 'batch_id'"):
            client.submit_batch([str(tmp_path / "test.pdf")])
    finally:
        client.close()


def test_upload_response_missing_data_is_reported(monkeypatch, tmp_path):
    client = _script_batch(monkeypatch, tmp_path, {}, upload_body={"code": 0})
    try:
        with pytest.raises(MinerUError, match="missing 'data'"):
            client.submit_batch([str(tmp_path / "test.pdf")])
    finally:
        client.close()


def test_upload_response_non_object_is_reported(monkeypatch, tmp_path):
    client = _script_batch(monkeypatch, tmp_path, {}, upload_body=["weird"])
    try:
        with pytest.raises(MinerUError) as caught:
            client.submit_batch([str(tmp_path / "test.pdf")])
    finally:
        client.close()
    message = str(caught.value)
    assert "not a JSON object" in message
    assert "weird" in message


if __name__ == "__main__":
    unittest.main()
