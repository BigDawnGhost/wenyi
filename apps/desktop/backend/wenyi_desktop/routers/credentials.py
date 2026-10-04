"""Authenticated Desktop credential management; never expose credential values."""

from __future__ import annotations

import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, SecretStr
from wenyi_backend.context import current_context
from wenyi_backend.global_settings import load_settings, registry_guard

from ..local_credentials import CredentialConflict, CredentialUnavailable, credential_store


def desktop_only(request: Request) -> None:
    token = current_context().api_token
    provided = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not token or not secrets.compare_digest(provided, token):
        raise HTTPException(401, "Invalid local token")
    origin = request.headers.get("origin")
    from ..desktop import ORIGINS

    allowed = ORIGINS
    if origin and origin not in allowed:
        raise HTTPException(403, "Origin is not allowed")


router = APIRouter(
    prefix="/desktop/credentials",
    tags=["desktop"],
    dependencies=[Depends(desktop_only)],
)


class CredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    mode: Literal["environment", "manual"]
    storage: Literal["auto", "system", "session"] = "auto"
    secret: SecretStr | None = None
    clear: bool = False


class CredentialStatus(BaseModel):
    mode: Literal["environment", "manual"]
    storage: Literal["system", "session"] | None
    available: bool
    environment: str | None
    system_storage_available: bool
    requires_key: bool


@router.get("", response_model=dict[str, CredentialStatus])
def statuses() -> dict:
    store = credential_store()
    with registry_guard() as conn:
        providers = load_settings(connection=conn).config.llm.providers
        records = store._records(conn)
    return {
        name: store.status(name, provider, record=records.get(name, {}))
        for name, provider in providers.items()
    }


@router.put(
    "/{connection}",
    response_model=CredentialStatus,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": CredentialInput.model_json_schema()}},
        }
    },
)
async def update(connection: str, request: Request) -> dict:
    # Do not let FastAPI's default validation responses echo secret-bearing inputs.
    try:
        body = CredentialInput.model_validate(await request.json())
    except Exception:
        raise HTTPException(422, "Invalid credential request") from None
    return await run_in_threadpool(save_credential, connection, body)


def save_credential(connection: str, body: CredentialInput) -> dict:
    """Vault access may prompt the OS; never block the API event loop."""
    store = credential_store()
    with registry_guard() as conn:
        initial = load_settings(connection=conn)
        provider = initial.config.llm.providers.get(connection)
        if provider is None:
            raise HTTPException(404, "Save this connection before configuring credentials")
        previous = store._records(conn).get(connection, {})
    change = None
    try:
        change = store.prepare(
            previous,
            mode=body.mode,
            storage=body.storage,
            secret=body.secret.get_secret_value() if body.secret is not None else None,
            clear=body.clear,
        )
        with registry_guard() as conn:
            current = load_settings(connection=conn)
            if (
                current.revision != initial.revision
                or current.config.llm.providers.get(connection) != provider
            ):
                raise CredentialConflict("Connection changed; reload before saving credentials")
            store.commit_change(connection, change, conn)
    except Exception as error:
        if change is not None:
            store.abort(change)
        if isinstance(error, CredentialConflict):
            raise HTTPException(409, str(error)) from None
        if isinstance(error, ValueError):
            raise HTTPException(422, str(error)) from None
        if isinstance(error, CredentialUnavailable):
            raise HTTPException(503, str(error)) from None
        raise HTTPException(503, "Could not save credential preferences") from None
    store.finish(change)
    return store.status(
        connection,
        provider,
        record=change.record,
        available=True if change.created and body.mode == "manual" else None,
    )
