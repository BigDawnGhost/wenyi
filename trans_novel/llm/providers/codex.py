"""OpenAI Codex provider via the official openai-codex SDK (ChatGPT subscription auth).

This is intentionally separate from the Platform API OpenAI provider. Codex ChatGPT login
and API-key billing are different authentication paths; do not reuse Codex OAuth tokens as
``sk-`` API keys against api.openai.com.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ..retrying import EmptyResponseError
from ..transport import (
    ConnectionOptions,
    Messages,
    ProviderAdapter,
    RequestContext,
    ResolvedModel,
)
from ..usage import UsageSample, make_usage_sample

_JSON_MODE_INSTRUCTION = "Output must be valid json."

ReasoningEffortName = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


class CodexConnectionOptions(ConnectionOptions):
    """Connection extras for Codex; authentication is managed by the openai-codex SDK."""


class CodexOptions(BaseModel):
    """Per-model Codex options."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    reasoning_effort: ReasoningEffortName = "medium"
    ephemeral: bool = True
    # Keep sandbox/approval locked down for translation workloads.
    sandbox: Literal["read-only"] = "read-only"
    approval_mode: Literal["deny_all"] = "deny_all"


def _require_openai_codex():
    """Import the optional SDK or raise an actionable install hint."""
    try:
        import openai_codex
    except ImportError as error:
        raise RuntimeError(
            "The Codex provider requires the optional dependency group "
            "`codex` (openai-codex). Install with: uv sync --extra codex"
        ) from error
    return openai_codex


def flatten_messages(messages: Messages) -> tuple[str | None, str]:
    """Split system prompts from the conversation transcript used as Codex run input."""
    system_parts: list[str] = []
    transcript: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = str(message.get("content") or "")
        if role == "system":
            if content.strip():
                system_parts.append(content)
            continue
        label = role.upper()
        transcript.append(f"{label}:\n{content}" if content else f"{label}:")
    developer = "\n\n".join(system_parts) if system_parts else None
    prompt = "\n\n".join(transcript).strip()
    if not prompt:
        prompt = "(empty)"
    return developer, prompt


def extract_codex_usage(usage: Any) -> UsageSample | None:
    """Map Codex ThreadTokenUsage into Wenyi UsageSample."""
    if usage is None:
        return None
    total = getattr(usage, "total", None) or getattr(usage, "last", None)
    if total is None:
        return None
    prompt_tokens = int(getattr(total, "input_tokens", 0) or 0)
    output_tokens = int(getattr(total, "output_tokens", 0) or 0)
    reasoning_tokens = int(getattr(total, "reasoning_output_tokens", 0) or 0)
    completion_tokens = output_tokens + reasoning_tokens
    total_tokens = int(getattr(total, "total_tokens", 0) or 0) or (
        prompt_tokens + completion_tokens
    )
    cache_hit_tokens = int(getattr(total, "cached_input_tokens", 0) or 0)
    cache_miss_tokens = max(0, prompt_tokens - cache_hit_tokens)
    return make_usage_sample(
        {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
    )


def account_status() -> dict[str, Any]:
    """Return a JSON-serializable Codex account snapshot for CLI status."""
    openai_codex = _require_openai_codex()
    with openai_codex.Codex() as client:
        response = client.account()
    root = getattr(getattr(response, "account", None), "root", None)
    email = getattr(root, "email", None)
    plan = getattr(getattr(root, "plan_type", None), "value", None) or getattr(
        root, "plan_type", None
    )
    account_type = getattr(root, "type", None)
    return {
        "signed_in": bool(email or account_type),
        "email": email,
        "plan_type": plan,
        "account_type": account_type,
        "requires_openai_auth": bool(getattr(response, "requires_openai_auth", False)),
    }


def login_chatgpt(*, device_code: bool = False) -> dict[str, Any]:
    """Run the official Codex ChatGPT login flow and wait until it completes."""
    openai_codex = _require_openai_codex()
    with openai_codex.Codex() as client:
        if device_code:
            handle = client.login_chatgpt_device_code()
            print(f"Verification URL: {handle.verification_url}")
            print(f"User code: {handle.user_code}")
        else:
            handle = client.login_chatgpt()
            print(f"Open this URL to sign in with ChatGPT:\n{handle.auth_url}")
        handle.wait()
    try:
        status = account_status()
        success = bool(status.get("signed_in"))
    except Exception:  # noqa: BLE001 - Login wait finished; status is best-effort confirmation.
        success = False
    return {"success": success, "device_code": device_code}


def logout_chatgpt() -> None:
    """Clear the Codex SDK-managed ChatGPT session."""
    openai_codex = _require_openai_codex()
    with openai_codex.Codex() as client:
        client.logout()


def preset_models() -> dict[str, ResolvedModel[CodexOptions]]:
    """Experimental defaults; all tiers use Codex ``gpt-5.6-luna``.

    Model IDs still depend on the signed-in plan. Tiers differ only by reasoning effort.
    """
    return {
        "strong": ResolvedModel(
            model="gpt-5.6-luna",
            options=CodexOptions(reasoning_effort="high"),
        ),
        "cheap": ResolvedModel(
            model="gpt-5.6-luna",
            options=CodexOptions(reasoning_effort="medium"),
        ),
        "fast": ResolvedModel(
            model="gpt-5.6-luna",
            options=CodexOptions(reasoning_effort="low"),
        ),
    }


class CodexClient(ProviderAdapter):
    """Call models through openai-codex using ChatGPT subscription authentication."""

    default_base_url = None
    default_api_key_env = None
    requires_api_key = False
    requires_base_url = False
    connection_options = CodexConnectionOptions

    def validate_credentials(self) -> None:
        """Require an existing Codex ChatGPT session managed by the official SDK."""
        _require_openai_codex()
        try:
            status = account_status()
        except Exception as error:  # noqa: BLE001 - Surface SDK/auth failures as credential errors.
            raise RuntimeError(
                "Codex ChatGPT authentication is unavailable. "
                "Run `uv run trans-novel auth codex login` after `uv sync --extra codex`."
            ) from error
        if not status.get("signed_in"):
            raise RuntimeError(
                "Not signed in to Codex ChatGPT. "
                "Run `uv run trans-novel auth codex login` (or `login --device-code`)."
            )

    def _ensure_client(self) -> Any:
        with self._client_lock:
            if self._client is None:
                openai_codex = _require_openai_codex()
                client = openai_codex.Codex()
                # Keep the app-server process warm across batch calls in one workflow.
                if hasattr(client, "__enter__"):
                    client.__enter__()
                self._client = client
            return self._client

    def close(self) -> None:
        with self._client_lock:
            client = self._client
            self._client = None
        if client is None:
            return
        close = getattr(client, "close", None)
        if callable(close):
            close()
            return
        exit_fn = getattr(client, "__exit__", None)
        if callable(exit_fn):
            exit_fn(None, None, None)

    def _request(
        self,
        messages: Messages,
        model: ResolvedModel,
        *,
        json_mode: bool,
        context: RequestContext,
    ) -> str:
        openai_codex = _require_openai_codex()
        Sandbox = openai_codex.Sandbox
        ApprovalMode = openai_codex.ApprovalMode
        # generated.* is not re-exported on the package root for static checkers.
        reasoning_effort_type = getattr(
            import_module("openai_codex.generated.v2_all"),
            "ReasoningEffort",
        )

        developer, prompt = flatten_messages(messages)
        if json_mode:
            suffix = _JSON_MODE_INSTRUCTION
            developer = f"{developer}\n\n{suffix}" if developer else suffix

        options = model.options
        assert isinstance(options, CodexOptions)
        effort = reasoning_effort_type(options.reasoning_effort)
        sandbox = Sandbox.read_only
        approval = ApprovalMode.deny_all

        client = self._ensure_client()
        thread = client.thread_start(
            model=model.model,
            ephemeral=options.ephemeral,
            sandbox=sandbox,
            approval_mode=approval,
            developer_instructions=developer,
            config={"model_reasoning_effort": options.reasoning_effort},
        )
        result = thread.run(
            prompt,
            effort=effort,
            sandbox=sandbox,
            approval_mode=approval,
        )
        if getattr(result, "error", None) is not None:
            detail = getattr(result.error, "message", None) or str(result.error)
            raise RuntimeError(f"Codex turn failed: {detail}")
        text = (getattr(result, "final_response", None) or "").strip()
        if not text:
            raise EmptyResponseError("Codex returned an empty final_response")
        context.record_usage(extract_codex_usage(getattr(result, "usage", None)))
        return text
