"""Minimal HTTP client for wenyi-babeldoc-bridge."""

from __future__ import annotations

import codecs
import json
import logging
import ssl
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

import httpx

from ..atomicio import publish_target
from ..models.errors import IngestError
from .connection import resolve_connection

logger = logging.getLogger(__name__)

#: Transient HTTP statuses, mirroring the shared LLM retry classification.
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})
_DEFAULT_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.5
_RETRY_MAX_DELAY_SECONDS = 8.0

# Module-level indirection keeps the backoff waits patchable in tests.
_sleep = time.sleep

_TERMINAL_PROTOCOL_TYPES = (
    httpx.InvalidURL,
    httpx.LocalProtocolError,
    httpx.UnsupportedProtocol,
    ssl.SSLCertVerificationError,
)
_RETRYABLE_TRANSPORT_TYPES = (httpx.TransportError, ConnectionError, TimeoutError)

_T = TypeVar("_T")


def _error_preview(response: httpx.Response, limit: int = 500) -> str:
    """Read only the displayed prefix of an HTTP error response."""
    try:
        decoder = codecs.getincrementaldecoder(response.encoding or "utf-8")(errors="replace")
    except LookupError:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = ""
    for chunk in response.iter_bytes(chunk_size=4096):
        text += decoder.decode(chunk)
        if len(text) >= limit:
            return text[:limit]
    return (text + decoder.decode(b"", final=True))[:limit]


class BabeldocBridgeError(IngestError):
    """Raised when the external BabelDOC bridge cannot complete a request.

    Terminal condition: retrying cannot succeed — HTTP 4xx, bridge protocol
    violations and local usage errors.
    """

    retryable: bool = False


class BabeldocBridgeUnavailableError(BabeldocBridgeError):
    """The bridge was unreachable or returned a transient failure (network/5xx)."""

    retryable = True


class _BridgeHTTPFailure(Exception):
    """Internal marker carrying an HTTP status through the retry classifier."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _exception_chain(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _failure_status(error: BaseException) -> int | None:
    for item in _exception_chain(error):
        candidates = (
            getattr(item, "status", None),
            getattr(item, "status_code", None),
            getattr(getattr(item, "response", None), "status_code", None),
        )
        for value in candidates:
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if 100 <= value <= 599:
                return value
    return None


def _is_retryable_failure(error: BaseException) -> bool:
    """True for network/timeout failures and 408/409/429/5xx; False for terminal ones."""
    for item in _exception_chain(error):
        if isinstance(item, BabeldocBridgeError) or isinstance(item, _TERMINAL_PROTOCOL_TYPES):
            return False
    status = _failure_status(error)
    if status is not None:
        return status in _RETRYABLE_STATUS_CODES or status >= 500
    return any(isinstance(item, _RETRYABLE_TRANSPORT_TYPES) for item in _exception_chain(error))


def _retry_delay(attempt: int) -> float:
    """Exponential backoff with a small base and a hard cap."""
    return min(_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)), _RETRY_MAX_DELAY_SECONDS)


class BabeldocBridgeClient:
    """Talk to the external AGPL bridge over HTTP without importing BabelDOC."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 600.0,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.max_attempts = max_attempts

    def _send_with_retry(
        self,
        operation: str,
        send: Callable[[], _T],
        wrap: Callable[[BaseException, bool], BabeldocBridgeError],
    ) -> _T:
        """Run send() with bounded retries on transient network or 5xx failures."""
        attempt = 1
        while True:
            try:
                return send()
            except BabeldocBridgeError:
                raise
            except Exception as error:
                retryable = _is_retryable_failure(error)
                if not retryable or attempt >= self.max_attempts:
                    raise wrap(error, retryable) from error
                delay = _retry_delay(attempt)
                logger.warning(
                    "BabelDOC bridge %s failed (attempt %d/%d), retrying in %.1fs: %s",
                    operation,
                    attempt,
                    self.max_attempts,
                    delay,
                    error,
                )
                _sleep(delay)
                attempt += 1

    def health(self) -> dict[str, Any]:
        connection = resolve_connection(self.base_url)

        def send() -> dict[str, Any]:
            response = httpx.get(
                f"{connection.url}/health",
                headers=connection.headers,
                trust_env=connection.trust_env,
                timeout=min(30.0, self.timeout),
            )
            response.raise_for_status()
            return response.json()

        def wrap(error: BaseException, retryable: bool) -> BabeldocBridgeError:
            error_class = BabeldocBridgeUnavailableError if retryable else BabeldocBridgeError
            return error_class(
                f"Cannot connect to BabelDOC bridge ({self.base_url}): {error}\n"
                "Start wenyi-babeldoc-bridge from its separate repository first."
            )

        return self._send_with_retry("health", send, wrap)

    def extract(self, pdf_path: str | Path, *, pages: str | None = None) -> dict[str, Any]:
        pdf_path = Path(pdf_path)
        if not pdf_path.is_file():
            raise BabeldocBridgeError(f"PDF does not exist: {pdf_path}")
        self.health()
        data: dict[str, str] = {}
        if pages:
            data["pages"] = pages
        connection = resolve_connection(self.base_url)

        def send() -> dict[str, Any]:
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
                raise _BridgeHTTPFailure(
                    response.status_code,
                    f"extract failed with HTTP {response.status_code}: {response.text[:500]}",
                )
            return response.json()

        def wrap(error: BaseException, retryable: bool) -> BabeldocBridgeError:
            error_class = BabeldocBridgeUnavailableError if retryable else BabeldocBridgeError
            if isinstance(error, _BridgeHTTPFailure):
                return error_class(str(error))
            return error_class(f"extract request failed: {error}")

        return self._send_with_retry("extract", send, wrap)

    def fillback(
        self,
        session_id: str,
        translations: dict[str, str],
        *,
        out_path: str | Path,
        annotations: dict[str, Any] | None = None,
        rows: dict[str, list[dict[str, int]]] | None = None,
    ) -> str:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        health = self.health()
        if annotations and "annotation-best-effort-v1" not in health.get("capabilities", []):
            raise BabeldocBridgeError(
                "This PDF requires best-effort annotation support. Update and restart "
                "wenyi-babeldoc-bridge before exporting."
            )
        if (
            annotations
            and any(value.get("protected_objects") for value in annotations.values())
            and "protected-objects-v1" not in health.get("capabilities", [])
        ):
            raise BabeldocBridgeError(
                "This PDF requires protected object support. Update and restart "
                "wenyi-babeldoc-bridge before exporting."
            )
        if (
            annotations
            and any(value.get("omitted_protected_objects") for value in annotations.values())
            and "protected-object-omission-v1" not in health.get("capabilities", [])
        ):
            raise BabeldocBridgeError(
                "This PDF requires explicit protected object omission support. Update and "
                "restart wenyi-babeldoc-bridge before exporting."
            )
        if rows and "toc-rows-v1" not in health.get("capabilities", []):
            raise BabeldocBridgeError(
                "This PDF requires explicit item row support. Update and restart "
                "wenyi-babeldoc-bridge before exporting."
            )
        connection = resolve_connection(self.base_url)
        payload: dict[str, Any] = {"session_id": session_id, "translations": translations}
        if annotations:
            payload["annotations"] = annotations
        if rows:
            payload["rows"] = rows

        def send() -> str:
            with httpx.stream(
                "POST",
                f"{connection.url}/fillback",
                headers=connection.headers,
                trust_env=connection.trust_env,
                json=payload,
                timeout=self.timeout,
            ) as response:
                if response.status_code >= 400:
                    raise _BridgeHTTPFailure(
                        response.status_code,
                        f"fillback failed with HTTP {response.status_code}: {_error_preview(response)}",
                    )
                # A failed download must not replace a previous PDF with a partial response.
                with publish_target(out_path) as temporary, temporary.open("wb") as output:
                    for chunk in response.iter_bytes(chunk_size=256 * 1024):
                        output.write(chunk)
                raw_statistics = response.headers.get("X-Wenyi-PDF-Annotation-Stats")
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

        def wrap(error: BaseException, retryable: bool) -> BabeldocBridgeError:
            error_class = BabeldocBridgeUnavailableError if retryable else BabeldocBridgeError
            if isinstance(error, _BridgeHTTPFailure):
                return error_class(str(error))
            return error_class(f"fillback request failed: {error}")

        return self._send_with_retry("fillback", send, wrap)
