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
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Callable

import httpx
from pypdf import PdfReader, PdfWriter

from trans_novel.ingest.errors import MinerUError, MinerUTimeoutError

API_BASE = "https://mineru.net/api/v4"
MAX_PAGES = 200
POLL_INTERVAL = 3.0
MAX_WAIT_PER_TASK = 600


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

    def submit_batch(
        self, file_paths: list[str], timeout: int = MAX_WAIT_PER_TASK
    ) -> list[bytes]:
        """Upload files, wait for extraction, return list of ZIP bytes in order."""
        # 1. Request pre-signed upload URLs
        files_meta = [{"name": Path(p).name} for p in file_paths]
        resp = self._client.post(
            "/file-urls/batch",
            json={
                "files": files_meta,
                "model_version": "vlm",
                "extra_formats": ["html"],
            },
        )
        resp.raise_for_status()
        data = _check(resp.json())["data"]

        # 2. Upload each file (no Content-Type — required by OSS signature)
        for local_path, upload_url in zip(file_paths, data["file_urls"]):
            put_resp = httpx.put(
                upload_url,
                content=Path(local_path).read_bytes(),
                timeout=httpx.Timeout(120.0),
            )
            put_resp.raise_for_status()

        # 3. Poll until all done
        return self._poll_batch(data["batch_id"], timeout)

    def _poll_batch(self, batch_id: str, timeout: int) -> list[bytes]:
        deadline = time.monotonic() + timeout
        interval = POLL_INTERVAL
        while True:
            resp = self._client.get(f"/extract-results/batch/{batch_id}")
            resp.raise_for_status()
            results = resp.json()["data"]["extract_result"]
            states = [r["state"] for r in results]
            if all(s in ("done", "failed") for s in states):
                zips: list[bytes] = []
                for r in results:
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
        resp = httpx.get(
            zip_url,
            timeout=httpx.Timeout(30.0, read=300.0),
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.content


# ── PDF splitting ──


def _split_pdf(pdf_path: str, max_pages: int = MAX_PAGES) -> list[Path]:
    reader = PdfReader(pdf_path)
    total = len(reader.pages)
    chunks = []
    for start in range(0, total, max_pages):
        end = min(start + max_pages, total)
        writer = PdfWriter()
        for i in range(start, end):
            writer.add_page(reader.pages[i])
        tmp = tempfile.NamedTemporaryFile(
            prefix=f"chunk_p{start + 1}-{end}_", suffix=".pdf", delete=False
        )
        writer.write(tmp.name)
        tmp.close()
        chunks.append(Path(tmp.name))
    return chunks


# ── HTML helpers ──


def _html_from_zip(zip_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.endswith((".html", ".htm")):
                return zf.read(name).decode("utf-8")
    raise MinerUError("No HTML file in result ZIP")


def _assemble_html(parts: list[str], title: str) -> str:
    head = _clean_head(parts[0])
    bodies = [_body_text(p) for p in parts]
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{title}</title>
{head}
</head>
<body>
{"".join(bodies)}
</body>
</html>"""


def _body_text(html: str) -> str:
    m = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
    return m.group(1) if m else html


def _clean_head(html: str) -> str:
    m = re.search(r"<head[^>]*>(.*?)</head>", html, re.DOTALL | re.IGNORECASE)
    if not m:
        return ""
    head = m.group(1)
    head = re.sub(r"<title[^>]*>.*?</title>", "", head, flags=re.DOTALL | re.IGNORECASE)
    head = re.sub(r"<meta[^>]*charset[^>]*/?>", "", head, flags=re.IGNORECASE)
    return head.strip()


# ── helpers ──


def _check(body: dict) -> dict:
    if body.get("code") != 0:
        raise MinerUError(f"API error: code={body.get('code')} msg={body.get('msg')}")
    return body


def _page_count(pdf_path: str) -> int:
    return len(PdfReader(pdf_path).pages)


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

    total_pages = _page_count(pdf_path)
    msg(f"PDF: {total_pages} pages")

    # Prepare chunks
    if total_pages <= MAX_PAGES:
        chunk_paths = [Path(pdf_path)]
        owned: list[Path] = []
    else:
        msg(f"Splitting into ≤{MAX_PAGES}-page chunks…")
        chunk_paths = _split_pdf(pdf_path, MAX_PAGES)
        owned = chunk_paths
        for i, cp in enumerate(chunk_paths):
            msg(f"  Chunk {i + 1}/{len(chunk_paths)}: {_page_count(str(cp))} pages")

    api = MinerUApi(api_token)
    try:
        msg(f"Uploading and extracting {len(chunk_paths)} chunk(s)…")
        zip_bytes_list = api.submit_batch([str(cp) for cp in chunk_paths])

        html_parts = [_html_from_zip(zb) for zb in zip_bytes_list]
        for i in range(len(html_parts)):
            msg(f"  Chunk {i + 1} done ({len(html_parts[i]):,} chars)")
    finally:
        api.close()
        for cp in owned:
            cp.unlink(missing_ok=True)

    html = (
        html_parts[0]
        if len(html_parts) == 1
        else _assemble_html(html_parts, Path(pdf_path).name)
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
