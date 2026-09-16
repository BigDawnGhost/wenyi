"""翻译策略与步骤注册表。"""

from __future__ import annotations

from fastapi import APIRouter

from ..schemas import StepDef, StrategyTemplateOut
from ..strategies import PRESET_TEMPLATES, STEP_REGISTRY

router = APIRouter(prefix="/strategies", tags=["strategies"])


@router.get("/steps", response_model=list[StepDef])
def list_steps() -> list[dict]:
    return STEP_REGISTRY


@router.get("/templates", response_model=list[StrategyTemplateOut])
def list_templates() -> list[dict]:
    return [
        {"name": t["name"], "description": t.get("description", ""),
         "time_factor": t.get("time_factor", 1),
         "recommended": t.get("recommended", False), "steps": t["steps"]}
        for t in PRESET_TEMPLATES
    ]
