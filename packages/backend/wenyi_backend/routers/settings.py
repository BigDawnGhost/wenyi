"""Global model registration and default project configuration."""

from __future__ import annotations

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from wenyi_core.llm.configuration import ProviderConfig
from wenyi_core.llm.models import list_connection_models
from wenyi_core.llm.registry import provider_spec

from ..config_documents import global_document
from ..context import current_context
from ..global_settings import (
    GlobalSettings,
    load_settings,
    registry_guard,
    save_settings,
    validate_settings,
)
from ..model_registry import project_registry_updates
from ..schemas import (
    GlobalConfigInput,
    GlobalConfigOut,
    ModelListRequest,
    ModelListResult,
    ModelPingRequest,
    ModelPingResult,
)

router = APIRouter(prefix="/settings", tags=["settings"])


def response(value: GlobalSettings) -> dict:
    document = global_document(value.config)
    return {
        "yaml": yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        "effective": document,
        "default_template": value.default_template,
        "revision": value.revision,
    }


@router.get("", response_model=GlobalConfigOut)
def get_settings() -> dict:
    return response(load_settings())


@router.get("/defaults", response_model=GlobalConfigOut)
def default_settings() -> dict:
    return response(GlobalSettings(current_context().settings_store.defaults()))


@router.post("/validate", response_model=GlobalConfigOut)
def validate(body: GlobalConfigInput) -> dict:
    try:
        config = validate_settings(body.yaml, body.default_template)
        with registry_guard() as conn:
            project_registry_updates(
                conn, load_settings(connection=conn).config, config, body.model_renames
            )
        return response(GlobalSettings(config, body.default_template, body.revision))
    except (ValueError, yaml.YAMLError) as error:
        raise HTTPException(422, str(error)) from error


@router.put("", response_model=GlobalConfigOut)
def save(body: GlobalConfigInput) -> dict:
    try:
        return response(
            save_settings(
                body.yaml,
                body.default_template,
                body.revision,
                model_renames=body.model_renames,
                provider_renames=body.provider_renames,
            )
        )
    except (ValueError, yaml.YAMLError) as error:
        raise HTTPException(422, str(error)) from error


@router.post("/models/ping", response_model=list[ModelPingResult])
def ping_models(body: ModelPingRequest) -> list[dict]:
    """Send one short live request per saved model profile; this uses provider tokens."""
    try:
        client = current_context().build_client(load_settings().config)
        results = client.probe_profiles(timeout=body.timeout)
    except (ValueError, RuntimeError) as error:
        raise HTTPException(422, str(error)) from error
    return [result.describe() for result in results]


@router.post(
    "/models/list",
    response_model=ModelListResult,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": ModelListRequest.model_json_schema()}},
        }
    },
)
async def list_models(request: Request) -> dict:
    """List a connection's model names; the connection may be an unsaved draft."""
    # Parse manually so default validation responses never echo a typed key.
    try:
        body = ModelListRequest.model_validate(await request.json())
    except Exception:
        raise HTTPException(422, "Invalid model list request") from None
    return await run_in_threadpool(_list_models, body)


def _list_models(body: ModelListRequest) -> dict:
    try:
        provider = ProviderConfig.model_validate(body.provider)
        # Unknown kinds and adapter-owned fields fail here, before any request.
        provider_spec(provider.kind).validate_connection(provider)
        typed = body.api_key.get_secret_value().strip() if body.api_key else ""
        resolve = current_context().connection_credential
        if typed:
            credential, injected = typed, True
        elif resolve is not None:
            # Desktop: a draft connection with no typed key uses the stored key, if any.
            credential = resolve(body.connection, provider) if body.connection else None
            injected = True
        else:
            # Web reads the environment named by the connection, like real requests do. Only
            # names already in use may be read, so a draft cannot send arbitrary variables.
            _require_known_environment(provider)
            credential, injected = None, False
        result = list_connection_models(
            provider, credential=credential, use_credential=injected, timeout=body.timeout
        )
    except ValidationError:
        raise HTTPException(422, "Invalid connection settings") from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return result.describe()


def _require_known_environment(provider: ProviderConfig) -> None:
    name = provider.api_key_env
    if name is None or name == provider_spec(provider.kind).adapter_type().default_api_key_env:
        return
    saved = load_settings().config.llm.providers.values()
    if name not in {connection.api_key_env for connection in saved}:
        raise ValueError(f"Save the configuration before reading {name}")
