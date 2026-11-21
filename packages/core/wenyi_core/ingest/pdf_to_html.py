#!/usr/bin/env python3
"""
Convert PDF to HTML via MinerU API.

Simple API usage:
    from pdf_to_html import convert_pdf_to_html
    output_path = convert_pdf_to_html("document.pdf")

CLI usage:
    uv run python pdf_to_html.py document.pdf [output.html]
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
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
                "extra_formats": ["html"],
            },
        )
        resp.raise_for_status()
        data = _check(resp.json())["data"]
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
        return self._poll_batch(data["batch_id"], timeout, expected_names=expected_names)

    def _poll_batch(self, batch_id: str, timeout: int, *, expected_names: list[str]) -> list[bytes]:
        deadline = time.monotonic() + timeout
        interval = POLL_INTERVAL
        while True:
            resp = _request_with_retry(self._client, "GET", f"/extract-results/batch/{batch_id}")
            resp.raise_for_status()
            results = resp.json()["data"]["extract_result"]
            states = [r["state"] for r in results]
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
                            f"Extraction failed for {r['file_name']}: {r.get('err_msg', 'unknown')}"
                        )
                    zips.append(self._download_zip(r["full_zip_url"]))
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


# ── HTML helpers ──


def _html_from_zip(zip_bytes: bytes) -> str:
    native = _epub_native.mineru_html_from_zip(zip_bytes)
    if native is not None:
        return native
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.endswith((".html", ".htm")):
                return zf.read(name).decode("utf-8")
    raise MinerUError("No HTML file in result ZIP")


# ── helpers ──


def _check(body: dict) -> dict:
    if body.get("code") != 0:
        raise MinerUError(f"API error: code={body.get('code')} msg={body.get('msg')}")
    return body


# ── public API ──

ProgressFn = Callable[[str], None]


def convert_pdf_to_html(
    pdf_path: str,
    output_path: str | None = None,
    *,
    api_token: str | None = None,
    on_progress: ProgressFn | None = None,
) -> str:
    """Convert a PDF file to HTML via MinerU API.

    Handles PDFs exceeding 200 pages by splitting into chunks and
    reassembling the HTML output.

    Args:
        pdf_path: Path to the source PDF file.
        output_path: Destination path. Defaults to ``<pdf_stem>.html``.
        api_token: MinerU API token. Reads ``MINERU_API_KEY`` from env if omitted.
        on_progress: Optional progress callback.

    Returns:
        Absolute path to the generated HTML file.
    """
    msg = on_progress or (lambda _: None)

    pdf_path = str(Path(pdf_path).resolve())
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(pdf_path)

    if output_path is None:
        output_path = str(Path(pdf_path).with_suffix(".html"))
    output_path = str(Path(output_path).resolve())

    if api_token is None:
        api_token = os.getenv("MINERU_API_KEY")
    if not api_token:
        raise MinerUError("API token not provided and MINERU_API_KEY not set")

    with tempfile.TemporaryDirectory(prefix="mineru-chunks-") as directory:
        total_pages, chunk_paths = _prepare_chunks(Path(pdf_path), Path(directory), MAX_PAGES)
        msg(f"PDF: {total_pages} pages")
        if len(chunk_paths) > 1:
            for i in range(len(chunk_paths)):
                count = min(MAX_PAGES, total_pages - i * MAX_PAGES)
                msg(f"  Chunk {i + 1}/{len(chunk_paths)}: {count} pages")
        with closing(MinerUApi(api_token)) as api:
            msg(f"Uploading and extracting {len(chunk_paths)} chunk(s)…")
            zip_bytes_list = api.submit_batch([str(cp) for cp in chunk_paths])
    html_parts = [_html_from_zip(zb) for zb in zip_bytes_list]
    for i, part in enumerate(html_parts):
        msg(f"  Chunk {i + 1} done ({len(part):,} chars)")

    html = (
        html_parts[0]
        if len(html_parts) == 1
        else _epub_native.mineru_assemble_html(html_parts, Path(pdf_path).name)
    )
    Path(output_path).write_text(html, encoding="utf-8")
    msg(f"Done → {output_path} ({len(html):,} chars)")
    return output_path


# ── CLI ──


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run python pdf_to_html.py <pdf_path> [output_html_path]")
        sys.exit(1)

    try:
        convert_pdf_to_html(
            sys.argv[1],
            sys.argv[2] if len(sys.argv) > 2 else None,
            on_progress=print,
        )
    except (MinerUError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
