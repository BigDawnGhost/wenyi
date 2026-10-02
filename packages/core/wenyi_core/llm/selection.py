"""Provider and model data behind the interactive ``wenyi model`` picker.

Everything here reads declared profiles and environment variables. Inspection of the local
selection never needs a credential and never sends a request; a live model list is fetched
only by :func:`candidate_models`, and :func:`llm_section` turns a choice into the ``llm:``
mapping that ``wenyi model`` writes back.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .configuration import LLMConfig, ProviderConfig
from .oauth.credentials import OAuthCredentialError
from .operations import TIERS
from .profiles import AUTH_API_KEY, ProviderProfile, get_profile
from .providers._credentials import candidate_env_vars, read_env_value, resolve_access_token
from .providers.codex import model_ids
from .registry import PROVIDERS, provider_spec
from .subscriptions import SUBSCRIPTIONS, SUBSCRIPTIONS_BY_KIND

MODEL_LIST_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class ProviderChoice:
    """One provider the picker can select, with its locally visible credential state."""

    kind: str
    display_name: str
    description: str
    signup_url: str
    sign_in: str | None
    auth: str
    credential_envs: tuple[str, ...]
    credential_env: str | None
    api_key_envs: tuple[str, ...]
    oauth_env: str | None
    requires_api_key: bool
    supports_model_listing: bool
    base_url: str | None
    base_url_env: str | None
    preset_models: Mapping[str, str]
    known_models: tuple[str, ...]
    notes: str

    @property
    def configured(self) -> bool:
        """Report whether a credential variable currently supplies this provider."""
        return self.credential_env is not None

    def describe(self) -> dict[str, Any]:
        """Return the JSON-friendly description used by ``wenyi model --json``."""
        return {
            "kind": self.kind,
            "display_name": self.display_name,
            "description": self.description,
            "signup_url": self.signup_url,
            "sign_in": self.sign_in,
            "auth": self.auth,
            "credential_envs": list(self.credential_envs),
            "credential_env": self.credential_env,
            "api_key_envs": list(self.api_key_envs),
            "oauth_env": self.oauth_env,
            "requires_api_key": self.requires_api_key,
            "supports_model_listing": self.supports_model_listing,
            "base_url": self.base_url,
            "base_url_env": self.base_url_env,
            "configured": self.configured,
            "preset_models": dict(self.preset_models),
            "known_models": list(self.known_models),
            "notes": self.notes,
        }


def _profile_for(kind: str) -> ProviderProfile | None:
    try:
        return get_profile(kind)
    except ValueError:
        return None


def _login_name(kind: str) -> str | None:
    """Return the ``wenyi auth login`` name for a provider, when it has one."""
    subscription = SUBSCRIPTIONS_BY_KIND.get(kind)
    if subscription is None:
        return None
    for name, candidate in SUBSCRIPTIONS.items():
        if candidate is subscription:
            return name
    return subscription.kind


def preset_models(kind: str) -> dict[str, str]:
    """Return declared tier-to-model defaults, empty when the provider declares none.

    A provider backed by a dedicated adapter module has no ``preset:`` form, so its profile's
    own tier defaults stand in; that keeps the catalog-only providers selectable with ``-y``.
    """
    try:
        preset = provider_spec(kind).preset()
    except ValueError:
        profile = _profile_for(kind)
        return profile.preset_models() if profile is not None else {}
    return {tier: preset["models"][f"default_{tier}"]["model"] for tier in TIERS}


def provider_choices() -> tuple[ProviderChoice, ...]:
    """Describe every registered provider without constructing a client."""
    choices: list[ProviderChoice] = []
    for kind, spec in PROVIDERS.items():
        profile = _profile_for(kind)
        subscription = SUBSCRIPTIONS_BY_KIND.get(kind)
        if profile is None:
            adapter = spec.adapter_type()
            default_env = adapter.default_api_key_env
            envs = (default_env,) if default_env else ()
            display_name = getattr(adapter, "display_name", "") or kind
            fallback: tuple[str, ...] = ()
            notes = ""
            description = ""
            signup_url = ""
            auth = AUTH_API_KEY
            api_key_envs = envs
            requires_api_key = bool(envs)
            supports_model_listing = False
            base_url = None
            base_url_env = None
        else:
            envs = candidate_env_vars(profile)
            display_name = profile.display()
            fallback = profile.fallback_models
            notes = profile.notes
            description = profile.description
            signup_url = profile.signup_url
            auth = profile.auth
            api_key_envs = profile.api_key_envs
            requires_api_key = profile.requires_api_key
            supports_model_listing = profile.supports_model_listing
            base_url = profile.base_url
            base_url_env = profile.base_url_env
        _, credential_env = read_env_value(envs)
        presets = preset_models(kind)
        known = tuple(dict.fromkeys([*presets.values(), *fallback]))
        choices.append(
            ProviderChoice(
                kind=kind,
                display_name=display_name,
                description=description,
                signup_url=signup_url,
                sign_in=_login_name(kind),
                auth=auth,
                credential_envs=envs,
                credential_env=credential_env,
                api_key_envs=api_key_envs,
                oauth_env=subscription.env_var if subscription is not None else None,
                requires_api_key=requires_api_key,
                supports_model_listing=supports_model_listing,
                base_url=base_url,
                base_url_env=base_url_env,
                preset_models=presets,
                known_models=known,
                notes=notes,
            )
        )
    return tuple(choices)


def candidate_models(
    kind: str,
    *,
    offline: bool = False,
    timeout: float = MODEL_LIST_TIMEOUT_SECONDS,
    base_url: str | None = None,
) -> tuple[tuple[str, ...], str]:
    """Return the model IDs to offer for one provider plus a live-list failure message.

    Online results contain only IDs returned by the endpoint. Declared model IDs are
    offered exclusively in explicit offline mode.
    """
    profile = _profile_for(kind)
    declared = tuple(
        dict.fromkeys(
            [
                *preset_models(kind).values(),
                *(profile.fallback_models if profile is not None else ()),
            ]
        )
    )
    if offline:
        return declared, ""
    try:
        live = list_models(kind, timeout=timeout, base_url=base_url)
    except ValueError as error:
        return (), str(error)
    return tuple(dict.fromkeys(live)), ""


def llm_section(
    kind: str,
    tier_models: Mapping[str, str] | None = None,
    *,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Build the ``llm:`` mapping that selects one provider for the three tiers.

    A provider with a declared preset stays readable: the mapping names that preset and lists
    only the tiers that deviate from it. A provider without one needs all three models. An
    explicit *base_url* is written as a connection override, because a preset must keep its
    own ``kind`` when the two maps merge.
    """
    spec = provider_spec(kind)
    chosen = {tier: (tier_models or {}).get(tier) for tier in TIERS}
    connection: dict[str, Any] = {"kind": spec.kind}
    if base_url is not None:
        connection["base_url"] = base_url
    try:
        defaults: dict[str, Any] | None = spec.preset()
    except ValueError:
        defaults = None

    if defaults is None:
        missing = [tier for tier in TIERS if not chosen[tier]]
        if missing:
            raise ValueError(
                f"Provider {spec.kind!r} declares no built-in models; choose a model for: "
                + ", ".join(missing)
            )
        section: dict[str, Any] = {
            "providers": {"default": connection},
            "models": {
                f"default_{tier}": {"provider": "default", "model": chosen[tier]} for tier in TIERS
            },
            "tiers": {tier: f"default_{tier}" for tier in TIERS},
        }
    else:
        section = {"preset": spec.kind}
        if base_url is not None:
            section["providers"] = {"default": connection}
        overrides: dict[str, Any] = {}
        for tier in TIERS:
            default_entry = defaults["models"][f"default_{tier}"]
            model = chosen[tier] or default_entry["model"]
            if model == default_entry["model"]:
                continue
            entry: dict[str, Any] = {"provider": "default", "model": model}
            # A model profile replaces its preset entry wholesale, so carry the reasoning
            # options the provider declared for this tier instead of silently dropping them.
            if default_entry.get("options"):
                entry["options"] = default_entry["options"]
            overrides[f"default_{tier}"] = entry
        if overrides:
            section["models"] = overrides

    LLMConfig.model_validate(section)
    return section


def list_models(
    kind: str, *, timeout: float = MODEL_LIST_TIMEOUT_SECONDS, base_url: str | None = None
) -> tuple[str, ...]:
    """Fetch the model IDs a provider currently advertises.

    Returns an empty tuple when the provider publishes no list. Raises ``ValueError`` with an
    actionable message when the credential or the request fails.
    """
    spec = provider_spec(kind)
    profile = _profile_for(kind)
    if profile is not None and profile.supports_model_listing and not profile.subscription:
        endpoint = base_url or effective_base_url(kind)
        url = profile.models_url
        if endpoint and (base_url or not url or endpoint != profile.base_url):
            if profile.wire == "anthropic_messages":
                url = endpoint.rstrip("/") + "/v1/models"
            elif profile.wire in {"openai_chat", "openai_responses"}:
                url = endpoint.rstrip("/") + "/models"
            elif profile.wire == "gemini_native":
                url = endpoint.rstrip("/") + "/v1beta/models"
        if url:
            return _list_profile_models(profile, url=url, timeout=timeout)

    adapter = spec.adapter_type()(
        ProviderConfig(kind=spec.kind, base_url=base_url, timeout=timeout)
    )
    lister = getattr(adapter, "available_models", None)
    credential = getattr(adapter, "credential", None)
    if callable(lister) and callable(credential):
        try:
            return tuple(lister(credential()))
        except OAuthCredentialError as error:
            raise ValueError(str(error)) from None
    return ()


def _list_profile_models(profile: ProviderProfile, *, url: str, timeout: float) -> tuple[str, ...]:
    """Read a model catalog from the selected endpoint."""
    try:
        token = resolve_access_token(profile)
    except OAuthCredentialError as error:
        raise ValueError(str(error)) from None
    headers = {**profile.default_headers, **profile.auth_headers}
    if token:
        if profile.wire == "gemini_native":
            headers["x-goog-api-key"] = token
        elif profile.api_key_header == "authorization":
            headers["authorization"] = f"Bearer {token}"
        else:
            headers[profile.api_key_header] = token
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=headers)
    except httpx.HTTPError as error:
        raise ValueError(f"Cannot list {profile.kind} models: {error}") from None
    if not 200 <= response.status_code < 300:
        raise ValueError(f"Cannot list {profile.kind} models: HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise ValueError(f"Cannot list {profile.kind} models: the response is not JSON") from None
    if profile.wire == "gemini_native":
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ValueError(f"Cannot list {profile.kind} models: invalid catalog response")
        return tuple(
            entry["name"].removeprefix("models/")
            for entry in payload["models"]
            if isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and "generateContent" in entry.get("supportedGenerationMethods", [])
        )
    return model_ids(payload)


def effective_base_url(kind: str) -> str | None:
    """Return the endpoint a connection uses now: environment override, then profile default."""
    profile = _profile_for(kind)
    if profile is None:
        return None
    if profile.base_url_env:
        override = os.environ.get(profile.base_url_env, "").strip()
        if override:
            return override
    return profile.base_url


__all__ = [
    "MODEL_LIST_TIMEOUT_SECONDS",
    "ProviderChoice",
    "candidate_models",
    "effective_base_url",
    "list_models",
    "llm_section",
    "preset_models",
    "provider_choices",
]
