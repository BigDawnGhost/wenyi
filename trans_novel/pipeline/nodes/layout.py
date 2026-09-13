"""EPUB 布局分析节点及其持久化恢复策略。"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

from trans_novel.assemble.epub.layout import build_layout_inventory
from trans_novel.epub.layout import LayoutInventory, LayoutProfile
from trans_novel.model_profiles import parse_model_selection, parse_provider_model
from trans_novel.pipeline.contracts import NodeOutcome, NodeRequest
from trans_novel.pipeline.nodes.layout_analysis import analyze_layout
from trans_novel.pipeline.planning.fingerprints import analyst_model_profile
from trans_novel.pipeline.state import NODE_LAYOUT, SCOPE_BOOK, input_fingerprint


def _analyst_fingerprint(config) -> str:
    candidates = config.llm.models.analyst
    routing = {}
    for candidate in candidates:
        provider, raw_model = parse_provider_model(candidate)
        key = f"{provider}/{parse_model_selection(raw_model).model}"
        value = config.llm.provider_routing.get(key)
        if value is not None:
            routing[key] = value.model_dump(mode="json", exclude_none=True)
    return input_fingerprint(analyst_model_profile(config), routing)


def _valid_attempt_id(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        attempt = UUID(hex=value)
    except ValueError:
        return False
    return attempt.version == 4 and attempt.hex == value


def current_layout_state(store, source_path: str) -> tuple[LayoutInventory, LayoutProfile | None]:
    """构建当前原文清单，并只接纳与清单完全匹配的持久化 profile。"""
    state = store.load_state()
    chapters = [store.load_chapter(chapter.index) for chapter in state.chapters]
    inventory = build_layout_inventory(source_path, chapters)
    raw = store.load_layout_profile()
    if raw is None:
        return inventory, None
    try:
        profile = LayoutProfile.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return inventory, None
    inventory_ledger = {
        (node.node_id, node.resource_href, node.path, node.source_sha256)
        for node in inventory.nodes
    }
    profile_ledger = {
        (item.node_id, item.resource_href, item.path, item.source_sha256)
        for item in profile.assignments
    }
    if (
        profile.source_sha256 != inventory.source_sha256
        or profile.inventory_digest != inventory.digest
        or profile.policy_version != inventory.policy_version
        or profile_ledger != inventory_ledger
    ):
        return inventory, None
    return inventory, profile


class LayoutNode:
    """分析原始 EPUB 结构；检查点可恢复，已接受 profile 只在成功后替换。"""

    node_id = NODE_LAYOUT
    scope = SCOPE_BOOK

    def __init__(
        self,
        *,
        analyzer,
        config,
        inventory: LayoutInventory,
        profile: LayoutProfile | None,
        refresh: bool = False,
    ) -> None:
        self.analyzer = analyzer
        self.config = config
        self.inventory = inventory
        self.profile = profile
        self.refresh = refresh

    def execute(self, request: NodeRequest) -> NodeOutcome:
        if self.profile is not None and not self.refresh:
            request.shared.layout_profile = self.profile
            return NodeOutcome(
                fingerprint=self.profile.digest,
                artifacts={"profile": self.profile},
            )
        mode = "refresh" if self.refresh else "automatic"
        base_profile_digest = (
            self.profile.digest if self.refresh and self.profile is not None else None
        )
        analyst_fingerprint = _analyst_fingerprint(self.config)
        checkpoint = None
        attempt_id = uuid4().hex
        accepted_attempt_id = (
            self.profile.provenance.get("analysis_attempt_id") if self.profile is not None else None
        )
        raw = request.store.load_layout_work()
        if (
            isinstance(raw, dict)
            and raw.get("analyst_fingerprint") == analyst_fingerprint
            and raw.get("mode") == mode
            and raw.get("base_profile_digest") == base_profile_digest
        ):
            candidate = raw.get("checkpoint")
            candidate_attempt_id = raw.get("attempt_id")
            if (
                isinstance(candidate, dict)
                and _valid_attempt_id(candidate_attempt_id)
                and candidate_attempt_id != accepted_attempt_id
            ):
                checkpoint = candidate
                attempt_id = candidate_attempt_id

        def save_checkpoint(value: dict) -> None:
            request.store.write_json(
                request.store.layout_work_path,
                {
                    "attempt_id": attempt_id,
                    "analyst_fingerprint": analyst_fingerprint,
                    "mode": mode,
                    "base_profile_digest": base_profile_digest,
                    "checkpoint": value,
                },
            )

        if self.inventory.nodes:
            request.store.log_event(
                "layout_analysis_started",
                observations=len(self.inventory.nodes),
                refresh=self.refresh,
                resumed=checkpoint is not None,
            )
            if request.progress:
                request.progress(0, 0, "正在分析 EPUB 排版，会调用模型并可能产生费用")
        else:
            request.store.log_event("layout_analysis_empty")
            if request.progress:
                request.progress(0, 0, "EPUB 排版中没有需要模型分析的内容")
        profile = analyze_layout(
            self.inventory,
            self.analyzer,
            checkpoint=checkpoint,
            save_checkpoint=save_checkpoint,
        )
        profile = replace(
            profile,
            provenance={
                **profile.provenance,
                "analysis_attempt_id": attempt_id,
                "analyst_configuration": {"candidates": list(self.config.llm.models.analyst)},
            },
        )
        request.store.write_json(request.store.layout_profile_path, profile.to_dict())
        request.shared.layout_profile = profile
        return NodeOutcome(fingerprint=profile.digest, artifacts={"profile": profile})


__all__ = ["LayoutNode", "current_layout_state"]
