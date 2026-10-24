"""Discover the model names a connection offers, without saving any configuration."""

from __future__ import annotations

from dataclasses import dataclass

from .configuration import ProviderConfig
from .credentials import validate_credential
from .errors import describe_provider_failure
from .registry import provider_spec

DEFAULT_LIST_TIMEOUT = 20.0
# Large gateways list thousands of models; keep responses bounded for the UI.
MAX_LISTED_MODELS = 2000


@dataclass(frozen=True)
class ModelListing:
    """Model names on success, or a provider-independent failure category and fixed message."""

    ok: bool
    models: tuple[str, ...] = ()
    category: str = "ok"
    message: str = ""
    status_code: int | None = None

    def describe(self) -> dict:
        return {
            "ok": self.ok,
            "models": list(self.models),
            "category": self.category,
            "message": self.message,
            "status_code": self.status_code,
        }


def list_connection_models(
    connection: ProviderConfig,
    *,
    credential: str | None = None,
    use_credential: bool = False,
    timeout: float = DEFAULT_LIST_TIMEOUT,
) -> ModelListing:
    """List models for one (possibly unsaved) connection with a single, non-retried request.

    ``use_credential`` injects ``credential`` instead of reading the environment, matching how
    Desktop snapshots keys. Listing is free on supported providers and records no usage.
    """
    if timeout <= 0:
        raise ValueError("Timeout must be positive")
    if use_credential:
        validate_credential(credential)
    adapter_type = provider_spec(connection.kind).adapter_type()
    probe = connection.model_copy(
        update={"max_retries": 0, "timeout": min(timeout, connection.timeout)}
    )
    adapter = (
        adapter_type(probe, credentials=(credential,)) if use_credential else adapter_type(probe)
    )
    try:
        adapter.validate_credentials()
    except RuntimeError as error:
        # Credential validation names the environment variable, never its value.
        return ModelListing(False, category="missing_credential", message=str(error))
    try:
        names = adapter.list_models()
    except NotImplementedError:
        return ModelListing(
            False,
            category="unsupported",
            message=f"Provider {connection.kind} cannot list models.",
        )
    except Exception as error:  # noqa: BLE001 - Every failure becomes a safe result.
        failure = describe_provider_failure(error)
        return ModelListing(
            False,
            category=failure.category,
            message=failure.message,
            status_code=failure.status_code,
        )
    unique = sorted(dict.fromkeys(name.strip() for name in names if name and name.strip()))
    return ModelListing(True, tuple(unique[:MAX_LISTED_MODELS]))
