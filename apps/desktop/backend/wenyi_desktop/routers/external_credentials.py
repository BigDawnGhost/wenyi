"""Independent Desktop service credentials; only the manual key can be read back locally."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, SecretStr, StrictBool, model_validator

from ..local_credentials import CredentialConflict, credential_store
from .credentials import desktop_only

router = APIRouter(
    prefix="/desktop/external-credentials",
    tags=["desktop"],
    dependencies=[Depends(desktop_only)],
)


class MinerUCredentialStatus(BaseModel):
    mode: Literal["environment", "manual"]
    storage: Literal["system", "session"] | None
    available: bool
    environment: str
    system_storage_available: bool
    requires_key: bool


class MinerUCredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    secret: SecretStr | None = None
    clear: StrictBool = False
    storage: Literal["auto", "system", "session"] = "auto"

    @model_validator(mode="after")
    def exclusive_operation(self):
        if self.clear == (self.secret is not None):
            raise ValueError("Provide a key or Clear")
        return self


@router.get("/mineru", response_model=MinerUCredentialStatus)
def status() -> dict:
    return credential_store().mineru_status()


class MinerUCredentialSecret(BaseModel):
    secret: str | None


@router.get("/mineru/secret", response_model=MinerUCredentialSecret)
def reveal(response: Response) -> dict:
    """Return the manually saved key for the local owner; MINERU_API_KEY is never returned."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"secret": credential_store().manual_mineru_secret()}
    except CredentialConflict:
        raise HTTPException(409, "MinerU credential changed; reload before reading") from None


@router.put(
    "/mineru",
    response_model=MinerUCredentialStatus,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": MinerUCredentialInput.model_json_schema()}},
        }
    },
)
async def update(request: Request) -> dict:
    # Avoid FastAPI's input-bearing validation details even for malformed JSON.
    try:
        body = MinerUCredentialInput.model_validate(await request.json())
    except Exception:
        raise HTTPException(422, "Invalid MinerU credential request") from None
    return await run_in_threadpool(save_credential, body)


def save_credential(body: MinerUCredentialInput) -> dict:
    try:
        return credential_store().save_mineru(
            secret=body.secret.get_secret_value() if body.secret is not None else None,
            clear=body.clear,
            storage=body.storage,
        )
    except CredentialConflict:
        raise HTTPException(409, "MinerU credential changed; reload before saving") from None
    except ValueError:
        raise HTTPException(422, "Enter a nonblank, header-safe plaintext MinerU API key") from None
    except Exception:
        raise HTTPException(
            503, "Could not save MinerU credentials. Try session-only storage."
        ) from None
