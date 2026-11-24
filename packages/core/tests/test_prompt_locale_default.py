"""Only an explicit locale renders ja/zh-specific prompt content; the default is en."""

import pytest
from wenyi_core.i18n.policy.models import PolicyContext
from wenyi_core.i18n.policy.resolver import resolve_policy
from wenyi_core.i18n.prompts import render


def test_default_locale_renders_english_guidance():
    default = render("translator_system")
    assert default == render("translator_system", src="en", tgt="en")
    assert "English" in default
    assert "Japanese" not in default
    assert "furigana" not in default
    assert "先輩" not in default


def test_explicit_ja_zh_locale_renders_pair_specific_guidance():
    explicit = render("translator_system", src="ja", tgt="zh")
    assert "Japanese" in explicit
    assert "furigana" in explicit
    assert "先輩" in explicit
    assert explicit != render("translator_system")


def test_plan_direction_must_be_stated_explicitly():
    plan = resolve_policy(PolicyContext("ja", "zh"))
    with pytest.raises(ValueError, match="does not match"):
        render("translator_system", plan=plan)
    assert "furigana" in render("translator_system", src="ja", tgt="zh", plan=plan)
