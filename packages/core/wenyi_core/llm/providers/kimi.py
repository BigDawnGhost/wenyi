"""Kimi Code key routing, with legacy Moonshot Chat Completions preserved."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlsplit

from ..configuration import ProviderConfig
from ..transport import Messages, RequestContext, ResolvedModel
from ._credentials import candidate_env_vars, configured_base_url, read_env_value
from ._options import ProviderOptions
from .wire.anthropic import AnthropicClient
from .wire.openai_chat import OpenAIChatClient

KIMI_CODE_BASE_URL = "https://api.kimi.com/coding"


class KimiClient(AnthropicClient, OpenAIChatClient):
    """Use Messages for Kimi Code keys and Chat Completions for Moonshot keys."""

    @classmethod
    def endpoint(cls, cfg: ProviderConfig) -> str | None:
        profile = cls.profile_for(cfg.kind)
        endpoint = configured_base_url(profile, cfg.base_url)
        # Explicit connection/environment overrides always win over key detection.
        if cfg.base_url or endpoint != profile.base_url:
            return endpoint
        key, _ = read_env_value(candidate_env_vars(profile, cfg.api_key_env))
        return KIMI_CODE_BASE_URL if key.startswith("sk-kimi-") else endpoint

    def _uses_messages(self) -> bool:
        url = urlsplit(self.resolve_base_url())
        return url.hostname == "api.kimi.com" and url.path.rstrip("/") in {"/coding", "/coding/v1"}

    def reasoning_parts(self, model: str, options: ProviderOptions) -> tuple[dict, dict]:
        # Moonshot's effort field is Chat-specific; Messages owns its thinking budget.
        if self._uses_messages():
            return {}, {}
        return super().reasoning_parts(model, options)

    def _request(
        self,
        messages: Messages,
        model: ResolvedModel[ProviderOptions],
        *,
        json_mode: bool,
        context: RequestContext,
    ) -> str:
        # Both Kimi surfaces reject or constrain temperature on reasoning models.
        model = replace(model, options=model.options.model_copy(update={"temperature": None}))
        if self._uses_messages():
            # Skip the generic effort builder so Messages can build extended thinking.
            options = model.options
            thinking = options.thinking_enabled(self.profile.default_thinking)
            extra = dict(options.extra_body)
            if "thinking" not in extra:
                if thinking:
                    from .wire.anthropic import thinking_budget

                    budget = thinking_budget(context.max_tokens or 4096)
                    if budget is not None:
                        extra["thinking"] = {"type": "enabled", "budget_tokens": budget}
                else:
                    extra["thinking"] = {"type": "disabled"}
            model = replace(model, options=options.model_copy(update={"extra_body": extra}))
            return AnthropicClient._request(
                self, messages, model, json_mode=json_mode, context=context
            )
        return OpenAIChatClient._request(
            self, messages, model, json_mode=json_mode, context=context
        )
