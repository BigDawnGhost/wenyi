"""MinerU client retry and validation tests (worklist-r2 X-3)."""

from __future__ import annotations

import io
import json
import logging
import traceback
import tracemalloc
import unittest
import zipfile
from contextlib import ExitStack, closing
from pathlib import Path
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
from wenyi_core.ingest.mineru_epub import render_mineru_results
from wenyi_core.llm.providers.fake import FakeClient as FakeLLMClient
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.file import FileStorage

_TOKEN = "fake-mineru-token-DO-NOT-LOG"
_SIGNATURE = "fake-signature-DO-NOT-LOG"
_BATCH_ID = "fake-batch-DO-NOT-LOG"
_UPSTREAM_TEXT = "untrusted-upstream-text-DO-NOT-LOG"
_OBJECT_URL = f"https://objects.example.test/private-path?signature={_SIGNATURE}"
_REFLECTED = f"{_TOKEN}\n{_UPSTREAM_TEXT}\n{_OBJECT_URL}"


class StreamedResponse(httpx.Response):
    """Response body delivered in parts; buffering it is a contract break.

    The parts are handed to a byte stream rather than a buffered body, so iter_bytes()
    yields them one at a time and read()/.content on the received response would mean
    the downloader kept the whole body instead of writing it out.
    """

    def __init__(self, chunks: list[bytes], **kwargs) -> None:
        self.parts = _Parts(chunks)
        kwargs["stream"] = self.parts
        super().__init__(**kwargs)

    def read(self) -> bytes:
        raise AssertionError("the result body was buffered instead of streamed")

    @property
    def content(self) -> bytes:
        raise AssertionError("the result body was buffered instead of streamed")


class _Parts(httpx.SyncByteStream):
    """A response stream that yields recorded parts and records how often it ran."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.iterations = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.iterations += 1
            yield chunk


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
            monkeypatch.setattr(httpx, "stream", anonymous.stream)
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
            client.submit_batch([str(source)], tmp_path)

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
            client.submit_batch([str(source)], tmp_path, timeout=-1)
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
            client.submit_batch([str(source)], tmp_path)

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

    def test_destination_write_failure_is_not_described_as_an_http_failure(self):
        class FullDisk(io.BytesIO):
            def write(self, data):
                raise OSError("disk full")

        client = FakeClient([httpx.Response(200, content=b"result ZIP")])
        with pytest.raises(OSError, match="disk full"):
            _request_with_retry(client, "GET", "https://x", operation="download", sink=FullDisk())
        self.assertEqual(client.calls, 1)

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
    uploaded = []

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
            with kwargs["body"]() as handle:
                uploaded.append(handle.read())
            return httpx.Response(200, request=request)
        elif url == result_url:
            body = StreamedResponse(
                [b"standard ", b"result ", b"ZIP"], status_code=200, request=request
            )
            for chunk in body.iter_bytes():
                kwargs["sink"].write(chunk)
            return body
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
        downloaded = client.submit_batch([str(source)], tmp_path)
    finally:
        client.close()
    assert uploaded == [source.read_bytes()]
    assert downloaded == [tmp_path / "result-1.zip"]
    assert downloaded[0].read_bytes() == b"standard result ZIP"
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
            client.submit_batch([str(tmp_path / "test.pdf")], tmp_path)
    _assert_safe(caught.value, caplog)
    assert reason in str(caught.value)


def test_download_streams_the_result_body_to_disk(fake_http, tmp_path):
    """The result ZIP is written chunk by chunk; the body is never held whole."""
    payload = b"".join(b"part-%03d" % index for index in range(1, 41))
    captured = []

    def respond(request):
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": _upload_data()})
        if request.method == "PUT":
            return httpx.Response(200)
        if request.url.host == "mineru.net":
            return httpx.Response(200, json={"data": _poll_data()})
        response = StreamedResponse(
            [payload[:400], payload[400:]], status_code=200, request=request
        )
        captured.append(response)
        return response

    fake_http(respond)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fake PDF")
    with closing(MinerUApi(_TOKEN)) as client:
        downloaded = client.submit_batch([str(source)], tmp_path)

    assert downloaded == [tmp_path / "result-1.zip"]
    assert downloaded[0].read_bytes() == payload
    (response,) = captured
    # The body went straight to the destination file: no buffered copy was created.
    assert not hasattr(response, "_content")
    assert response.parts.iterations == 2


def test_retried_upload_reopens_the_file_for_each_attempt(fake_http, tmp_path, monkeypatch):
    """Every attempt streams from its own handle, so the retry sends the full body."""
    payload = b"synthetic upload payload"
    source = tmp_path / "book.pdf"
    source.write_bytes(payload)
    attempts = []
    handles = []
    original_open = Path.open

    def tracking_open(self, *args, **kwargs):
        handle = original_open(self, *args, **kwargs)
        if self == source:
            handles.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracking_open)

    def respond(request):
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": _upload_data()})
        if request.method == "PUT":
            attempts.append(bytes(request.content))
            if len(attempts) == 1:
                raise httpx.ConnectError("reset")
            return httpx.Response(200)
        if request.url.host == "mineru.net":
            return httpx.Response(200, json={"data": _poll_data()})
        return httpx.Response(200, content=b"result ZIP")

    fake_http(respond)
    with closing(MinerUApi(_TOKEN)) as client:
        downloaded = client.submit_batch([str(source)], tmp_path)

    assert attempts == [payload, payload]
    assert len(handles) == 2 and all(handle.closed for handle in handles)
    assert [path.read_bytes() for path in downloaded] == [b"result ZIP"]


def _synthetic_bundle(path, text, filler):
    blocks = [
        [
            {
                "type": "paragraph",
                "content": {"paragraph_content": [{"type": "text", "content": text}]},
            }
        ]
    ]
    with ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("book_content_list_v2.json", json.dumps(blocks))
        archive.writestr("book/full.md", filler)
    return path


def _render_peak(filler, count, root):
    """Render count synthetic bundles and report the on-disk size and traced peak."""
    bundles = [
        _synthetic_bundle(root / f"result-{index}.zip", f"Chunk {index}.", filler)
        for index in range(count)
    ]
    total = sum(path.stat().st_size for path in bundles)
    tracemalloc.start()
    try:
        render_mineru_results(bundles, root / "source.epub", title="Synthetic book", lang="en")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return total, peak


def test_render_peak_memory_does_not_follow_the_result_set(tmp_path):
    """Rendering reads one archive at a time, so the peak tracks one chunk, not the book."""
    filler = bytes(range(256)) * (10 * 1024)
    small_root, large_root = tmp_path / "small", tmp_path / "large"
    small_root.mkdir()
    large_root.mkdir()
    small_total, small_peak = _render_peak(filler, 2, small_root)
    large_total, large_peak = _render_peak(filler, 8, large_root)

    # Four times the result payload must not mean four times the resident archive data.
    assert large_total >= small_total * 3.5
    assert large_peak < large_total / 4
    assert large_peak < small_peak * 2


def test_render_reads_bundles_without_keeping_them_whole(tmp_path, monkeypatch):
    """Only the chunk being rendered is open; earlier archives are already closed."""
    filler = bytes(range(256)) * (2 * 1024)
    bundles = [
        _synthetic_bundle(tmp_path / f"result-{index}.zip", f"Chunk {index}.", filler)
        for index in range(3)
    ]
    opened = []
    closed = []
    real_archive = ZipFile

    class TrackedArchive:
        def __init__(self, path):
            self._archive = real_archive(path)
            opened.append(path)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self._archive.close()
            closed.append(True)

        def namelist(self):
            return self._archive.namelist()

        def read(self, name):
            return self._archive.read(name)

    monkeypatch.setattr("wenyi_core.ingest.mineru_epub.ZipFile", TrackedArchive)
    output = tmp_path / "source.epub"
    render_mineru_results(bundles, output, title="Synthetic book", lang="en")

    assert output.is_file()
    assert opened == bundles
    assert len(closed) == len(bundles)


def test_interrupted_conversion_leaves_no_reusable_cache(fake_http, tmp_path, monkeypatch):
    """An interrupted render publishes nothing and keeps no downloaded bundle."""
    _serve_result(fake_http, _bundle(_paragraph_block()))
    source = tmp_path / "book.pdf"
    _write_pdf(source)
    cache = tmp_path / "cache"
    received = []

    def interrupted(results, output, *, title, lang):
        received.extend(results)
        assert all(path.is_file() for path in received)
        output.write_bytes(b"partial derived EPUB")
        raise MinerUError("conversion interrupted")

    monkeypatch.setattr("wenyi_core.ingest.mineru_epub.render_mineru_results", interrupted)
    with pytest.raises(MinerUError, match="conversion interrupted"):
        pdf_reader.read_pdf(str(source), "en", "zh", cache_dir=str(cache), api_token=_TOKEN)

    assert received
    assert not list(cache.rglob("*.epub"))
    assert not list(cache.rglob("*.zip"))
    assert not any(path.exists() for path in received)


def _paragraph_block():
    return {
        "type": "paragraph",
        "content": {"paragraph_content": [{"type": "text", "content": "Synthetic text."}]},
    }


if __name__ == "__main__":
    unittest.main()
