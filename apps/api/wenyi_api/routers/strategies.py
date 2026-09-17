"""Translation strategy and workflow step registry endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from ..global_settings import load_settings
from ..schemas import StepDef, StrategyTemplateOut
from ..strategies import PRESET_TEMPLATES, STEP_REGISTRY, strategy_to_config, workflow_steps

router = APIRouter(prefix="/strategies", tags=["strategies"])


@router.get("/steps", response_model=list[StepDef])
def list_steps() -> list[dict]:
    return STEP_REGISTRY


@router.get("/templates", response_model=list[StrategyTemplateOut])
def list_templates() -> list[dict]:
    defaults = load_settings()
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "time_factor": t.get("time_factor", 1),
            "recommended": t["name"] == defaults.default_template,
            "steps": workflow_steps(strategy_to_config({"template": t["name"]}, defaults.config)),
        }
        for t in PRESET_TEMPLATES
    ]
