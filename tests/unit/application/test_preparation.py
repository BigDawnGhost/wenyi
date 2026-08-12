from __future__ import annotations

import hashlib
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

import pytest

from trans_novel.application.preparation import (
    InitializationEventConfig,
    PreparationCoordinator,
    PreparationPolicy,
)


@dataclass
class _Segment:
    source: str


@dataclass
class _Chapter:
    text_segments: list[_Segment]


@dataclass
class _Document:
    title: str = "Book"
    fmt: str = "txt"
    source_lang: str = "en"
    target_lang: str = "zh"
    chapters: list[_Chapter] = field(
        default_factory=lambda: [_Chapter([_Segment("source text" * 30)])]
    )


@dataclass
class _State:
    run_dir: str
    exists: bool = False
    manifest: dict[str, Any] | None = None


class _FakePort:
    """记录端口调用，以白盒方式验证事务顺序和短路边界。"""

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.states: dict[str, _State] = {}
        self.document = _Document()
        self.initial_hash = hashlib.sha256(b"source").hexdigest()
        self.current_hash = self.initial_hash
        self.detected = "en"
        self.fail_manifest = False

    def state_for_title(self, state_dir: str, title: str, *, create: bool) -> _State:
        self.calls.append(("state", title, create))
        key = f"{state_dir}/{title}"
        state = self.states.setdefault(key, _State(key))
        return state

    def state_lock(self, state: _State):
        self.calls.append("lock")
        return nullcontext()

    @staticmethod
    def state_exists(state: _State) -> bool:
        return state.exists

    @staticmethod
    def state_run_dir(state: _State) -> str:
        return state.run_dir

    @staticmethod
    def source_cache_dir(state: _State) -> str:
        return f"{state.run_dir}/source"

    def bind_state(self, state: _State, progress) -> None:
        del state, progress
        self.calls.append("bind")

    def load_document(self, input_path: str, **kwargs: object) -> _Document:
        self.calls.append(("load", input_path, kwargs))
        return self.document

    def initial_source_hash(self, input_path: str) -> str:
        del input_path
        self.calls.append("initial_hash")
        return self.initial_hash

    def verified_source_hash(self, input_path: str) -> str:
        del input_path
        self.calls.append("verified_hash")
        return self.current_hash

    def ensure_state_source(self, state: _State, input_path: str) -> str:
        del state, input_path
        self.calls.append("ensure_source")
        return self.current_hash

    def begin_initialization(self, state: _State, source_hash: str) -> None:
        del state, source_hash
        self.calls.append("begin")

    def finish_initialization(self, state: _State) -> None:
        del state
        self.calls.append("finish")

    def detect_language(self, document: _Document) -> str:
        del document
        self.calls.append("detect")
        return self.detected

    def apply_language(self, source_lang: str) -> None:
        self.calls.append(("apply_language", source_lang))

    def stage_document(
        self,
        state: _State,
        document: _Document,
        *,
        source_hash: str,
    ) -> dict[str, Any]:
        del state, document, source_hash
        self.calls.append("stage")
        return {"chapters": []}

    def open_glossary(self, state: _State) -> object:
        del state
        self.calls.append("open_glossary")
        return object()

    def close_glossary(self, glossary: object) -> None:
        del glossary
        self.calls.append("close_glossary")

    def analyze(self, sample: str) -> dict[str, Any]:
        assert sample
        self.calls.append("analyze")
        return {"genre": "novel"}

    def sample_text(self, document: _Document) -> str:
        del document
        self.calls.append("sample_text")
        return "custom sample"

    def seed_glossary(self, glossary: object, analysis: object) -> None:
        del glossary, analysis
        self.calls.append("seed_glossary")

    def save_analysis(self, state: _State, analysis: object) -> None:
        del state, analysis
        self.calls.append("save_analysis")

    def save_initial_context(self, state: _State, *, max_recent_keep: int) -> None:
        del state
        self.calls.append(("save_context", max_recent_keep))

    def save_manifest(self, state: _State, manifest: object) -> None:
        self.calls.append("save_manifest")
        if self.fail_manifest:
            raise OSError("disk full")
        state.manifest = dict(manifest)  # type: ignore[arg-type]
        state.exists = True

    def emit_event(self, state: _State, event: str, **attributes: object) -> None:
        del state, attributes
        self.calls.append(("event", event))


def _coordinator(port: _FakePort, *, source_lang: str | None = "en") -> PreparationCoordinator:
    return PreparationCoordinator(
        policy=PreparationPolicy(
            state_dir="state",
            source_lang=source_lang,
            target_lang="zh",
            max_chars_per_segment=1200,
            rolling_context_segments=12,
            initialization_event=InitializationEventConfig(
                review=True,
                polish=False,
                backtranslate_sample=0.1,
                book_understanding=True,
                review_concurrency=2,
                review_output_retries=1,
            ),
        ),
        port=port,
    )


def test_application_preparation_imports_without_legacy_or_graph_runtime() -> None:
    """应用协调器可以被 LangGraph 节点复用，却不反向加载任何运行时。"""
    script = """
import sys
import trans_novel.application.preparation

forbidden = (
    "trans_novel.config",
    "trans_novel.ingest",
    "trans_novel.llm",
    "trans_novel.pipeline.orchestrator",
    "trans_novel.pipeline.runstore",
    "trans_novel.storage",
    "langgraph",
)
loaded = [name for name in forbidden if name in sys.modules]
if loaded:
    raise SystemExit(f"unexpected preparation dependencies: {loaded}")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_pdf_resume_does_not_parse_or_begin_initialization() -> None:
    port = _FakePort()
    state = _State("state/Book", exists=True)
    port.states["state/Book"] = state

    result = _coordinator(port).prepare("Book.PDF")

    assert result is state
    assert "ensure_source" in port.calls
    assert not any(isinstance(call, tuple) and call[0] == "load" for call in port.calls)
    assert "begin" not in port.calls


def test_initialization_commits_manifest_last_and_then_clears_marker() -> None:
    port = _FakePort()

    state = _coordinator(port).prepare("book.txt")

    assert state.manifest == {"chapters": [], "initialized": True}
    assert port.calls.index("begin") < port.calls.index("stage")
    assert port.calls.index("stage") < port.calls.index("save_manifest")
    assert port.calls.index("sample_text") < port.calls.index("analyze")
    assert port.calls.index("save_manifest") < port.calls.index("finish")
    assert port.calls.index("finish") < port.calls.index(("event", "run_initialized"))
    assert ("save_context", 40) in port.calls


def test_coordinator_obtains_style_sample_through_the_port_seam() -> None:
    port = _FakePort()
    observed: list[str] = []

    def analyze(sample: str) -> dict[str, Any]:
        observed.append(sample)
        return {}

    port.analyze = analyze  # type: ignore[method-assign]

    _coordinator(port).prepare("book.txt")

    assert observed == ["custom sample"]


def test_direct_locked_compatibility_call_hashes_source_when_digest_is_missing() -> None:
    port = _FakePort()
    coordinator = _coordinator(port)
    state = _State("state/Book")

    coordinator.initialize_locked(
        port.document,
        state,
        "book.txt",
        None,
        source_hash=None,
    )

    assert port.calls[0] == "verified_hash"
    assert port.calls[1] == "begin"
    assert state.exists


def test_pdf_preserves_preconversion_and_locked_initialization_markers() -> None:
    port = _FakePort()

    _coordinator(port).prepare("Book.pdf")

    # The legacy path deliberately refreshes the marker once before expensive
    # conversion and once at the common locked initialization boundary.
    assert port.calls.count("begin") == 2
    first_begin = port.calls.index("begin")
    load_index = next(
        index
        for index, call in enumerate(port.calls)
        if isinstance(call, tuple) and call[0] == "load"
    )
    second_begin = port.calls.index("begin", first_begin + 1)
    assert first_begin < load_index < second_begin


def test_prepare_invokes_the_legacy_locked_initializer_seam() -> None:
    port = _FakePort()
    observed: list[tuple[object, ...]] = []

    def compatibility_initializer(*args: object, **kwargs: object) -> _State:
        observed.append((*args, kwargs))
        return args[1]  # type: ignore[return-value]

    coordinator = PreparationCoordinator(
        policy=_coordinator(port).policy,
        port=port,
        locked_initializer=compatibility_initializer,
    )

    state = coordinator.prepare("book.txt")

    assert state.run_dir == "state/Book"
    assert len(observed) == 1
    assert observed[0][-1] == {"source_hash": port.initial_hash}
    assert "begin" not in port.calls


def test_manifest_failure_leaves_initialization_marker_and_closes_glossary() -> None:
    port = _FakePort()
    port.fail_manifest = True

    with pytest.raises(OSError, match="disk full"):
        _coordinator(port).prepare("book.txt")

    assert "save_manifest" in port.calls
    assert "finish" not in port.calls
    assert ("event", "run_initialized") not in port.calls
    assert port.calls[-1] == "close_glossary"


def test_pdf_source_change_is_rejected_before_document_staging() -> None:
    port = _FakePort()
    port.current_hash = hashlib.sha256(b"changed").hexdigest()

    with pytest.raises(ValueError, match="PDF 在解析期间发生变化"):
        _coordinator(port).prepare("Book.pdf")

    assert "begin" in port.calls
    assert "stage" not in port.calls


def test_auto_language_failure_emits_recovery_event_before_error() -> None:
    port = _FakePort()
    port.detected = ""

    with pytest.raises(RuntimeError, match="自动识别源语言失败"):
        _coordinator(port, source_lang="auto").prepare("book.txt")

    assert ("event", "language_detection_failed") in port.calls
    assert "stage" not in port.calls
