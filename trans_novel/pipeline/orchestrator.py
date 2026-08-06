"""ç¼–æ’å™¨ï¼šé©±åŠ¨å…¨æµç¨‹ï¼Œç« çº§çŠ¶æ€æœº + æ–­ç‚¹ç»­è·‘ã€‚

å•ç« ç¿»è¯‘æµæ°´çº¿ï¼ˆç« å†…æ‰¹æ¬¡**ä¸²è¡Œ**ï¼Œé€æ‰¹åˆ·æ–°æ»šåŠ¨ä¸Šä¸‹æ–‡ä¸æœ¯è¯­å¿«ç…§ï¼›è·¨ç« äº¦ä¸²è¡Œä¼ é€’æ¢—æ¦‚ï¼‰ï¼š
  æ¯æ‰¹ï¼šæ¸²æŸ“ä¸Šä¸‹æ–‡ï¼ˆå«å‰ä¸€æ‰¹åˆšè¯‘å‡ºçš„è¯‘æ–‡ï¼‰â†’ ç¿»è¯‘ï¼ˆå¯¹é½ä¿è¯ï¼‰â†’ æ¶¦è‰²ï¼ˆå¯é€‰ï¼‰â†’
        å«æ³¨é‡Šé€»è¾‘æ®µå®šç¨¿å¹¶ä¸²è¡Œå®šä½é“¾æ¥ â†’ æœ¯è¯­/ç§°å‘¼/å›ºå®šè¡¨è¾¾å®æ—¶æŠ½å–å…¥åº“ â†’
        ç«‹å³ä¾›ä¸‹ä¸€æ‰¹å‚ç…§ã€‚
  ç« æœ«ï¼šå…¶ä½™æ®µè½æ ‡ç‚¹è§„èŒƒåŒ– â†’ å…¨ç« æœ¯è¯­å…œåº•æŠ½å– â†’ å›è¯‘æŠ½æ£€ â†’ è½ç›˜æ ‡è®° doneã€‚
ç¿»è¯‘å‰å…ˆé¢„æ‰«æºæ–‡å»ºç«‹å…¨ä¹¦ç†è§£ï¼ˆé€ç« æ¢—æ¦‚+å…¨ä¹¦æ¦‚è§ˆï¼Œfast æ¡£å¹¶è¡Œï¼‰ï¼Œä½œæ’å®šå‰ç¼€æ³¨å…¥æ¯ç« ç¿»è¯‘ã€‚

å…¨ä¹¦ç¿»è¯‘å®Œæˆåï¼Œç‹¬ç«‹ Review é˜¶æ®µä½¿ç”¨æœ€ç»ˆæœ¯è¯­åº“æŒ‰ç« å¹¶è¡Œå®¡æ ¡ï¼›å€™é€‰é—®é¢˜è¿›å…¥
æœ‰ç•Œ Agent Loop æŒ‰éœ€æ£€ç´¢å…¨ä¹¦è¯æ®ï¼Œè·¨å—çŸ›ç›¾å»ºè®®å†ç»Ÿä¸€ä»²è£ã€‚ç»“æœå†™å…¥ç‹¬ç«‹çš„
æ­£å¼ Review ç›®å½•ï¼Œä¸æ”¹æ­£æ–‡ï¼›run_all éšåä»ä»¥æ­£å¼ç« èŠ‚æ‰§è¡Œä¸€è‡´æ€§ QAã€æŠ¥å‘Šå’Œå¯¼å‡ºã€‚
è¿›åº¦å›è°ƒ progress(done_segments, total_segments, label) ä¸ UI æ— å…³ï¼Œæ¯æ‰¹å®Œæˆå³è§¦å‘ã€‚
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from ..agents.analyzer import Analyzer
from ..agents.annotation_aligner import (
    AnnotationAligner,
    AnnotationUnit,
    target_digest,
)
from ..agents.polisher import Polisher
from ..agents.review_fixer import (
    ProvisionalPatch,
    ReviewFixer,
    ReviewFixerProtocolError,
)
from ..agents.review_loop import (
    ReviewAgentLoop,
    ReviewConflictArbiter,
    apply_review_arbitrations,
    build_conflict_groups,
    normalize_review_issues,
)
from ..agents.reviewer import BackTranslator, Reviewer, ReviewOutputError
from ..agents.synopsis import Synopsizer
from ..agents.translator import Translator
from ..config import Config
from ..glossary.extractor import GlossaryExtractor, TranslatedSegmentEvidence
from ..glossary.store import GlossaryStore, GlossaryTerm
from ..ingest.models import Chapter, Segment
from ..ingest.segmenter import batch_segments, load_document
from ..llm.base import LLMClient
from ..llm.factory import build_client
from ..llm.usage import merge_usage_summaries, usage_delta
from ..postprocess.punct import normalize_zh_segments
from .context import RollingContext
from .eta import PipelineETAEstimator, ProgressEstimate
from .review_evidence import BookEvidenceIndex
from .review_run import ReviewOutcome, ReviewRunStore
from .runstore import STATUS_DONE, RunStore, slugify

ProgressFn = Callable[[int, int, str], None]


# è¯­è¨€å/ä»£ç  â†’ ISO 639-1 ä¸¤å­—æ¯ä»£ç ï¼ˆæ¨¡å‹æ£€æµ‹ç»“æœå½’ä¸€åŒ–ï¼‰
_LANG_ALIASES = {
    "japanese": "ja",
    "æ—¥è¯­": "ja",
    "æ—¥æ–‡": "ja",
    "jp": "ja",
    "jpn": "ja",
    "english": "en",
    "è‹±è¯­": "en",
    "è‹±æ–‡": "en",
    "eng": "en",
    "russian": "ru",
    "ä¿„è¯­": "ru",
    "ä¿„æ–‡": "ru",
    "rus": "ru",
    "chinese": "zh",
    "ä¸­æ–‡": "zh",
    "æ±‰è¯­": "zh",
    "zh-cn": "zh",
    "zho": "zh",
    "korean": "ko",
    "éŸ©è¯­": "ko",
    "éŸ©æ–‡": "ko",
    "kor": "ko",
    "french": "fr",
    "æ³•è¯­": "fr",
    "æ³•æ–‡": "fr",
    "german": "de",
    "å¾·è¯­": "de",
    "å¾·æ–‡": "de",
    "spanish": "es",
    "è¥¿ç­ç‰™è¯­": "es",
    "è¥¿ç­ç‰™æ–‡": "es",
    "italian": "it",
    "æ„å¤§åˆ©è¯­": "it",
    "æ„å¤§åˆ©æ–‡": "it",
    "portuguese": "pt",
    "è‘¡è„ç‰™è¯­": "pt",
    "è‘¡è„ç‰™æ–‡": "pt",
}


def _normalize_lang(code: str) -> str:
    """æŠŠæ¨¡å‹è¿”å›çš„è¯­è¨€åæˆ–åˆ«åè§„æ•´ä¸º ISO 639-1 ä¸¤å­—æ¯ä»£ç ã€‚"""
    c = (code or "").strip().lower()
    if not c or c in {"auto", "unknown", "und", "uncertain", "mixed", "å¤šè¯­è¨€", "æœªçŸ¥"}:
        return ""
    if c in _LANG_ALIASES:
        return _LANG_ALIASES[c]
    return c[:2] if c[:2].isalpha() else ""


def _resume_batches(segments, max_chars: int) -> list[list]:
    """æŒ‰å­—ç¬¦é¢„ç®—åˆ†æ‰¹åï¼Œå†æ²¿â€œå·²å®Œæˆ/å¾…ç¿»è¯‘â€è¾¹ç•Œåˆ‡å¼€ã€‚

    ç”¨æˆ·è°ƒæ•´æ‰¹æ¬¡é¢„ç®—æ—¶ï¼Œæ–°çš„æ‰¹æ¬¡å¯èƒ½åŒæ—¶åŒ…å«å·²æœ‰è¯‘æ–‡å’Œç©ºè¯‘æ–‡ã€‚è‹¥ç›´æ¥é‡è·‘
    è¯¥æ··åˆæ‰¹æ¬¡ä¼šè¦†ç›–å·²ç¡®è®¤å†…å®¹ï¼›æŒ‰å®ŒæˆçŠ¶æ€åˆ†ç»„å¯åªè¡¥è¯‘ç¼ºå¤±æ®µã€‚
    """
    batches: list[list] = []
    for raw_batch in batch_segments(segments, max_chars):
        current: list = []
        current_done: bool | None = None
        for segment in raw_batch:
            done = bool(segment.target and segment.target.strip())
            if current and done != current_done:
                batches.append(current)
                current = []
            current.append(segment)
            current_done = done
        if current:
            batches.append(current)
    return batches


@dataclass
class _BatchResult:
    targets: list[str]
    bt_samples: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class _ReviewRoundResult:
    """ä¸€æ¬¡å…¨ä¹¦å½±å­è¯‘æ–‡ Review åŠå†²çªä»²è£åçš„ç¡®å®šæ€§ç»“æœã€‚"""

    issues: list[dict[str, Any]]
    pre_arbitration_issues: list[dict[str, Any]]
    arbitration_superseded: list[dict[str, Any]]
    conflict_groups: list[dict[str, Any]]
    residual_conflicts: list[dict[str, Any]]
    fallback_agent_count: int


def _review_overlay_digest(
    chapters,
    overrides: Mapping[tuple[int, int], str],
) -> str:
    """è®¡ç®—å…¨ä¹¦æœ‰æ•ˆå½±å­è¯‘æ–‡æŒ‡çº¹ï¼Œç”¨äºæ£€æµ‹æ— è¿›å±•ä¸ Aâ†”B æŒ¯è¡ã€‚"""
    payload = [
        (
            chapter.index,
            text_index,
            overrides.get((chapter.index, text_index), segment.target or ""),
        )
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_content_digest(chapters) -> str:
    """è®¡ç®—æœ¬æ¬¡ Review å®é™…è¯»å–çš„æ­£å¼æ­£æ–‡æ‘˜è¦ã€‚"""
    payload = [
        (
            chapter.index,
            text_index,
            segment.index,
            segment.anchor or "",
            segment.kind,
            segment.source,
            segment.target or "",
        )
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_net_changes(
    chapters,
    overrides: Mapping[tuple[int, int], str],
    patch_records: list[dict[str, Any]],
    active_patches: Mapping[tuple[int, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    """æŠŠå¤šè½®å½±å­è¡¥ä¸æŠ˜å æˆæ¯æ®µä¸€æ¡çš„æœ€ç»ˆä¿®æ”¹å»ºè®®ã€‚"""
    baseline = {
        (chapter.index, text_index): segment.target or ""
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    }
    issue_keys_by_location: dict[tuple[int, int], set[str]] = {}
    for patch in patch_records:
        chapter = patch.get("chapter")
        index = patch.get("index")
        if (
            not isinstance(chapter, int)
            or isinstance(chapter, bool)
            or not isinstance(index, int)
            or isinstance(index, bool)
            or patch.get("status") == "rejected_cycle"
        ):
            continue
        keys = issue_keys_by_location.setdefault((chapter, index), set())
        keys.update(str(key) for key in patch.get("issue_keys", []) if isinstance(key, str) and key)

    changes: list[dict[str, Any]] = []
    for location, suggested_target in sorted(overrides.items()):
        if baseline.get(location) == suggested_target:
            continue
        active = active_patches.get(location) or {}
        changes.append(
            {
                "chapter": location[0],
                "index": location[1],
                "suggested_target": suggested_target,
                "issue_keys": sorted(issue_keys_by_location.get(location, set())),
                "review_result": str(active.get("status") or "provisional"),
            }
        )
    return changes


def _review_public_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """è£å‰ªå†…éƒ¨å®¡æ ¡å­—æ®µï¼Œç”Ÿæˆé¢å‘ç”¨æˆ·çš„ç¨³å®šé—®é¢˜åˆ—è¡¨ã€‚"""
    public: dict[str, dict[str, Any]] = {}
    for issue in issues:
        issue_key = issue.get("issue_key")
        chapter = issue.get("chapter")
        index = issue.get("index")
        if (
            not isinstance(issue_key, str)
            or not issue_key
            or not isinstance(chapter, int)
            or isinstance(chapter, bool)
            or not isinstance(index, int)
            or isinstance(index, bool)
        ):
            continue
        public[issue_key] = {
            "issue_key": issue_key,
            "chapter": chapter,
            "index": index,
            "type": str(issue.get("type") or ""),
            "detail": str(issue.get("detail") or ""),
            "suggestion": str(issue.get("suggestion") or ""),
        }
    return sorted(
        public.values(),
        key=lambda issue: (issue["chapter"], issue["index"], issue["issue_key"]),
    )


def _review_conflict_records(
    groups: list[dict[str, Any]],
    arbitrations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """æŠŠå†²çªç»„åŠå¯¹åº”ä»²è£ç»“æœåºåˆ—åŒ–ä¸ºç¨³å®šçš„é€è½®è®°å½•ã€‚"""
    return [
        {
            "conflict_id": group["conflict_id"],
            "consistency_key": group["consistency_key"],
            "issue_ids": [issue["issue_id"] for issue in group["issues"]],
            "proposals": [
                {
                    "issue_id": issue["issue_id"],
                    "chapter": issue["chapter"],
                    "index": issue["index"],
                    "proposed_value": issue["consistency"]["proposed_value"],
                }
                for issue in group["issues"]
            ],
            "arbitration": arbitration,
        }
        for group, arbitration in zip(groups, arbitrations)
    ]


def _review_unresolved_conflict_records(
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """ä»æœ€ç»ˆæœªè§£å†³é—®é¢˜é‡å»ºå†²çªè®°å½•ï¼Œé¿å…è¢«æœ€åä¸€è½®ç©ºç»“æœæ©ç›–ã€‚"""
    groups = build_conflict_groups(issues)
    arbitrations: list[dict[str, Any]] = []
    for group in groups:
        issue_ids = [str(issue["issue_id"]) for issue in group["issues"]]
        annotations = [
            issue.get("arbitration")
            for issue in group["issues"]
            if isinstance(issue.get("arbitration"), dict)
        ]
        reasons = [
            str(annotation.get("reason", "")).strip()
            for annotation in annotations
            if str(annotation.get("reason", "")).strip()
        ]
        evidence_refs = sorted(
            {
                str(ref)
                for issue in group["issues"]
                for ref in issue.get("evidence_refs", [])
                if isinstance(ref, str) and ref
            }
        )
        arbitrations.append(
            {
                "conflict_id": group["conflict_id"],
                "consistency_key": group["consistency_key"],
                "issue_ids": issue_ids,
                "status": "unresolved",
                "recommended_value": "",
                "reason": reasons[-1] if reasons else "æœ€ç»ˆæœªè§£å†³é—®é¢˜ä»åŒ…å«äº’æ–¥å»ºè®®ã€‚",
                "supported_issue_ids": issue_ids,
                "rejected_issue_ids": [],
                "evidence_refs": evidence_refs,
            }
        )
    return _review_conflict_records(groups, arbitrations)


def _review_unresolved_fallback_count(issues: list[dict[str, Any]]) -> int:
    """ç»Ÿè®¡æœ€ç»ˆæœªè§£å†³é—®é¢˜ä¸­ä»ç”±é™çº§ Agent äº§ç”Ÿçš„ç‹¬ç«‹å®¡æ ¡å—ã€‚"""
    return len(
        {
            str(issue.get("_chunk_id") or issue.get("issue_key") or issue.get("issue_id"))
            for issue in issues
            if issue.get("agent_fallback")
        }
    )


class Orchestrator:
    def __init__(self, config: Config, client: LLMClient | None = None):
        """åˆå§‹åŒ–å…±äº« LLM å®¢æˆ·ç«¯ã€ç”¨é‡æ£€æŸ¥ç‚¹å’Œå„æµæ°´çº¿ Agentã€‚"""
        self.config = config
        self.client = client or build_client(config)
        self._eta = PipelineETAEstimator(self.client.performance)
        self._eta_expected_steps: set[str] = set()
        self._eta_only_chapter: int | None = None
        # client çš„ç»Ÿè®¡æ˜¯è¿›ç¨‹å†…ç´¯è®¡ï¼›checkpoint ç”¨äºæ¯æ¬¡è½ç›˜æ—¶åªæå–æ–°å¢éƒ¨åˆ†ã€‚
        self._usage_checkpoint = self.client.usage_summary()
        self.analyzer = Analyzer(self.client, config)
        self.synopsizer = Synopsizer(self.client, config)
        self.translator = Translator(self.client, config)
        self.reviewer = Reviewer(self.client, config)
        self.backtrans = BackTranslator(self.client, config)
        self.polisher = Polisher(self.client, config)
        self.extractor = GlossaryExtractor(self.client, config)
        self.annotation_aligner = AnnotationAligner(self.client, config)

    def _bind_llm_events(self, store: RunStore) -> None:
        """æŠŠ provider é‡è¯•äº‹ä»¶å®æ—¶å†™å…¥å½“å‰ä¹¦ç±çš„è¿½åŠ å¼äº‹ä»¶æ—¥å¿—ã€‚"""
        self.client.set_event_sink(store.log_event)

    def progress_estimate(self) -> ProgressEstimate:
        """è¿”å›å½“å‰å‘½ä»¤çš„é˜¶æ®µ/å…¨ç¨‹ ETA ä¸æœ‰æ•ˆ token é€Ÿåº¦ã€‚"""
        return self._eta.snapshot()

    def _track_progress(self, progress: ProgressFn | None) -> ProgressFn | None:
        """ä¸ºæ—¢æœ‰ä¸‰å‚æ•°è¿›åº¦å›è°ƒé™„åŠ  ETA è§‚æµ‹ï¼Œä¿æŒå…¬å¼€ç­¾åä¸å˜ã€‚"""
        return self._eta.track(progress)

    def _punctuation_enabled(self) -> bool:
        """åˆ¤æ–­å½“å‰ç›®æ ‡è¯­è¨€æ˜¯å¦åº”å¯ç”¨ä¸­æ–‡æ ‡ç‚¹è§„èŒƒåŒ–ã€‚"""
        target = (self.config.target_lang or "").lower().replace("_", "-")
        return self.config.punctuation_normalize and (target == "zh" or target.startswith("zh-"))

    def _flush_usage(self, store: RunStore, *, scope: str) -> dict[str, Any]:
        """æŠŠå½“å‰ client å°šæœªè½ç›˜çš„ç”¨é‡å¢é‡åˆå¹¶åˆ°æœ¬ä¹¦ usage.jsonã€‚"""
        current = self.client.usage_summary()
        increment = usage_delta(current, self._usage_checkpoint)
        self._usage_checkpoint = current
        accumulated = store.load_usage() or {
            "totals": {},
    ßŞ<êÚ$z{-®éÜj×&WGW&â&W7VÇ@Ğ¢&V6÷&E÷&V6÷fW'’€Ğ¢'&Wf–Wu÷6–ævÆWFöåöf–ÆVB"ÀĞ¢7F'Eö–æFWƒÖ6‡Væµö&6RÀĞ¢6÷VçCÓÀĞ¢GFV×G3×&WG&–W2²ÀĞ¢&V6öãÖÆ7EöW'&÷"ç&V6öâÀĞ¢Ğ¢&—6RÆ7EöW'&÷ Ğ Ğ¢FVb&Wf–WuööæR†¦ö#¢GWÆU¶–çBÂÆ—7EÒ’ÓâÆ—7E¶F–7EÓ Ğ¢"".Zêj
KˆKŠ®X‰ŞZx¾‹ùî{ºŞYÙ~ûÈÎ[›nYÊ[ø^Šhi{nhš~ŠÎ[˜:h.ZHŞ8""" Ğ¢6‡Væµö&6RÂ6‡Væ²Ò¦ö Ğ¢&WGW&â&Wf–WuöFF—fR†6‡Væµö&6RÂ6‡Væ²Ğ Ğ¢v÷&¶W'2ÒÖ–â€Ğ¢Ö‚ƒÂ6VÆbæ6öæf–rç—VÆ–æRç&Wf–Wuö6öæ7W'&Væ7’’ÀĞ¢ÆVâ†¦ö'2’ÀĞ¢Ğ¢G'“ Ğ¢–bv÷&¶W'2ÓÒ Ğ¢&W7VÇG2ÒµĞĞ¢f÷"¦ö"–â¦ö'3 Ğ¢&W7VÇG2æVæB‡&Wf–WuööæR†¦ö"’Ğ¢–böåö6‡Væµöf–æ—6†VC Ğ¢öåö6‡Væµöf–æ—6†VB€Ğ¢ÆVâ†¦ö%³Ò’ÀĞ¢ÀĞ¢Ğ¢VÇ6S Ğ¢÷&FW&VE÷&W7VÇG3¢Æ—7E¶Æ—7E¶F–7EÒÂæöæUÒÒ´æöæUÒ¢ÆVâ†¦ö'2Ğ¢v—F‚F‡&VEööÄW†V7WF÷"†Ö…÷v÷&¶W'3×v÷&¶W'2’2Wƒ Ğ¢gWGW&W2Ò°Ğ¢W‚ç7V&Ö—B‡&Wf–WuööæRÂ¦ö"“¢€Ğ¢÷6—F–öâÀĞ¢ÆVâ†¦ö%³Ò’ÀĞ¢ÀĞ¢Ğ¢f÷"÷6—F–öâÂ¦ö"–âVçVÖW&FR†¦ö'2Ğ¢ĞĞ¢f÷"gWGW&R–â5ö6ö×ÆWFVB†gWGW&W2“ Ğ¢÷6—F–öâÂ6VvÖVçEö6÷VçBÂv÷&²ÒgWGW&W5¶gWGW&UĞĞ¢÷&FW&VE÷&W7VÇG5·÷6—F–öåÒÒgWGW&Rç&W7VÇB‚Ğ¢–böåö6‡Væµöf–æ—6†VC Ğ¢öåö6‡Væµöf–æ—6†VB‡6VvÖVçEö6÷VçBÂv÷&²Ğ¢&W7VÇG2Ò·&W7VÇBf÷"&W7VÇB–â÷&FW&VE÷&W7VÇG2–b&W7VÇB—2æ÷BæöæUĞĞ¢f–æÆÇ“ Ğ¢–bFV'Vr—2æ÷BæöæS Ğ¢v—F‚&V6÷fW'•öÆö6³ Ğ¢WfVçEö÷&FW"Ò°Ğ¢'&Wf–Wuö§6öå÷&W—&VB#¢ÀĞ¢'&Wf–Wuö6‡Væµ÷7Æ—B#¢ÀĞ¢'&Wf–Wu÷6–ævÆWFöå÷&WG'’#¢ÀĞ¢'&Wf–Wu÷6–ævÆWFöå÷&V6÷fW&VB#¢"ÀĞ¢'&Wf–Wu÷6–ævÆWFöåöf–ÆVB#¢"ÀĞ¢ĞĞ¢VæF–æuöWfVçG2Ò6÷'FVB€Ğ¢&V6÷fW'•öWfVçG2ÀĞ¢¶W“ÖÆÖ&F&÷s¢€Ğ¢&÷rævWB‚'7F'Eö–æFW‚"ÂÓ’ÀĞ¢×&÷rævWB‚&6÷VçB"Â’ÀĞ¢WfVçEö÷&FW"ævWB‡&÷rævWB‚&WfVçB"Â""’Â“’’ÀĞ¢&÷rævWB‚&GFV×B"Â’ÀĞ¢’ÀĞ¢Ğ¢f÷"&÷r–âVæF–æuöWfVçG3 Ğ¢WfVçBÒ&÷u²&WfVçB%ĞĞ¢–ÆöBÒ°Ğ¢&6†FW"#¢6†FW%ö–æFW‚ÀĞ¢¢§¶¶W“¢fÇVRf÷"¶W’ÂfÇVR–â&÷ræ—FV×2‚’–b¶W’Ò&WfVçB'ÒÀĞ¢ĞĞ¢FV'VræÆöuöWfVçB†WfVçBÂ¢§–ÆöBĞ¢&WGW&â¶—77VRf÷"6‡Væµö—77VW2–â&W7VÇG2f÷"—77VR–â6‡Væµö—77VW5ĞĞ Ğ¢7FF–6ÖWF†ö@Ğ¢FVb÷6µö6öçF–wV÷W2‡6Vw2Â'VFvWC¢–çB’ÓâÆ—7E¶Æ—7EÓ Ğ¢"".hÈk©ih~ZÙ~zÊnš(Nzé~h¨®jë^KùŞ[¨şh™>XÈ^h‰ˆº^[›.‹ùî{ºŞYÙ~8""" Ğ¢6‡Væ·3¢Æ—7E¶Æ—7EÒÒµĞĞ¢7W#¢Æ—7BÒµĞĞ¢6—¦RÒ Ğ¢f÷"2–â6Vw3 Ğ¢–b7W"æB6—¦R²ÆVâ‡2ç6÷W&6R’â'VFvWC Ğ¢6‡Væ·2æVæB†7W"Ğ¢7W"Â6—¦RÒµÒÂ Ğ¢7W"æVæB‡2Ğ¢6—¦R³ÒÆVâ‡2ç6÷W&6RĞ¢–b7W# Ğ¢6‡Væ·2æVæB†7W"Ğ¢&WGW&â6‡Væ·0Ğ Ğ¢FVb÷&ö6W75ö&F6‚€Ğ¢6VÆbÀĞ¢&F6‚ÀĞ¢FW&×2ÀĞ¢7G…÷FW‡C¢7G"ÀĞ¢7G–ÆS¢7G"ÀĞ¢&ööµ÷7–æ÷6—3¢7G"Ò""ÀĞ¢6†FW%öF–vW7C¢7G"Ò""ÀĞ¢’Óâô&F6…&W7VÇC Ğ¢"".XÙ^KŠ®h›jÊûÉ®i[Nh›{û¾Šù(i"kjnˆ›.8 Ğ Ğ¢jøşjë^˜;ŞYÊˆz®‹ª¾Kˆ®Kˆ¾ih~˜xÎ{û¾ŠùûÈÎKˆŞ‹zKØŞ{ÚîZHŞyJŠùih~ûÈ˜şXXŞKŠ.ZKŠúŞZ(>KúhşûÈ8 Ğ¢XZKšnjh.Šx‚şiÊÎzºj)~jh.KÙÎK‹®h.Zé®X˜Ş{Èk:XZ^ûÈÎŠêŠùˆ^h¨®húXZ[8 Ğ¢j~x+ŠxNˆÈ>XÉnYÊzºiÊ¾{¹şKˆhš~ŠÎûÈÎKº^{»NhÈ‹zjë^[É^Xû~x«nh8 Ğ¢ÄÄÒZêj
KˆŞYÊ{û¾Šùh›Xh^X®ûÉ¾XZKšnZèÎh‰YîyKxºÎz¸²&Wf–Wr™‹një^{¹şKˆhš~ŠÎ8 Ğ¢"" Ğ¢6÷W&6W2Ò·2ç6÷W&6Rf÷"2–â&F6…ĞĞ¢F&vWG2Ò6VÆbçG&ç6ÆF÷"çG&ç6ÆFUö&F6‚€Ğ¢6÷W&6W2ÀĞ¢vÆ÷76'•÷FW&×3×FW&×2ÀĞ¢7G–ÆS×7G–ÆRÀĞ¢6öçFW‡CÖ7G…÷FW‡BÀĞ¢&ööµ÷7–æ÷6—3Ö&ööµ÷7–æ÷6—2ÀĞ¢6†FW%öF–vW7CÖ6†FW%öF–vW7BÀĞ¢Ğ Ğ¢–b6VÆbæ6öæf–rç—VÆ–æRçöÆ—6ƒ Ğ¢öÆ—6†VBÒ6VÆbçöÆ—6†W"çöÆ—6‚‡F&vWG2ÂvÆ÷76'•÷FW&×3×FW&×2Â7G–ÆS×7G–ÆRĞ¢–bÆVâ‡öÆ—6†VB’ÓÒÆVâ‡F&vWG2“ Ğ¢F&vWG2ÒöÆ—6†V@Ğ Ğ¢'E÷6×ÆW3¢Æ—7E·GWÆU·7G"Â7G%ÕÒÒµĞĞ¢&FRÒ6VÆbæ6öæf–rç—VÆ–æRæ&6·G&ç6ÆFU÷6×ÆPĞ¢–b&FRâ Ğ¢f÷"2ÂB–â¦—‡6÷W&6W2ÂF&vWG2“ Ğ¢–b&æFöÒç&æFöÒ‚’Â&FS Ğ¢'E÷6×ÆW2æVæB‚‡2ÂB÷"""’Ğ Ğ¢&WGW&âô&F6…&W7VÇB‡F&vWG3×F&vWG2Â'E÷6×ÆW3Ö'E÷6×ÆW2Ğ Ğ¢2)H)HXúş˜jÚ^šªBò‹ùî{ºŞXZkXzˆ²)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H Ğ¢ÄÅõ5DU2Ò‚'G&ç6ÆFR"Â'&Wf–Wr"Â'"Â'&W÷'B"Â&76VÖ&ÆR"Ğ Ğ¢FVb'Vå÷&Wf–Wr€Ğ¢6VÆbÀĞ¢–çWE÷Fƒ¢7G"ÀĞ¢¢ÀĞ¢&öw&W73¢&öw&W74fâÂæöæRÒæöæRÀĞ¢’ÓâF–7E·7G"Âç•Ó Ğ¢"".XZ˜xşhš~ŠÎXú®Šû²&Wf–W~ûÈÎ[›nKùŞZÙjÚ>[Èş{¹>iéÎ8K¨¾K»nKˆîyJ˜xş8""" Ğ¢6VÆbåöWFööæÇ•ö6†FW"ÒæöæPĞ¢6VÆbåöWFöW‡V7FVE÷7FW2æFB‚'&Wf–Wr"Ğ¢&öw&W72Ò6VÆbå÷G&6µ÷&öw&W72‡&öw&W72Ğ¢7F÷&RÒ6VÆbåöÆö6FUöW†—7F–æu÷7F÷&R†–çWE÷F‚Â&öw&W73×&öw&W72Ğ¢6VÆbå÷Æåö&ööµöWF‡7F÷&RĞ¢v—F‚7F÷&RæÆö6²‚“ Ğ¢Öæ–fW7BÒ7F÷&RæÆöEöÖæ–fW7B‚Ğ¢6VÆbåöÇ•öÆæwVvR†Öæ–fW7BævWB‚'6÷W&6UöÆær"’÷"6VÆbæ6öæf–rç6÷W&6UöÆærĞ¢FW&×2ÒvÆ÷76'•7F÷&RæÆöE÷FW&×5÷&VFöæÇ’‡7F÷&RævÆ÷76'•÷F‚Ğ¢÷WF6öÖRÒ6VÆbå÷'Vå÷&Wf–Wu÷6W76–öâ€Ğ¢7F÷&RÀĞ¢FW&×2ÀĞ¢&öw&W73×&öw&W72ÀĞ¢Ğ¢&WGW&â°Ğ¢'7F÷&R#¢7F÷&RÀĞ¢'&Wf–Wuö—77VW2#¢÷WF6öÖRæ—77VW2ÀĞ¢'&Wf–Wuö6†ævW2#¢÷WF6öÖRæ6†ævW2ÀĞ¢'&Wf–Wu÷&W7VÇB#¢÷WF6öÖRç&W7VÇBÀĞ¢'&Wf–WuöF—"#¢÷WF6öÖRç'VåöF—"ÀĞ¢ĞĞ Ğ¢FVb'Vå÷7FW2€Ğ¢6VÆbÀĞ¢–çWE÷Fƒ¢7G"ÀĞ¢7FW2ÀĞ¢¢ÀĞ¢&öw&W73¢&öw&W74fâÂæöæRÒæöæRÀĞ¢÷WEöf÷&ÖC¢7G"Ò&WV""ÀĞ¢÷WE÷Fƒ¢7G"ÂæöæRÒæöæRÀĞ¢FeöVæv–æS¢7G"Ò'vV7—&–çB"ÀĞ¢’ÓâF–7E·7G"Âç•Ó Ğ¢"".hÈ™Èhš~ŠÎjÚ^šªNZÙ™¸nûÈXúşXÙ^˜XúşXZ˜ûÈ8'7FW2(¨bÄÅõ5DU>8""" Ğ¢7FW2Ò6WB‡7FW2Ğ¢6VÆbåöWFööæÇ•ö6†FW"ÒæöæPĞ¢6VÆbåöWFöW‡V7FVE÷7FW2çWFFR‡7FW2Ğ¢&öw&W72Ò6VÆbå÷G&6µ÷&öw&W72‡&öw&W72Ğ¢'Vå÷7FW5ö–çWBÒ6÷'FVB‡7FW2Ğ¢–b7FW2ÓÒ²'&Wf–Wr'Ó Ğ¢&Wf–WvVBÒ6VÆbç'Vå÷&Wf–Wr†–çWE÷F‚Â&öw&W73×&öw&W72Ğ¢&WGW&â°Ğ¢'7F÷&R#¢&Wf–WvVE²'7F÷&R%ÒÀĞ¢&÷WGWB#¢æöæRÀĞ¢&÷WGWG2#¢µÒÀĞ¢'&W÷'B#¢æöæRÀĞ¢'&Wf–Wuö—77VW2#¢&Wf–WvVE²'&Wf–Wuö—77VW2%ÒÀĞ¢'&Wf–Wuö6†ævW2#¢&Wf–WvVE²'&Wf–Wuö6†ævW2%ÒÀĞ¢'&Wf–Wu÷&W7VÇB#¢&Wf–WvVE²'&Wf–Wu÷&W7VÇB%ÒÀĞ¢'&Wf–WuöF—"#¢&Wf–WvVE²'&Wf–WuöF—"%ÒÀĞ¢'ö—77VW2#¢µÒÀĞ¢ĞĞ Ğ¢–b'G&ç6ÆFR"–â7FW3 Ğ¢7F÷&RÒ6VÆbç'Vâ†–çWE÷F‚Â&öw&W73×&öw&W72Ğ¢VÇ6S Ğ¢7F÷&RÒ6VÆbç&W&R†–çWE÷F‚Â&öw&W73×&öw&W72Ğ¢ÒÒ7F÷&RæÆöEöÖæ–fW7B‚Ğ¢6VÆbåöÇ•öÆæwVvR†ÒævWB‚'6÷W&6UöÆær"’÷"6VÆbæ6öæf–rç6÷W&6UöÆærĞ¢v—F‚7F÷&RæÆö6²‚“ Ğ¢&WGW&â6VÆbåöf–æ—6…÷7FW5öÆö6¶VB€Ğ¢7F÷&RÀĞ¢–çWE÷FƒÖ–çWE÷F‚ÀĞ¢7FW3×7FW2ÀĞ¢'Vå÷7FW5ö–çWC×'Vå÷7FW5ö–çWBÀĞ¢&öw&W73×&öw&W72ÀĞ¢÷WEöf÷&ÖCÖ÷WEöf÷&ÖBÀĞ¢÷WE÷FƒÖ÷WE÷F‚ÀĞ¢FeöVæv–æS×FeöVæv–æRÀĞ¢Ğ Ğ¢FVböf–æ—6…÷7FW5öÆö6¶VB€Ğ¢6VÆbÀĞ¢7F÷&S¢'Vå7F÷&RÀĞ¢¢ÀĞ¢–çWE÷Fƒ¢7G"ÀĞ¢7FW3¢6WE·7G%ÒÀĞ¢'Vå÷7FW5ö–çWC¢Æ—7E·7G%ÒÀĞ¢&öw&W73¢&öw&W74fâÂæöæRÀĞ¢÷WEöf÷&ÖC¢7G"ÀĞ¢÷WE÷Fƒ¢7G"ÂæöæRÀĞ¢FeöVæv–æS¢7G"ÀĞ¢’ÓâF–7E·7G"Âç•Ó Ğ¢"".YÊKšn{ª~™HXh^hš~ŠÂ8hª^Y®Y(ÎZûÎX{®iKn[îjÚ^šªN[›n‹ùNY¹î{¹>iéÎk~h¾8""" Ğ¢g&öÒâævVçG2æ6öç6—7FVæ7’–×÷'B6öç6—7FVæ7”6†V6¶W Ğ¢g&öÒâæ76VÖ&ÆRç&W÷'B–×÷'B'V–ÆE÷&W÷'@Ğ¢g&öÒâæ76VÖ&ÆRçw&—FW"–×÷'B76VÖ&ÆRÂ&–Æ–æwVÅö÷WE÷F€Ğ Ğ¢7F÷&RæÆöuöWfVçB‚''Vå÷7FW5÷7F'FVB"Â7FW3×'Vå÷7FW5ö–çWBÂ–çWE÷FƒÖ–çWE÷F‚Ğ Ğ¢vÆ÷76'’Ò€Ğ¢vÆ÷76'•7F÷&R‡7F÷&RævÆ÷76'•÷F‚’–b²'"Â'&W÷'B'Òæ–çFW'6V7F–öâ‡7FW2’VÇ6RæöæPĞ¢Ğ¢&Wf–Wuö—77VW3¢Æ—7E¶F–7EÒÒµĞĞ¢&Wf–Wuö6†ævW3¢Æ—7E¶F–7EÒÒµĞĞ¢&Wf–Wu÷&W7VÇC¢F–7E·7G"Âç•ÒÂæöæRÒæöæPĞ¢&Wf–WuöF—#¢7G"ÂæöæRÒæöæPĞ¢ö—77VW3¢Æ—7E¶F–7EÒÒµĞĞ¢&W÷'C¢F–7E·7G"Âç•ÒÂæöæRÒæöæPĞ¢G'“ Ğ¢–b'&Wf–Wr"–â7FW3 Ğ¢2XXKùŞZÙjÚNX˜Ş™‹një^y¨NZ)î˜xşûÈÎKÛşKÉ®ŠùÒW6vRæ§6öâXú®XÈ^Y
²&Wf–Wr‹>yJ8 Ğ¢6VÆbåöfÇW6…÷W6vR‡7F÷&RÂ66÷SÒ'—VÆ–æR"Ğ¢÷WF6öÖRÒ6VÆbå÷'Vå÷&Wf–Wu÷6W76–öâ€Ğ¢7F÷&RÀĞ¢€Ğ¢vÆ÷76'’æÆÅ÷FW&×2‚Ğ¢–bvÆ÷76'’—2æ÷BæöæPĞ¢VÇ6RvÆ÷76'•7F÷&RæÆöE÷FW&×5÷&VFöæÇ’‡7F÷&RævÆ÷76'•÷F‚Ğ¢’ÀĞ¢&öw&W73×&öw&W72ÀĞ¢Ğ¢&Wf–Wuö—77VW2Ò÷WF6öÖRæ—77VW0Ğ¢&Wf–Wuö6†ævW2Ò÷WF6öÖRæ6†ævW0Ğ¢&Wf–Wu÷&W7VÇBÒ÷WF6öÖRç&W7VÇ@Ğ¢&Wf–WuöF—"Ò÷WF6öÖRç'VåöF— Ğ Ğ¢–b'"–â7FW3 Ğ¢–bvÆ÷76'’—2æöæS¢2&vÖ¢æò6÷fW"ÒyKæVVG2iÚK»nKùŞŠøĞ¢&—6R'VçF–ÖTW'&÷"‚%™ÈŠhiÊşŠúŞ[©2"Ğ¢6VÆbåöWFæ&Vv–å÷7FvR€Ğ¢'"ÀĞ¢Æ&VÃÒ.Kˆˆ{Nh
r"ÀĞ¢F÷FÅ÷v÷&³ÓÀĞ¢¶–æCÒ&6ÆÇ2"ÀĞ¢v÷&¶W'3ÓÀĞ¢F–W#Ò&6†V"ÀĞ¢Ğ¢6VÆbåöWFç6WEö7F—fU÷v÷&²ƒĞ¢–b&öw&W73 Ğ¢&öw&W72ƒÂÂ.Kˆˆ{Nh
r(
b"Ğ¢ö—77VW2Ò6öç6—7FVæ7”6†V6¶W"‡6VÆbæ6Æ–VçBÂ6VÆbæ6öæf–r’æ6†V6²‡7F÷&RÂvÆ÷76'’Ğ¢6VÆbåöWFæGfæ6R‚Ğ¢6VÆbåöWFæf–æ—6…÷7FvR‚'"Ğ¢7F÷&RæÆöuöWfVçB€Ğ¢&6öç6—7FVæ7•÷öf–æ—6†VB"ÀĞ¢—77VUö6÷VçCÖÆVâ‡ö—77VW2’ÀĞ¢—77VW3×ö—77VW2ÀĞ¢Ğ Ğ¢6VÆbåöfÇW6…÷W6vR‡7F÷&RÂ66÷SÒ'—VÆ–æR"Ğ¢–b'&W÷'B"–â7FW3 Ğ¢–bvÆ÷76'’—2æöæS¢2&vÖ¢æò6÷fW"ÒyKæVVG2iÚK»nKùŞŠøĞ¢&—6R'VçF–ÖTW'&÷"‚.hª^Y®yIşh‰™ÈŠhiÊşŠúŞ[©2"Ğ¢–b&öw&W73 Ğ¢&öw&W72ƒÂÂ.yIşh‰hª^Y®(
b"Ğ¢6VÆbåöWFæÖ&µöf–æ—6†–ær‚Ğ¢&W÷'BÒ'V–ÆE÷&W÷'B‡7F÷&RÂvÆ÷76'’Ğ¢&W÷'E²&6öç6—7FVæ7•ö—77VW2%ÒÒö—77VW0Ğ¢7F÷&Rç6fU÷&W÷'B‡&W÷'BĞ¢7F÷&RæÆöuöWfVçB‚'&W÷'E÷6fVB"ÂFƒ×7F÷&Rç&W÷'E÷F‚Ğ¢f–æÆÇ“ Ğ¢–bvÆ÷76'’—2æ÷BæöæS Ğ¢vÆ÷76'’æ6Æ÷6R‚Ğ¢6VÆbåöfÇW6…÷W6vR‡7F÷&RÂ66÷SÒ'—VÆ–æR"Ğ Ğ¢÷WGWG3¢Æ—7E·7G%ÒÒµĞĞ¢–b&76VÖ&ÆR"–â7FW3 Ğ¢–b&öw&W73 Ğ¢&öw&W72ƒÂÂ.Y¹îZ¾Šùih~(
b"Ğ¢6VÆbåöWFæÖ&µöf–æ—6†–ær‚Ğ¢÷WEö6frÒ6VÆbæ6öæf–ræ÷WGW@Ğ¢FõöÖöæòÂFõö&–Æ–æwVÂÒ÷WEö6fræÖöæòÂ÷WEö6fræ&–Æ–æwVÀĞ¢–bæ÷BFõöÖöæòæBæ÷BFõö&–Æ–æwVÃ Ğ¢FõöÖöæòÒG'VR2XYÎ[©^ûÉ¦Ööæòö&–Æ–æwVÂ˜;ŞX[>i{nˆ{>[	Kª~KˆKŠ®XÙ^ŠúŞKª~xšĞ¢–bFõöÖöæó Ğ¢÷WGWG2æVæB€Ğ¢76VÖ&ÆR€Ğ¢7F÷&RÀĞ¢–çWE÷F‚ÀĞ¢÷WE÷FƒÖ÷WE÷F‚ÀĞ¢÷WEöf÷&ÖCÖ÷WEöf÷&ÖBÀĞ¢&–Æ–æwVÃÔfÇ6RÀĞ¢&÷WE÷vSÖ÷WEö6fræ&÷WE÷vRÀĞ¢FeöVæv–æS×FeöVæv–æRÀĞ¢Ğ¢Ğ¢–bFõö&–Æ–æwVÃ Ğ¢&•ö÷WE÷F‚Ò&–Æ–æwVÅö÷WE÷F‚†÷WE÷F‚’–b÷WE÷F‚VÇ6RæöæPĞ¢÷WGWG2æVæB€Ğ¢76VÖ&ÆR€Ğ¢7F÷&RÀĞ¢–çWE÷F‚ÀĞ¢÷WE÷FƒÖ&•ö÷WE÷F‚ÀĞ¢÷WEöf÷&ÖCÖ÷WEöf÷&ÖBÀĞ¢&–Æ–æwVÃÕG'VRÀĞ¢÷&FW#Ö÷WEö6fræ&–Æ–æwVÅö÷&FW"ÀĞ¢&W6W'fU÷6÷W&6U÷7G–ÆSÒ†÷WEö6fræ&–Æ–æwVÅ÷&W6W'fU÷6÷W&6U÷7G–ÆR’ÀĞ¢&÷WE÷vSÖ÷WEö6fræ&÷WE÷vRÀĞ¢FeöVæv–æS×FeöVæv–æRÀĞ¢Ğ¢Ğ¢7F÷&RæÆöuöWfVçB‚&76VÖ&ÆVB"Â÷WGWG3Ö÷WGWG2Â÷WEöf÷&ÖCÖ÷WEöf÷&ÖBĞ Ğ¢7F÷&RæÆöuöWfVçB€Ğ¢''Vå÷7FW5öf–æ—6†VB"ÀĞ¢7FW3×'Vå÷7FW5ö–çWBÀĞ¢÷WGWG3Ö÷WGWG2ÀĞ¢ö—77VUö6÷VçCÖÆVâ‡ö—77VW2’ÀĞ¢Ğ¢&WGW&â°Ğ¢'7F÷&R#¢7F÷&RÀĞ¢&÷WGWB#¢÷WGWG5³Ò–b÷WGWG2VÇ6RæöæRÀĞ¢&÷WGWG2#¢÷WGWG2ÀĞ¢'&W÷'B#¢&W÷'BÀĞ¢'&Wf–Wuö—77VW2#¢&Wf–Wuö—77VW2ÀĞ¢'&Wf–Wuö6†ævW2#¢&Wf–Wuö6†ævW2ÀĞ¢'&Wf–Wu÷&W7VÇB#¢&Wf–Wu÷&W7VÇBÀĞ¢'&Wf–WuöF—"#¢&Wf–WuöF—"ÀĞ¢'ö—77VW2#¢ö—77VW2ÀĞ¢ĞĞ Ğ¢FVb'VåöÆÂ€Ğ¢6VÆbÀĞ¢–çWE÷Fƒ¢7G"ÀĞ¢¢ÀĞ¢&öw&W73¢&öw&W74fâÂæöæRÒæöæRÀĞ¢÷WEöf÷&ÖC¢7G"Ò&WV""ÀĞ¢÷WE÷Fƒ¢7G"ÂæöæRÒæöæRÀĞ¢Fõ÷¢&ööÂÂæöæRÒæöæRÀĞ¢FeöVæv–æS¢7G"Ò'vV7—&–çB"ÀĞ¢’ÓâF–7E·7G"Âç•Ó Ğ¢"".{û¾Šù(i"iÈ{¸Zêj
(i"Kˆˆ{Nh
r(i"hª^Y¢(i"Y¹îZ¾ûÈÎ‹ùNY¹î{¹>iéÎk~h¾8""" Ğ¢7FW2Ò²'G&ç6ÆFR"Â'&W÷'B"Â&76VÖ&ÆR'ĞĞ¢–b6VÆbæ6öæf–rç—VÆ–æRç&Wf–Ws Ğ¢7FW2æFB‚'&Wf–Wr"Ğ¢–bFõ÷–bFõ÷—2æ÷BæöæRVÇ6R6VÆbæ6öæf–rç—VÆ–æRæ6öç6—7FVæ7•÷ Ğ¢7FW2æFB‚'"Ğ¢&WGW&â6VÆbç'Vå÷7FW2€Ğ¢–çWE÷F‚ÀĞ¢7FW2ÀĞ¢&öw&W73×&öw&W72ÀĞ¢÷WEöf÷&ÖCÖ÷WEöf÷&ÖBÀĞ¢÷WE÷FƒÖ÷WE÷F‚ÀĞ¢FeöVæv–æS×FeöVæv–æRÀĞ¢Ğ