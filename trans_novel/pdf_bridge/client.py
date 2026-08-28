"""Minimal HTTP client for wenyi-babeldoc-bridge."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from ..ingest.errors import IngestError


class BabeldocBridgeError(IngestError):
    """Raised when the external BabelDOC bridge cannot complete a request."""


class BabeldocBridgeClient:
    """Talk to an AGPL bridge process. Wenyi stays MIT-only."""

    def __init__(self, base_url: str, *, timeout: float = 600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        try:
            response = httpx.get(f"{self.base_url}/health", timeout=min(30.0, self.timeout))
            response.raise_for_status()
            return response.json()
        except Exception as error:
            raise BabeldocBridgeError(
                f"无法连接 BabelDOC bridge（{self.base_url}）：{error}\n"
                "请先在独立仓库启动：wenyi-babeldoc-bridge"
            ) from error

    def extract(self, pdf_path: str | Path, *, pages: str | None = None) -> dict[str, Any]:
        pdf_path = Path(pdf_path)
        if not pdf_path.is_file():
            raise BabeldocBridgeError(f"PDF 不存在：{pdf_path}")
        self.health()
        data: dict[str, str] = {}
        if pages:
            data["pages"] = pages
        try:
            with pdf_path.open("rb") as handle:
                files = {"file": (pdf_path.name, handle, "application/pdf")}
                response = httpx.post(
                    f"{self.base_url}/extract",
                    data=data,
                    files=files,
                    timeout=self.timeout,
                )
            if response.status_code >= 400:
                raise BabeldocBridgeError(
                    f"extract 失败 HTTP {response.status_code}: {response.text[:500]}"
                )
            return response.json()
        except BabeldocBridgeError:
            raise
        except Exception as error:
            raise BabeldocBridgeError(f"extract 请求失败：{error}") from error

    def fillback(
        self,
        session_id: str,
        translations: dict[str, str],
        *,
        out_path: str | Path,
    ) -> str:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self.health()
        try:
            response = httpx.post(
                f"{self.base_url}/fillback",
                json={"session_id": session_id, "translations": translations},
                timeout=self.timeout,
            )
            if response.status_code >= 400:
                raise BabeldocBridgeError(
                    f"fillback 失败 HTTP {response.status_code}: {response.text[:500]}"
                )
            out_path.write_bytes(response.content)
            return str(out_path)
        except BabeldocBridgeError:
            raise
        except Exception as error:
            raise BabeldocBridgeError(f"fillback 请求失败：{error}") from error

    def delete_session(self, session_id: str) -> None:
        try:
            httpx.delete(f"{self.base_url}/session/{session_id}", timeout=min(30.0, self.timeout))
        except Exception:
            # Best-effort cleanup.
            return
