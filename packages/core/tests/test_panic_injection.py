"""Tests for the feature-gated native panic injection probe."""

import pytest

try:
    from wenyi_core.markup import _epub_native
except ImportError:  # pragma: no cover - native extension not built
    _epub_native = None

probe = getattr(_epub_native, "panic_injection_probe", None) if _epub_native is not None else None

pytestmark = pytest.mark.skipif(
    probe is None, reason="native extension lacks the panic-injection feature"
)


def test_panic_injection_probe_converts_panic_to_runtime_error() -> None:
    assert probe is not None
    with pytest.raises(RuntimeError, match="^native panic: probe boom$"):
        probe("probe boom")
