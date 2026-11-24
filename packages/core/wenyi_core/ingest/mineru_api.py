"""Retrieve ordered MinerU Precision API result bundles for a PDF."""

from __future__ import annotations

import json
import tempfile
import time
from contextlib import closing
from pathlib import Path

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


def _request_with_retry(
    client: httpx.Client | None,
    method: str,
    url: str,
    *,
    timeout: httpx.Timeout | None = None,
    **kwargs,
) -> httpx.Response:
    """Send one request with bounded retries for transient transport and 5xx/429 failures.

    Non-transient 4xx responses return immediately so ``raise_for_status`` handling at the
    call site keeps its meaning. ``client=None`` uses the module-level httpx request path
    (pre-signed OSS uploads carry their own host, not the API base URL).
    """
    sender = client.request if client is not None else httpx.request
    request_kwargs: dict = dict(kwargs)
    if timeout is not None:
        request_kwargs["timeout"] = timeout
    last_error: Exception | None = None
    for attempt in range(_TRANSIENT_ATTEMPTS):
        try:
            resp = sender(method, url, **request_kwargs)
        except httpx.TransportError as error:
            last_error = error
        else:
            if resp.status_code < 500 and resp.status_code != 429:
                return resp
            last_error = MinerUError(f"transient HTTP {resp.status_code}")
        if attempt + 1 < _TRANSIENT_ATTEMPTS:
            _sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise MinerUError(
        f"MinerU request failed after {_TRANSIENT_ATTEMPTS} attempts: {last_error}"
    ) from last_error


# ── API client ──


class MinerUApi:
    """Thin HTTP client for MinerU Precision API (v4)."""

    def __init__(self, token: str) -> None:
        self._client = httpx.Client(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(60.0),
        )

    def close(self) -> None:
        self._client.close()

    def submit_batch(self, file_paths: list[str], timeout: int = MAX_WAIT_PER_TASK) -> list[bytes]:
        """Upload files, wait for extraction, return list of ZIP bytes in order."""
        # 1. Request pre-signed upload URLs
        files_meta = [{"name": Path(p).name} for p in file_paths]
        resp = _request_with_retry(
            self._client,
            "POST",
            "/file-urls/batch",
            json={
                "files": files_meta,
                "model_version": "vlm",
            },
        )
        resp.raise_for_status()
        body = _json_body(resp)
        if not isinstance(body, dict):
            raise MinerUError(
                "MinerU upload response is not a JSON object; "
                f"server reason: {_server_reason(body)}"
            )
        data = _check(body).get("data")
        if not isinstance(data, dict):
            raise MinerUError(
                f"MinerU upload response is missing 'data'; server reason: {_server_reason(body)}"
            )
        upload_urls = data.get("file_urls")
        if not isinstance(upload_urls, list) or len(upload_urls) != len(file_paths):
            # A short URL list would silently skip chunks and misassemble the book.
            raise MinerUError(
                f"MinerU returned {len(upload_urls) if isinstance(upload_urls, list) else 0} "
                f"upload URLs for {len(file_paths)} files"
            )

        # 2. Upload each file (no Content-Type — required by OSS signature)
        for local_path, upload_url in zip(file_paths, upload_urls):
            put_resp = _request_with_retry(
                None,
                "PUT",
                upload_url,
                timeout=httpx.Timeout(120.0),
                content=Path(local_path).read_bytes(),
            )
            put_resp.raise_for_status()

        # 3. Poll until all done, matching results back to the upload manifest by name
        expected_names = [Path(p).name for p in file_paths]
        batch_id = data.get("batch_id")
        if not batch_id:
            raise MinerUError(
                "MinerU upload response is missing 'batch_id'; "
                f"server reason: {_server_reason(data)}"
            )
        return self._poll_batch(batch_id, timeout, expected_names=expected_names)

    def _poll_batch(self, batch_id: str, timeout: int, *, expected_names: list[str]) -> list[bytes]:
        deadline = time.monotonic() + timeout
        interval = POLL_INTERVAL
        while True:
            resp = _request_with_retry(self._client, "GET", f"/extract-results/batch/{batch_id}")
            resp.raise_for_status()
            results = _poll_results(resp)
            for result in results:
                if not result.get("state"):
                    raise MinerUError(
                        f"MinerU polling result for {result.get('file_name', 'unknown file')} "
                        f"is missing 'state'; server reason: {_server_reason(result)}"
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
                by_name = {r.get("file_name"): r for r in results}
                missing = [name for name in expected_names if name not in by_name]
                if missing:
                    raise MinerUError(
                        f"MinerU extraction results are missing uploaded files: "
                        f"{', '.join(missing[:3])}"
                    )
                zips: list[bytes] = []
                for name in expected_names:
                    r = by_name[name]
                    if r["state"] == "failed":
                        raise MinerUError(
                            f"Extraction failed for {r.get('file_name', 'unknown file')}: "
                            f"{r.get('err_msg', 'unknown')}"
                        )
                    zip_url = r.get("full_zip_url")
                    if not zip_url:
                        raise MinerUError(
                            f"Extraction result for {r.get('file_name', 'unknown file')} "
                            f"is missing 'full_zip_url'; server reason: {_server_reason(r)}"
                        )
                    zips.append(self._download_zip(zip_url))
                return zips
            if time.monotonic() > deadline:
                raise MinerUTimeoutError(f"Batch {batch_id} timed out")
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
            interval = min(interval * 1.5, 30.0)

    def _download_zip(self, zip_url: str) -> bytes:
        resp = _request_with_retry(
            None,
            "GET",
            zip_url,
            timeout=httpx.Timeout(30.0, read=300.0),
            follow_redirects=True,
        )
        resp.raise_for_status()
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


def _check(body: dict) -> dict:
    if body.get("code") != 0:
        raise MinerUError(f"API error: code={body.get('code')} msg={body.get('msg')}")
    return body


def _server_reason(body: object) -> str:
    """Return the server-provided failure reason, or a short JSON snippet."""
    if isinstance(body, dict):
        for key in ("msg", "message", "err_msg", "error"):
            reason = body.get(key)
            if reason:
                return str(reason)
        if "code" in body:
            return f"code={body.get('code')}"
    return json.dumps(body, ensure_ascii=False, default=str)[:200]


def _json_body(response: httpx.Response) -> object:
    """Parse a JSON body, wrapping decode failures in MinerUError with a short snippet."""
    try:
        return response.json()
    except ValueError:
        raise MinerUError(f"MinerU returned a non-JSON body: {response.text[:200]!r}") from None


def _poll_results(response: httpx.Response) -> list[dict]:
    """Extract polling results, reporting the server reason instead of a bare KeyError."""
    body = _json_body(response)
    data = body.get("data") if isinstance(body, dict) else None
    results = data.get("extract_result") if isinstance(data, dict) else None
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        raise MinerUError(
            "MinerU polling response is missing 'data.extract_result'; "
            f"server reason: {_server_reason(body)}"
        )
    return results


def extract_pdf(pdf_path: str, *, api_token: str) -> list[bytes]:
    """Return result bundles in source-page order, removing private chunks on exit."""
    with tempfile.TemporaryDirectory(prefix="mineru-chunks-") as directory:
        _, chunk_paths = _prepare_chunks(Path(pdf_path).resolve(), Path(directory), MAX_PAGES)
        with closing(MinerUApi(api_token)) as api:
            return api.submit_batch([str(path) for path in chunk_paths])
