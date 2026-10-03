"""Global model registration and default project configuration."""

from __future__ import annotations

import yaml
from fastapi import APIRouter, HTTPException

from ..config_documents import config_document
from ..context import current_context
from ..global_settings import (
    GlobalSettings,
    load_settings,
    registry_guard,
    save_settings,
    validate_settings,
)
from ..model_registry import project_registry_updates
from ..schemas import GlobalConfigInput, GlobalConfigOut

router = APIRouter(prefix="/settings", tags=["settings"])


def response(value: GlobalSettings) -> dict:
    document = config_document(value.config)
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
