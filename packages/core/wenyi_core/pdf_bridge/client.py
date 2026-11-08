"""Minimal HTTP client for wenyi-babeldoc-bridge."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from ..atomicio import publish_bytes
from ..models.errors import IngestError
from .connection import resolve_connection

logger = logging.getLogger(__name__)


class BabeldocBridgeError(IngestError):
    """Raised when the external BabelDOC bridge cannot complete a request."""


class BabeldocBridgeClient:
    """Talk to the external AGPL bridge over HTTP without importing BabelDOC."""

    def __init__(self, base_url: str, *, timeout: float = 600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        connection = resolve_connection(self.base_url)
        try:
            response = httpx.get(
                f"{connection.url}/health",
                headers=connection.headers,
                trust_env=connection.trust_env,
                timeout=min(30.0, self.timeout),
            )
            response.raise_for_status()
            return response.json()
        except Exception as error:
            raise BabeldocBridgeError(
                f"Cannot connect to BabelDOC bridge ({self.base_url}): {error}\n"
                "Start wenyi-babeldoc-bridge from its separate repository first."
            ) from error

    def extract(self, pdf_path: str | Path, *, pages: str | None = None) -> dict[str, Any]:
        pdf_path = Path(pdf_path)
        if not pdf_path.is_file():
            raise BabeldocBridgeError(f"PDF does not exist: {pdf_path}")
        self.health()
        data: dict[str, str] = {}
        if pages:
            data["pages"] = pages
        connection = resolve_connection(self.base_url)
        try:
            with pdf_path.open("rb") as handle:
                files = {"file": (pdf_path.name, handle, "application/pdf")}
                response = httpx.post(
                    f"{connection.url}/extract",
                    headers=connection.headers,
                    trust_env=connection.trust_env,
                    data=data,
                    files=files,
                    timeout=self.timeout,
                )
            if response.status_code >= 400:
                raise BabeldocBridgeError(
                    f"extract failed with HTTP {response.status_code}: {response.text[:500]}"
                )
            return response.json()
        except BabeldocBridgeError:
            raise
        except Exception as error:
            raise BabeldocBridgeError(f"extract request failed: {error}") from error

    def fillback(
        self,
        session_id: str,
        translations: dict[str, str],
        *,
        out_path: str | Path,
        annotations: dict[str, Any] | None = None,
    ) -> str:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        health = self.health()
        if annotations and "annotation-best-effort-v1" not in health.get("capabilities", []):
            raise BabeldocBridgeError(
                "This PDF requires best-effort annotation support. Update and restart "
                "wenyi-babeldoc-bridge before exporting."
            )
        connection = resolve_connection(self.base_url)
        payload: dict[str, Any] = {"session_id": session_id, "translations": translations}
        if annotations:
            payload["annotations"] = annotations
        try:
            response = httpx.post(
                f"{connection.url}/fillback",
                headers=connection.headers,
                trust_env=connection.trust_env,
                json=payload,
                timeout=self.timeout,
            )
            if response.status_code >= 400:
                raise BabeldocBridgeError(
                    f"fillback failed with HTTP {response.status_code}: {response.text[:500]}"
                )
            # Publish atomically: a crash must not leave a truncated PDF at the target.
            publish_bytes(out_path, response.content)
            raw_statistics = getattr(response, "headers", {}).get("X-Wenyi-PDF-Annotation-Stats")
            if raw_statistics:
                try:
                    statistics = json.loads(raw_statistics)
                except (TypeError, ValueError):
                    logger.warning("BabelDOC bridge returned invalid annotation statistics")
                else:
                    if isinstance(statistics, dict):
                        logger.info(
                            "PDF annotation statistics: %s",
                            json.dumps(statistics, sort_keys=True),
                        )
            return str(out_path)
        except BabeldocBridgeError:
            raise
        except Exception as error:
            raise BabeldocBridgeError(f"fillback request failed: {error}") from error
