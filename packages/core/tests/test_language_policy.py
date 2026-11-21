"""Resolution is deterministic, role-aware and independent of model or state services."""

from copy import deepcopy
from dataclasses import replace
from typing import cast

import pytest
from wenyi_core.assemble.policy import export_plan
from wenyi_core.i18n import languages
from wenyi_core.i18n.policy.models import OperationBinding, OperationSpec, PolicyContext
from wenyi_core.i18n.policy.registry import OPERATIONS
from wenyi_core.i18n.policy.resolver import resolve_policy, sort_operations
from wenyi_core.i18n.resources import read_json
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.storage.file import FileStorage


def export(src="ja", tgt="zh", fmt="epub", **kwargs):
    return resolve_policy(PolicyContext(src, tgt, phase="export", format=fmt, **kwargs))


def test_profiles_keep_roles_and_scripts_separate():
    plan = export("ja-JP", "zh-Hans")
    assert plan.export.language_tag == "zh-Hans"
    assert plan.export.about_locale == "zh"
    traditional = export("en", "zh-TW")
    assert traditional.export.language_tag == "zh-Hant"
    assert traditional.export.about_locale == "en"
    assert export("vi", "en", "docx").export.target_font is None
    assert export("en", "zh", "docx").export.target_font == "宋体"


def test_explicit_on_never_bypasses_format():
    context = PolicyContext("en", "zh", phase="export", format="epub")
    with pytest.raises(ValueError, match="docx.chinese_font"):
        resolve_policy(context, {"docx.chinese_font": OperationBinding(mode="on")})
    off = resolve_policy(
        replace(context, format="docx"), {"docx.chinese_font": OperationBinding(mode="off")}
    )
    assert not off.enabled("docx.chinese_font")
    assert off.selection("docx.chinese_font").origins[-1] == "developer"


def test_unknown_operations_and_parameters_are_rejected():
    with pytest.raises(ValueError, match="Unknown language operation"):
        resolve_policy(PolicyContext("ja", "zh"), {"made.up": OperationBinding()})
    with pytest.raises(ValueError, match="Unknown option"):
        resolve_policy(
            PolicyContext("ja", "zh"),
            {"docx.chinese_font": OperationBinding(options={"made_up": "x"})},
        )


def test_bridge_does_not_select_native_layout_operations():
    bridge = export("ja", "zh", "pdf", backend="babeldoc")
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


def test_export_identity_binds_targets_and_output_choices_not_cache_timestamps(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("Original.", encoding="utf-8")
    chapter = Chapter(index=0, segments=[Segment(index=0, source="Original.", target=None)])
    store = FileStorage(str(tmp_path / "run"))
    store.init_from_document(
        Document(
            title="Test",
            source_lang="en",
            target_lang="zh",
            fmt="text",
            source_path=str(source),
            chapters=[chapter],
        )
    )
    initial = export_plan(store, "epub")
    manifest = store.load_manifest()
    manifest["created_at"] = "changed timestamp"
    store.save_manifest(manifest)
    chapter.meta["source_digest_policy"] = "irrelevant analysis identity"
    store.save_chapter(chapter)
    assert export_plan(store, "epub").fingerprint == initial.fingerprint
    assert export_plan(store, "epub", bilingual=True).fingerprint != initial.fingerprint
    assert export_plan(store, "epub", about_page=False).fingerprint != initial.fingerprint
    chapter.segments[0].target = ""
    store.save_chapter(chapter)
    assert export_plan(store, "epub").fingerprint != initial.fingerprint


@pytest.mark.parametrize("target", ["zh", "en", "zh-Hant"])
@pytest.mark.parametrize(
    "phase,fmt,about_page,invalidates",
    [
        ("analysis", "", True, False),
        ("translation", "", True, False),
        ("review", "", True, False),
        ("export", "epub", True, True),
        ("export", "epub", False, False),
        ("export", "txt", True, False),
        ("export", "docx", True, False),
    ],
)
def test_about_page_copy_only_invalidates_enabled_epub_exports(
    monkeypatch, target, phase, fmt, about_page, invalidates
):
    from wenyi_core.i18n.policy import resolver

    context = PolicyContext("en", target, phase=phase, format=fmt, about_page=about_page)
    before = resolve_policy(context)
    original = resolver.read_text

    def updated_text(path: str) -> str:
        text = original(path)
        if path.startswith("export/about."):
            return text + "\n<!-- Updated project description. -->"
        return text

    monkeypatch.setattr(resolver, "read_text", updated_text)
    after = resolve_policy(context)
    assert (before.fingerprint != after.fingerprint) == invalidates
    assert (before.semantic_fingerprint != after.semantic_fingerprint) == invalidates


def test_sort_uses_stable_ids_and_rejects_invalid_graphs():
    a = OperationSpec("a", "export.style", roles=("target",))
    b = replace(a, id="b", after=("a",))
    assert sort_operations((b, a), {"a": a, "b": b}) == (a, b)
    assert sort_operations((b,), {"a": a, "b": b}) == (b,)
    with pytest.raises(ValueError, match="cycle"):
        sort_operations((replace(a, after=("b",)), b), {"a": a, "b": b})
    with pytest.raises(ValueError, match="requires"):
        sort_operations((replace(b, requires=("a",)),), {"a": a, "b": b})
    c = replace(a, id="c", point="export.metadata")
    with pytest.raises(ValueError, match="extension point"):
        sort_operations((replace(a, after=("c",)), c), {"a": a, "c": c})


def test_registry_is_immutable():
    with pytest.raises(TypeError):
        cast(dict[str, OperationSpec], OPERATIONS)["oops"] = OPERATIONS["docx.chinese_font"]


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

    def profile(code, *, genre):
        value = original(code, genre=genre)
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
