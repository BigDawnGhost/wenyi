"""Resolution is deterministic, role-aware and independent of model or state services."""

from copy import deepcopy
from dataclasses import replace
from typing import cast

import pytest
from wenyi_core.i18n import languages
from wenyi_core.i18n.policy.models import OperationBinding, OperationSpec, PolicyContext
from wenyi_core.i18n.policy.registry import OPERATIONS
from wenyi_core.i18n.policy.resolver import resolve_policy, sort_operations
from wenyi_core.i18n.resources import read_json


def export(src="ja", tgt="zh", fmt="epub", **kwargs):
    return resolve_policy(PolicyContext(src, tgt, phase="export", format=fmt, **kwargs))


def test_profiles_keep_roles_and_scripts_separate():
    plan = export("ja-JP", "zh-Hans")
    assert plan.enabled("markup.japanese_ruby")
    assert plan.enabled("punctuation.zh_cn")
    assert plan.export.language_tag == "zh-Hans"
    assert plan.export.about_locale == "zh"
    traditional = export("en", "zh-TW")
    assert not traditional.enabled("punctuation.zh_cn")
    assert traditional.export.language_tag == "zh-Hant"
    assert traditional.export.about_locale == "en"
    assert not export("en", "ja").enabled("markup.japanese_ruby")
    assert export("vi", "en", "docx").export.target_font is None
    assert export("en", "zh", "docx").export.target_font == "宋体"


def test_explicit_on_never_bypasses_format_or_existing_flags():
    context = PolicyContext("en", "zh", phase="export", format="epub")
    with pytest.raises(ValueError, match="docx.chinese_font"):
        resolve_policy(context, {"docx.chinese_font": OperationBinding(mode="on")})
    with pytest.raises(ValueError, match="punctuation.zh_cn"):
        resolve_policy(
            replace(context, punctuation_normalize=False),
            {"punctuation.zh_cn": OperationBinding(mode="on")},
        )
    off = resolve_policy(context, {"punctuation.zh_cn": OperationBinding(mode="off")})
    assert not off.enabled("punctuation.zh_cn")
    assert off.selection("punctuation.zh_cn").origins[-1] == "developer"


def test_unknown_operations_and_parameters_are_rejected():
    with pytest.raises(ValueError, match="Unknown language operation"):
        resolve_policy(PolicyContext("ja", "zh"), {"made.up": OperationBinding()})
    with pytest.raises(ValueError, match="Unknown option"):
        resolve_policy(
            PolicyContext("ja", "zh"),
            {"punctuation.zh_cn": OperationBinding(options={"font": "x"})},
        )


def test_srt_and_bridge_do_not_select_book_layout_operations():
    srt = resolve_policy(PolicyContext("ja", "en", path="srt"))
    assert all(op.point == "prompt.compose" for op in srt.operations)
    bridge = export("ja", "zh", "pdf", backend="babeldoc")
    assert not bridge.enabled("markup.japanese_ruby")
    assert not bridge.enabled("docx.chinese_font")


def test_fingerprints_exclude_export_options_from_paid_phases():
    ctx = PolicyContext("ja", "zh")
    a = resolve_policy(ctx)
    b = resolve_policy(ctx, {"docx.chinese_font": OperationBinding(options={"font": "Arial"})})
    assert a.fingerprint == b.fingerprint
    assert (
        export(fmt="docx").fingerprint
        != resolve_policy(
            replace(ctx, phase="export", format="docx"),
            {"docx.chinese_font": OperationBinding(options={"font": "Arial"})},
        ).fingerprint
    )
    assert a.to_dict()["fingerprint"] == a.fingerprint


def test_sort_uses_stable_ids_and_rejects_invalid_graphs():
    a = OperationSpec("a", "export.text", roles=("target",))
    b = replace(a, id="b", after=("a",))
    assert sort_operations((b, a), {"a": a, "b": b}) == (a, b)
    assert sort_operations((b,), {"a": a, "b": b}) == (b,)
    with pytest.raises(ValueError, match="cycle"):
        sort_operations((replace(a, after=("b",)), b), {"a": a, "b": b})
    with pytest.raises(ValueError, match="requires"):
        sort_operations((replace(b, requires=("a",)),), {"a": a, "b": b})
    c = replace(a, id="c", point="export.style")
    with pytest.raises(ValueError, match="extension point"):
        sort_operations((replace(a, after=("c",)), c), {"a": a, "c": c})


def test_registry_is_immutable():
    with pytest.raises(TypeError):
        cast(dict[str, OperationSpec], OPERATIONS)["oops"] = OPERATIONS["punctuation.zh_cn"]


def test_recursive_inheritance_replaces_bindings_and_rejects_cycles(monkeypatch):
    profiles = {
        "languages/en.json": {
            "policy": {
                "target": {"docx.chinese_font": {"mode": "auto", "options": {"font": "Arial"}}}
            }
        },
        "languages/en-US.json": {
            "extends": "en",
            "policy": {"target": {"docx.chinese_font": {"mode": "off"}}},
        },
        "languages/en-GB.json": {"extends": "en-US"},
    }
    monkeypatch.setattr(
        languages,
        "read_json",
        lambda path: read_json(path) if path not in profiles else deepcopy(profiles[path]),
    )
    assert languages.profile("en-GB")["policy"]["target"]["docx.chinese_font"] == {"mode": "off"}
    profiles["languages/en.json"]["extends"] = "en-GB"
    with pytest.raises(ValueError, match="cycle"):
        languages.profile("en-GB")
    profiles["languages/en.json"]["extends"] = "missing"
    with pytest.raises(ValueError, match="Unsupported"):
        languages.profile("en-GB")


def test_exact_pairs_and_cross_role_conflicts_need_explicit_resolution(monkeypatch):
    from wenyi_core.i18n.policy import resolver

    original = resolver.profile

    def profile(code):
        value = original(code)
        value["policy"] = (
            {"source": {"prompt.language_rules": {"options": {}}}}
            if code == "ja"
            else {"target": {"prompt.language_rules": {"mode": "on"}}}
        )
        return value

    monkeypatch.setattr(resolver, "profile", profile)
    with pytest.raises(ValueError, match="Conflicting source/target"):
        resolve_policy(PolicyContext("ja", "zh-Hant"))
    monkeypatch.setattr(
        resolver,
        "read_json",
        lambda path: (
            {"policy": {"operations": {"prompt.language_rules": {"mode": "auto"}}}}
            if path == "pairs/ja__zh.json"
            else read_json(path)
        ),
    )
    assert resolve_policy(PolicyContext("ja", "zh")).enabled("prompt.language_rules")
    with pytest.raises(ValueError, match="Conflicting source/target"):
        resolve_policy(PolicyContext("ja", "zh-Hant"))
    assert resolve_policy(
        PolicyContext("ja", "zh-Hant"), {"prompt.language_rules": OperationBinding()}
    ).enabled("prompt.language_rules")


def test_profile_export_defaults_reject_unknown_fields_and_null(monkeypatch):
    for metadata in ({"about_locale": None}, {"about_locale": "ja"}, {"unknown": "x"}):
        monkeypatch.setattr(
            languages,
            "read_json",
            lambda path: (
                {"policy": {"export": metadata}} if path == "languages/en.json" else read_json(path)
            ),
        )
        with pytest.raises(ValueError):
            languages.profile("en")


@pytest.mark.parametrize("origin", ["profile", "pair"])
@pytest.mark.parametrize("operation", ["prompt.language_rules", "export.language_metadata"])
def test_source_definitions_cannot_disable_required_operations(monkeypatch, origin, operation):
    from wenyi_core.i18n.policy import resolver

    def definition(path):
        data = read_json(path)
        if origin == "profile" and path == "languages/zh.json":
            data.setdefault("policy", {}).setdefault("target", {})[operation] = {"mode": "off"}
        if origin == "pair" and path == "pairs/ja__zh.json":
            data["policy"] = {"operations": {operation: {"mode": "off"}}}
        return data

    monkeypatch.setattr(languages, "read_json", definition)
    monkeypatch.setattr(resolver, "read_json", definition)
    with pytest.raises(ValueError, match="Required language operation cannot be disabled"):
        export()


@pytest.mark.parametrize(
    "policy",
    [
        {"export": {"about_locale": None}},
        {"export": {"about_locale": "ja"}},
        {"export": {"unknown": "x"}},
        {"operation": {}},
    ],
)
def test_pair_definitions_reject_invalid_fields(monkeypatch, policy):
    from wenyi_core.i18n.policy import resolver

    monkeypatch.setattr(
        resolver,
        "read_json",
        lambda path: {"policy": policy} if path == "pairs/ja__zh.json" else read_json(path),
    )
    with pytest.raises(ValueError):
        export(fmt="docx")


def test_target_binding_can_refine_common_defaults_without_a_role_conflict(monkeypatch):
    def definition(path):
        data = read_json(path)
        if path == "languages/zh.json":
            data["policy"]["target"]["export.language_metadata"] = {"mode": "on"}
        return data

    monkeypatch.setattr(languages, "read_json", definition)
    assert export(src="en").enabled("export.language_metadata")
