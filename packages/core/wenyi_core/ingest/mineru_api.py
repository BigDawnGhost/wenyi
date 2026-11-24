"""Retrieve ordered MinerU Precision API result bundles for a PDF."""

from __future__ import annotations

import logging
import tempfile
import time
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

import httpx
from pypdf import PdfReader, PdfWriter

from wenyi_core.markup import _epub_native
from wenyi_core.models.errors import MinerUError, MinerUTimeoutError

API_BASE = "https://mineru.net/api/v4"
MAX_PAGES = 200
POLL_INTERVAL = 3.0
MAX_WAIT_PER_TASK = 600

# Transient-failure retry budget for single HTTP calls (audit X-3): a momentary 5xx or
# transport error must not abort a batch the service is still processing.
_TRANSIENT_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 1.0
_sleep = time.sleep

_Operation = Literal["batch creation", "upload", "polling", "download"]
_private_request = ContextVar("mineru_private_request", default=False)


def _non_mineru_log(_record: logging.LogRecord) -> bool:
    return not _private_request.get()


# HTTPX logs full URLs, and httpcore's traces include response headers and
# exceptions. Logger filters are not inherited, so filter their emitting loggers.
for _logger_name in (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
):
    logging.getLogger(_logger_name).addFilter(_non_mineru_log)


@contextmanager
def _private_http():
    scope = _private_request.set(True)
    try:
        yield
    finally:
        _private_request.reset(scope)


def _request_with_retry(
    client: httpx.Client | None,
    method: str,
    url: str,
    *,
    operation: _Operation,
    timeout: httpx.Timeout | None = None,
    **kwargs,
) -> httpx.Response:
    """Return a successful response, or report the stage and a safe failure reason.

    ``client=None`` keeps API authorization out of pre-signed object requests.
    """
    sender = client.request if client is not None else httpx.request
    if timeout is not None:
        kwargs["timeout"] = timeout
    for attempt in range(1, _TRANSIENT_ATTEMPTS + 1):
        retry = False
        try:
            with _private_http():
                response = sender(method, url, **kwargs)
        except (httpx.InvalidURL, httpx.LocalProtocolError, httpx.UnsupportedProtocol):
            reason = "invalid HTTP request"
        except httpx.TooManyRedirects:
            reason = "too many redirects"
        except httpx.DecodingError:
            reason = "invalid HTTP response"
        except httpx.TimeoutException:
            reason, retry = "timeout", True
        except httpx.TransportError:
            reason, retry = "transport failure", True
        except Exception:
            reason = "HTTP client failure"
        else:
            status = response.status_code
            if type(status) is not int or not 100 <= status < 600:
                reason = "invalid HTTP status"
            elif 200 <= status < 300:
                return response
            else:
                reason = f"HTTP {status}"
                retry = status == 429 or status >= 500
        if not retry or attempt == _TRANSIENT_ATTEMPTS:
            break
        _sleep(_RETRY_BACKOFF_SECONDS * attempt)
    attempts = f" after {attempt} attempts" if attempt > 1 else ""
    # Raise outside the handler: raw HTTP exceptions may retain requests and responses.
    raise MinerUError(f"MinerU {operation} failed: {reason}{attempts}") from None


# ── API client ──


class MinerUApi:
    """Thin HTTP client for MinerU Precision API (v4)."""

    def __init__(self, token: str) -> None:
        try:
            with _private_http():
                self._client = httpx.Client(
                    base_url=API_BASE,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    timeout=httpx.Timeout(60.0),
                )
        except Exception:
            pass
        else:
            return
        raise MinerUError(
            "MinerU HTTP client setup failed. Check the credential and proxy settings."
        ) from None

    def close(self) -> None:
        try:
            with _private_http():
                self._client.close()
        except Exception:
            pass
        else:
            return
        raise MinerUError("MinerU HTTP client cleanup failed.") from None

    def submit_batch(self, file_paths: list[str], timeout: int = MAX_WAIT_PER_TASK) -> list[bytes]:
        """Upload files, wait for extraction, return list of ZIP bytes in order."""
        # 1. Request pre-signed upload URLs
        expected_names = [Path(p).name for p in file_paths]
        resp = _request_with_retry(
            self._client,
            "POST",
            "/file-urls/batch",
            operation="batch creation",
            json={
                "files": [{"name": name} for name in expected_names],
                "model_version": "vlm",
            },
        )
        data = _response_data(resp, operation="batch creation")
        batch_id = data.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            raise MinerUError(
                "MinerU batch creation response is missing 'batch_id' or it is invalid"
            )
        upload_urls = data.get("file_urls")
        if not isinstance(upload_urls, list) or not all(
            isinstance(url, str) and url for url in upload_urls
        ):
            raise MinerUError("MinerU batch creation response has invalid 'file_urls'")
        if len(upload_urls) != len(file_paths):
            # A short URL list would silently skip chunks and misassemble the book.
            raise MinerUError(
                f"MinerU batch creation returned {len(upload_urls)} upload URLs "
                f"for {len(file_paths)} files"
            )

        # 2. Upload each file (no Content-Type — required by OSS signature)
        for local_path, upload_url in zip(file_paths, upload_urls):
            _request_with_retry(
                None,
                "PUT",
                upload_url,
                operation="upload",
                timeout=httpx.Timeout(120.0),
                content=Path(local_path).read_bytes(),
            )

        # 3. Poll until all done, matching results back to the upload manifest by name
        return self._poll_batch(batch_id, timeout, expected_names=expected_names)

    def _poll_batch(self, batch_id: str, timeout: int, *, expected_names: list[str]) -> list[bytes]:
        deadline = time.monotonic() + timeout
        interval = POLL_INTERVAL
        while True:
            resp = _request_with_retry(
                self._client, "GET", f"/extract-results/batch/{batch_id}", operation="polling"
            )
            results = _poll_results(resp)
            for index, result in enumerate(results, 1):
                state = result.get("state")
                if not isinstance(state, str) or not state:
                    raise MinerUError(
                        f"MinerU polling result {index} is missing 'state' or it is invalid"
                    )
            states = [result["state"] for result in results]
            if all(s in ("done", "failed") for s in states):
                if len(results) != len(expected_names):
                    # Result order/length must match the upload manifest; chunks arriving
                    # out of order or missing would silently misassemble the book.
                    raise MinerUError(
                        f"MinerU returned {len(results)} extraction results "
                        f"for {len(expected_names)} uploaded files"
                    )
                by_name = {}
                for index, result in enumerate(results, 1):
                    name = result.get("file_name")
                    if not isinstance(name, str) or not name:
                        raise MinerUError(f"MinerU polling result {index} has invalid 'file_name'")
                    by_name[name] = result
                if not all(name in by_name for name in expected_names):
                    raise MinerUError("MinerU polling results do not match the uploaded files")
                zips: list[bytes] = []
                for index, name in enumerate(expected_names, 1):
                    r = by_name[name]
                    if r["state"] == "failed":
                        raise MinerUError(f"MinerU polling: extraction failed for file {index}")
                    zip_url = r.get("full_zip_url")
                    if not isinstance(zip_url, str) or not zip_url:
                        raise MinerUError(
                            f"MinerU polling result for file {index} is missing 'full_zip_url' "
                            "or it is invalid"
                        )
                    zips.append(self._download_zip(zip_url))
                return zips
            if time.monotonic() > deadline:
                raise MinerUTimeoutError("MinerU polling timed out")
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
            interval = min(interval * 1.5, 30.0)

    def _download_zip(self, zip_url: str) -> bytes:
        resp = _request_with_retry(
            None,
            "GET",
            zip_url,
            operation="download",
            timeout=httpx.Timeout(30.0, read=300.0),
            follow_redirects=True,
        )
        return resp.content


# ── PDF splitting ──


def _chunk_path(directory: Path, start: int, end: int) -> Path:
    return directory / f"chunk_p{start + 1}-{end}.pdf"


def _prepare_chunks(source: Path, directory: Path, max_pages: int) -> tuple[int, list[Path]]:
    """Reuse the untouched file below the limit. Split once and never reopen chunks to count."""
    total = _epub_native.mineru_prepare_chunks(source, directory, max_pages)
    if total is None:
        # Preserve the page cloning semantics of pypdf for unsupported document features.
        with PdfReader(source) as reader:
            total = len(reader.pages)
            if total > max_pages:
                for start in range(0, total, max_pages):
                    end = min(start + max_pages, total)
                    with PdfWriter() as writer:
                        for i in range(start, end):
                            writer.add_page(reader.pages[i])
                        writer.write(_chunk_path(directory, start, end))
    return total, (
        [source]
        if total <= max_pages
        else [
            _chunk_path(directory, start, min(start + max_pages, total))
            for start in range(0, total, max_pages)
        ]
    )


# ── helpers ──


def _response_data(
    response: httpx.Response, *, operation: _Operation, code_required: bool = True
) -> dict:
    """Validate the API envelope without propagating any upstream diagnostic fields."""
    try:
        body = response.json()
    except (ValueError, RecursionError):
        pass
    else:
        if not isinstance(body, dict):
            raise MinerUError(f"MinerU {operation} response is not a JSON object")
        if code_required or "code" in body:
            code = body.get("code")
            if type(code) is not int:
                raise MinerUError(f"MinerU {operation} response has missing or invalid 'code'")
            if code != 0:
                raise MinerUError(f"MinerU {operation}: API reported failure")
        data = body.get("data")
        if not isinstance(data, dict):
            raise MinerUError(f"MinerU {operation} response is missing 'data' or it is invalid")
        return data
    raise MinerUError(f"MinerU {operation} returned a non-JSON body") from None


def _poll_results(response: httpx.Response) -> list[dict]:
    """Extract polling results; older successful replies can omit the top-level code."""
    data = _response_data(response, operation="polling", code_required=False)
    results = data.get("extract_result")
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        raise MinerUError(
            "MinerU polling response is missing 'data.extract_result' or it is invalid"
        )
    return results


def extract_pdf(pdf_path: str, *, api_token: str) -> list[bytes]:
    """Return result bundles in source-page order, removing private chunks on exit."""
    with tempfile.TemporaryDirectory(prefix="mineru-chunks-") as directory:
        _, chunk_paths = _prepare_chunks(Path(pdf_path).resolve(), Path(directory), MAX_PAGES)
        with closing(MinerUApi(api_token)) as api:
            return api.submit_batch([str(path) for path in chunk_paths])
