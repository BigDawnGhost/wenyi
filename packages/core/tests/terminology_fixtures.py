"""Shared offline terminology responses and temporary source fixtures."""

import json
from typing import Generic, TypeVar

from wenyi_core.agents.terminology import TerminologyAgent
from wenyi_core.config import Config
from wenyi_core.glossary.evidence_models import DiscoveryResult, KnowledgeResult, SourcePassage
from wenyi_core.glossary.store import GlossaryTerm, source_matches_text
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_core.llm.base import LLMClient
from wenyi_core.storage.file import FileStorage

from tests.fake_llm import routing_handler

PASSAGES = [
    SourcePassage(reference="c0:s0:0-24", chapter=0, segment=0, source="Ann is Bob's mother.")
]
TERM = GlossaryTerm("Ann", "安")
NAMES = {"Alice": "艾丽丝", "Beth": "贝丝", "Carl": "卡尔"}
ClientT = TypeVar("ClientT", bound=LLMClient)


class FakeClient(LLMClient):
    def __init__(self, data):
        super().__init__()
        self.data = data
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return json.dumps(self.data)


class ResponseAgent(TerminologyAgent, Generic[ClientT]):
    """Use the real agent with a typed, inspectable response client."""

    def __init__(self, client: ClientT, config: Config):
        super().__init__(client, config)
        self.client = client


def agent(data):
    return ResponseAgent(FakeClient(data), Config(source_lang="en", target_lang="zh"))


class EvidenceAgent(TerminologyAgent):
    def __init__(self):
        super().__init__(FakeClient({}), Config(source_lang="en", target_lang="zh"))
        self.calls = []
        self.fail_evidence = False
        self.empty_evidence = False

    def discover(self, passages, existing):
        self.calls.append(("discover", tuple(p.reference for p in passages)))
        return DiscoveryResult(
            terms=[
                GlossaryTerm("Alice", "艾丽丝", type="person"),
                GlossaryTerm("Carl", "卡尔", type="person"),
            ],
        )

    def enrich(self, term, passages):
        self.calls.append(("evidence", tuple(p.reference for p in passages)))
        if self.fail_evidence:
            raise RuntimeError("Injected evidence interruption")
        if self.empty_evidence:
            return KnowledgeResult()
        return KnowledgeResult(
            note=f"{term.target}：根据全书原文确认的人物信息。",
            evidence_refs=[p.reference for p in passages],
        )

    def merge(self, term, results):
        self.calls.append(("merge", len(results)))
        return KnowledgeResult(
            note=results[0].note,
            evidence_refs=list(
                dict.fromkeys(ref for item in results for ref in item.evidence_refs)
            ),
        )


def book_store(tmp_path):
    chapters = [
        Chapter(index=0, segments=[Segment(index=7, source="Alice waited for Carl, her uncle.")]),
        Chapter(
            index=1,
            segments=[
                Segment(index=3, source="Beth was Alice's mother."),
                Segment(index=9, source="Carl was Beth's elder brother."),
            ],
        ),
    ]
    store = FileStorage(str(tmp_path / "run"))
    source = tmp_path / "source.txt"
    source.write_text("\n".join(s.source for chapter in chapters for s in chapter.segments))
    store.init_from_document(
        Document(
            title="Invented family",
            source_lang="en",
            target_lang="zh",
            fmt="text",
            source_path=str(source),
            chapters=chapters,
        )
    )
    return store, chapters


class FamilyHandler:
    def __init__(self, *, interrupt_postcheck=False):
        self.interrupt_postcheck = interrupt_postcheck
        self.translation_inputs = []

    def __call__(self, messages, tier, json_mode):
        system, user = messages[0]["content"], messages[-1]["content"]
        if "pre-translation analyst" in system:
            return json.dumps(
                {
                    "style_guide": "Keep the narrator's restraint.",
                    "characters": [
                        {"source": "Alice", "target": NAMES["Alice"], "note": "LOCAL ALICE"}
                    ],
                    "terms": [],
                }
            )
        if system.startswith("You discover terminology"):
            payload = json.loads(user.split("\n", 1)[1])
            terms = []
            for name, target in NAMES.items():
                refs = [
                    p["reference"]
                    for p in payload["passages"]
                    if source_matches_text(name, p["source"])
                ]
                if refs:
                    terms.append(
                        {"source": name, "target": target, "type": "person", "evidence_refs": refs}
                    )
            return json.dumps({"terms": terms})
        if system.startswith("You extract terminology knowledge"):
            payload = json.loads(user.split("\n", 1)[1])
            return json.dumps(
                {
                    "note": payload["term"]["target"] + "的全书人物说明。",
                    "evidence_refs": [p["reference"] for p in payload["passages"]],
                }
            )
        if "terminology" in system and "extractor" in system:
            if self.interrupt_postcheck:
                self.interrupt_postcheck = False
                raise KeyboardInterrupt("after saved body, before extraction checkpoint")
            return '{"terms":[]}'
        if "literary translator" in system:
            self.translation_inputs.append(user)
        return routing_handler(messages, tier, json_mode)


def inputs(tmp_path, *, enabled=True):
    source = tmp_path / "family.txt"
    source.write_text(
        "# First\n\nAlice waited for Carl, her uncle.\n\n"
        "# Later\n\nBeth was Alice's mother.\n\nCarl was Beth's elder brother.\n",
        encoding="utf-8",
    )
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {
                "book_understanding": False,
                "terminology_context": enabled,
                "review": False,
                "polish": False,
            },
            "paths": {"state_dir": str(tmp_path / "state")},
        }
    )
    return source, config, FileStorage(str(tmp_path / "run"))
