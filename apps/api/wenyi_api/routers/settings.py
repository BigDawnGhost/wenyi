"""Global model registration and default project configuration."""

from __future__ import annotations

import yaml
from fastapi import APIRouter, HTTPException
from wenyi_core.config import Config

from ..config import settings
from ..config_documents import config_document
from ..db import get_pool
from ..global_settings import GlobalSettings, load_settings, save_settings, validate_settings
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
    return response(GlobalSettings(Config.load(settings.config_path)))


@router.post("/validate", response_model=GlobalConfigOut)
def validate(body: GlobalConfigInput) -> dict:
    try:
        config = validate_settings(body.yaml, body.default_template)
        with get_pool().connection() as conn:
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
                body.yaml, body.default_template, body.revision, model_renames=body.model_renames
            )
        )
    except (ValueError, yaml.YAMLError) as error:
        raise HTTPException(422, str(error)) from error
