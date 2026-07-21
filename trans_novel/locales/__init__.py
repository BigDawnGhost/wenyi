"""Runtime localization for Wenyi's user interface.

The UI locale is intentionally independent from the source and target languages of a
translation. ``WENYI_LANG`` provides an explicit override; otherwise standard locale
environment variables are checked before the Windows process locale. English is the
fallback for every locale other than ``zh*``.
"""

from __future__ import annotations

import locale
import os
from collections.abc import Mapping
from typing import Literal

from .en import MESSAGES as EN_MESSAGES
from .zh import MESSAGES as ZH_MESSAGES

UILanguage = Literal["en", "zh"]


def _language_from_locale(value: str | None) -> UILanguage:
    """Map a locale identifier to one of the supported interface languages."""
    normalized = (value or "").strip().lower().replace("-", "_")
    return "zh" if normalized.startswith("zh") else "en"


def detect_ui_language(
    environ: Mapping[str, str] | None = None,
    *,
    windows_locale: str | None = None,
    is_windows: bool | None = None,
) -> UILanguage:
    """Detect the UI language without consulting translation configuration.

    Locale environment variables follow their standard precedence. The optional
    arguments make Windows detection deterministic in tests.
    """
    values = os.environ if environ is None else environ
    override = values.get("WENYI_LANG", "").strip()
    if override:
        return _language_from_locale(override)

    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = values.get(name, "").strip()
        if value:
            return _language_from_locale(value)

    if is_windows is None:
        is_windows = os.name == "nt"
    if is_windows and windows_locale is None:
        try:
            windows_locale = locale.getlocale()[0]
        except (ValueError, OSError):  # pragma: no cover - platform dependent
            windows_locale = None
    return _language_from_locale(windows_locale)


UI_LANGUAGE: UILanguage = detect_ui_language()
_CATALOGS = {"en": EN_MESSAGES, "zh": ZH_MESSAGES}


def message(key: str, /, **values: object) -> str:
    """Return a formatted message from the active English or Chinese catalog."""
    template = _CATALOGS[UI_LANGUAGE][key]
    return template.format(**values) if values else template


__all__ = ["UI_LANGUAGE", "detect_ui_language", "message"]
