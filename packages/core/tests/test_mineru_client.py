"""MinerU client retry and validation tests (worklist-r2 X-3)."""

from __future__ import annotations

import io
import json
import logging
import traceback
import unittest
from contextlib import ExitStack, closing
from unittest.mock import patch
from zipfile import ZipFile

import httpx
import pytest
from pypdf import PdfWriter
from wenyi_core.config import Config
from wenyi_core.ingest import mineru_api, pdf_reader
from wenyi_core.ingest.mineru_api import (
    MinerUApi,
    MinerUError,
    MinerUTimeoutError,
    _request_with_retry,
)
from wenyi_core.llm.providers.fake import FakeClient as FakeLLMClient
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.file import FileStorage

_TOKEN = "fake-mineru-token-DO-NOT-LOG"
_SIGNATURE = "fake-signature-DO-NOT-LOG"
_BATCH_ID = "fake-batch-DO-NOT-LOG"
_UPSTREAM_TEXT = "untrusted-upstream-text-DO-NOT-LOG"
_OBJECT_URL = f"https://objects.example.test/private-path?signature={_SIGNATURE}"
_REFLECTED = f"{_TOKEN}\n{_UPSTREAM_TEXT}\n{_OBJECT_URL}"


@pytest.fixture
def fake_http(monkeypatch):
    """Run both authenticated API and anonymous object requests through offline HTTPX."""
    client_type = httpx.Client
    calls = []
    with ExitStack() as stack:

        def install(handler):
            def dispatch(request):
                calls.append(request)
                return handler(request)

            transport = httpx.MockTransport(dispatch)
            anonymous = stack.enter_context(client_type(transport=transport))
            monkeypatch.setattr(httpx, "request", anonymous.request)
            monkeypatch.setattr(
                httpx,
                "Client",
                lambda *args, **kwargs: stack.enter_context(
                    client_type(*args, transport=transport, **kwargs)
                ),
            )
            monkeypatch.setattr(mineru_api, "_sleep", lambda _: None)
            return calls

        yield install


def _assert_safe(error, caplog):
    rendered = "".join(traceback.format_exception(error))
    for text in (str(error), repr(error), rendered, caplog.text):
        for marker in (_TOKEN, _SIGNATURE, _UPSTREAM_TEXT, _BATCH_ID, "private-path"):
            assert marker not in text
    assert error.__cause__ is None
    assert error.__context__ is None


def _upload_data():
    return {"batch_id": _BATCH_ID, "file_urls": [_OBJECT_URL]}


def _poll_data():
    return {
        "extract_result": [{"file_name": "book.pdf", "state": "done", "full_zip_url": _OBJECT_URL}]
    }


def _write_pdf(path):
    with PdfWriter() as writer:
        writer.add_blank_page(width=200, height=200)
        writer.write(path)


def test_prepare_missing_batch_id_cannot_expose_upload_urls(fake_http, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    calls = fake_http(
        lambda _: httpx.Response(200, json={"code": 0, "data": {"file_urls": [_OBJECT_URL]}})
    )
    source = tmp_path / "book.pdf"
    _write_pdf(source)
    store = FileStorage(str(tmp_path / "state"))
    config = Config.from_dict({"language": {"source": "en", "target": "zh"}})
    orchestrator = Orchestrator(
        config, client=FakeLLMClient(), storage=store, mineru_token_resolver=lambda: _TOKEN
    )

    with pytest.raises(MinerUError, match="missing 'batch_id'") as caught:
        orchestrator.prepare(str(source))

    _assert_safe(caught.value, caplog)
    assert [call.method for call in calls] == ["POST"]
    assert not store.exists()
    for path in (tmp_path / "state").rglob("*"):
        if path.is_file():
            assert _TOKEN.encode() not in path.read_bytes()
            assert _SIGNATURE.encode() not in path.read_bytes()


@pytest.mark.parametrize(
    ("stage", "failure", "reason", "attempts"),
    [
        ("batch creation", httpx.Response(200, text=_REFLECTED), "non-JSON body", 1),
        ("batch creation", httpx.Response(401, text=_REFLECTED), "HTTP 401", 1),
        (
            "batch creation",
            httpx.Response(200, json={"code": {"detail": _REFLECTED}, "msg": _REFLECTED}),
            "invalid 'code'",
            1,
        ),
        ("upload", httpx.Response(403, text=_REFLECTED), "HTTP 403", 1),
        ("upload", httpx.Response(503, text=_REFLECTED), "HTTP 503 after 3 attempts", 3),
        ("upload", httpx.ConnectError(_REFLECTED), "transport failure after 3 attempts", 3),
        ("upload", httpx.InvalidURL(_REFLECTED), "invalid HTTP request", 1),
        ("batch creation", httpx.LocalProtocolError(_REFLECTED), "invalid HTTP request", 1),
        ("polling", httpx.Response(200, text=_REFLECTED), "non-JSON body", 1),
        (
            "polling",
            httpx.Response(200, json={"code": 12, "msg": _REFLECTED, "data": _poll_data()}),
            "API reported failure",
            1,
        ),
        (
            "polling",
            httpx.Response(200, json={"data": {"extract_result": [{"file_name": _REFLECTED}]}}),
            "missing 'state'",
            1,
        ),
        (
            "polling",
            httpx.Response(
                200,
                json={"data": {"extract_result": [{"file_name": [_REFLECTED], "state": "done"}]}},
            ),
            "invalid 'file_name'",
            1,
        ),
        (
            "polling",
            httpx.Response(
                200,
                json={
                    "data": {
                        "extract_result": [
                            {"file_name": "book.pdf", "state": "failed", "err_msg": _REFLECTED}
                        ]
                    }
                },
            ),
            "extraction failed for file 1",
            1,
        ),
        ("polling", httpx.ReadTimeout(_REFLECTED), "timeout after 3 attempts", 3),
        ("download", httpx.Response(403, text=_REFLECTED), "HTTP 403", 1),
        ("download", httpx.DecodingError(_REFLECTED), "invalid HTTP response", 1),
        ("download", httpx.TooManyRedirects(_REFLECTED), "too many redirects", 1),
        ("download", RuntimeError(_REFLECTED), "HTTP client failure", 1),
        ("download", MinerUError(_REFLECTED), "HTTP client failure", 1),
    ],
)
def test_external_failures_expose_only_safe_diagnostics(
    fake_http, tmp_path, caplog, stage, failure, reason, attempts
):
    caplog.set_level(logging.DEBUG)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fake PDF")
    failures = 0

    def respond(request):
        nonlocal failures
        if request.method == "POST":
            current = "batch creation"
            success = {"code": 0, "data": _upload_data()}
        elif request.method == "PUT":
            current = "upload"
            success = None
        elif request.url.host == "mineru.net":
            current = "polling"
            success = {"code": 0, "data": _poll_data()}
        else:
            current = "download"
            success = None
        if current != stage:
            return httpx.Response(200, json=success)
        failures += 1
        if isinstance(failure, Exception):
            raise failure
        return failure

    fake_http(respond)
    with closing(MinerUApi(_TOKEN)) as client:
        with pytest.raises(MinerUError) as caught:
            client.submit_batch([str(source)])

    _assert_safe(caught.value, caplog)
    assert f"MinerU {stage}" in str(caught.value)
    assert reason in str(caught.value)
    assert failures == attempts


def test_poll_deadline_does_not_report_the_batch_id(fake_http, tmp_path, caplog):
    def respond(request):
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": _upload_data()})
        if request.method == "PUT":
            return httpx.Response(200)
        return httpx.Response(
            200,
            json={"data": {"extract_result": [{"state": "running", "err_msg": _REFLECTED}]}},
        )

    fake_http(respond)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fake PDF")
    caplog.set_level(logging.DEBUG)
    with closing(MinerUApi(_TOKEN)) as client:
        with pytest.raises(MinerUTimeoutError, match="polling timed out") as caught:
            client.submit_batch([str(source)], timeout=-1)
    _assert_safe(caught.value, caplog)


def _serve_result(fake_http, content):
    def respond(request):
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": _upload_data()})
        if request.method == "PUT":
            return httpx.Response(200)
        if request.url.host == "mineru.net":
            return httpx.Response(200, json={"data": _poll_data()})
        return httpx.Response(200, content=content)

    return fake_http(respond)


def _bundle(block, **members):
    result = io.BytesIO()
    with ZipFile(result, "w") as archive:
        archive.writestr("book_content_list_v2.json", json.dumps([[block]]))
        for name, data in members.items():
            archive.writestr(name, data)
    return result.getvalue()


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (_REFLECTED.encode(), "PDF conversion failed"),
        (_bundle({"type": _REFLECTED, "content": {}}), "PDF conversion failed"),
        (
            _bundle(
                {"type": "image", "content": {"image_source": {"path": "scan.gif"}}},
                **{"scan.gif": b"GIF89a"},
            ),
            "no text from the PDF.*OCR",
        ),
    ],
    ids=["not-a-zip", "invalid-block", "image-only"],
)
def test_downloaded_result_errors_are_safe_but_keep_local_guidance(
    fake_http, tmp_path, caplog, result, reason
):
    caplog.set_level(logging.DEBUG)
    _serve_result(fake_http, result)
    source = tmp_path / "book.pdf"
    _write_pdf(source)
    cache = tmp_path / "cache"

    with pytest.raises(MinerUError, match=reason) as caught:
        pdf_reader.read_pdf(str(source), "en", "zh", cache_dir=str(cache), api_token=_TOKEN)

    _assert_safe(caught.value, caplog)
    assert not list(cache.rglob("*.epub"))


def test_derived_epub_parser_failure_cannot_expose_upstream_text(monkeypatch, tmp_path, caplog):
    def convert(_path, output, **kwargs):
        output.write_bytes(b"fake derived EPUB")

    def parse(*args):
        raise ValueError(_REFLECTED)

    monkeypatch.setattr(pdf_reader, "convert_pdf_to_epub", convert)
    monkeypatch.setattr(pdf_reader, "read_epub", parse)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fake PDF")
    cache = tmp_path / "cache"
    with pytest.raises(MinerUError, match="could not read the MinerU source EPUB") as caught:
        pdf_reader.read_pdf(str(source), "en", "zh", cache_dir=str(cache), api_token=_TOKEN)
    _assert_safe(caught.value, caplog)
    assert not list(cache.rglob("*.epub"))


def test_formula_fallback_does_not_log_returned_latex(fake_http, tmp_path, caplog):
    source = tmp_path / "book.pdf"
    _write_pdf(source)
    calls = _serve_result(
        fake_http,
        _bundle(
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [
                        {"type": "text", "content": "A paragraph with a formula."},
                        {
                            "type": "equation_inline",
                            "content": rf"\unknowncommand {_UPSTREAM_TEXT}",
                        },
                    ]
                },
            }
        ),
    )
    caplog.set_level(logging.DEBUG)
    document = pdf_reader.read_pdf(
        str(source), "en", "zh", cache_dir=str(tmp_path / "cache"), api_token=_TOKEN
    )

    assert document.chapters
    assert "LaTeX text fallback" in caplog.text
    assert _UPSTREAM_TEXT not in caplog.text
    assert _SIGNATURE not in caplog.text
    assert [request.headers.get("authorization") for request in calls] == [
        f"Bearer {_TOKEN}",
        None,
        f"Bearer {_TOKEN}",
        None,
    ]


def test_transport_debug_logs_are_suppressed_only_during_mineru_requests(
    fake_http, tmp_path, caplog
):
    logger = logging.getLogger("httpcore.http11")
    caplog.set_level(logging.DEBUG)

    def respond(request):
        logger.debug("received untrusted headers: %s %s", request.headers, _REFLECTED)
        return httpx.Response(401, text=_REFLECTED)

    fake_http(respond)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fake PDF")
    with closing(MinerUApi(_TOKEN)) as client:
        with pytest.raises(MinerUError, match="HTTP 401") as caught:
            client.submit_batch([str(source)])

    _assert_safe(caught.value, caplog)
    logger.debug("unrelated HTTP request")
    assert "unrelated HTTP request" in caplog.text


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
            resp = _request_with_retry(client, "GET", "https://x", operation="download")
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
            resp = _request_with_retry(client, "GET", "https://x", operation="download")
        self.assertEqual(resp.status_code, 200)

    def test_permanent_4xx_fails_without_retry(self):
        sleeps: list[float] = []
        client = FakeClient([httpx.Response(404, request=httpx.Request("GET", "https://x"))])
        with patch.object(mineru_api, "_sleep", sleeps.append):
            with pytest.raises(MinerUError, match="HTTP 404"):
                _request_with_retry(client, "GET", "https://x", operation="download")
        self.assertEqual(sleeps, [])

    def test_429_is_treated_as_transient(self):
        sleeps: list[float] = []
        client = FakeClient(
            [
                httpx.Response(429, request=httpx.Request("GET", "https://x")),
                httpx.Response(200, request=httpx.Request("GET", "https://x")),
            ]
        )
        with patch.object(mineru_api, "_sleep", sleeps.append):
            resp = _request_with_retry(client, "GET", "https://x", operation="download")
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


@pytest.mark.parametrize(
    ("upload", "poll", "reason"),
    [
        ([_REFLECTED], None, "not a JSON object"),
        ({"code": 0, "msg": _REFLECTED}, None, "missing 'data'"),
        (None, [_REFLECTED], "not a JSON object"),
        (None, {"data": {"msg": _REFLECTED}}, "missing 'data.extract_result'"),
        (
            None,
            {
                "data": {
                    "extract_result": [
                        {"file_name": "test.pdf", "state": "done", "msg": _REFLECTED}
                    ]
                }
            },
            "missing 'full_zip_url'",
        ),
    ],
    ids=[
        "upload-array",
        "upload-missing-data",
        "poll-array",
        "poll-missing-results",
        "missing-zip",
    ],
)
def test_malformed_envelopes_name_only_the_invalid_field(
    monkeypatch, tmp_path, caplog, upload, poll, reason
):
    with closing(_script_batch(monkeypatch, tmp_path, poll, upload_body=upload)) as client:
        with pytest.raises(MinerUError) as caught:
            client.submit_batch([str(tmp_path / "test.pdf")])
    _assert_safe(caught.value, caplog)
    assert reason in str(caught.value)


if __name__ == "__main__":
    unittest.main()
