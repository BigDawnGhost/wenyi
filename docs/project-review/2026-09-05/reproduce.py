"""Offline probes for the 2026-09-05 review; all generated data stays in /tmp.

These assert the reviewed revision's symptoms, not the desired fixed behavior.
Run from an installed Wenyi checkout with: uv run --no-sync python PATH_TO_THIS_FILE
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import Mock, patch

from typer.testing import CliRunner

from trans_novel.assemble.html_resources import _load_html_resource
from trans_novel.cli import app
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryTerm
from trans_novel.ingest.models import Chapter, Segment
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.runstore import RunStore, source_sha256
from trans_novel.review.run_store import ReviewOutcome, ReviewRunStore
from trans_novel.srt.store import SrtRunStore
from trans_novel.srt.translate import _merge_batch_result, translate_srt


def config(root: Path) -> Config:
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {
                "provider": "fake",
                "tiers": {
                    "strong": {"model": "audit-a"},
                    "cheap": {"model": "audit-a"},
                },
            },
            "pipeline": {
                "review_autofix": False,
                "review_fix_loop": False,
                "review_agent_loop": False,
                "prescan_concurrency": 1,
            },
            "paths": {"state_dir": str(root / "state")},
            "output": {"about_page": False},
        }
    )


def book(root: Path, count: int = 1) -> tuple[Path, RunStore]:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "book.txt"
    source.write_text("Original source.\n", encoding="utf-8")
    store = RunStore(str(root / "state" / "book"))
    for i in range(count):
        store.save_chapter(
            Chapter(index=i, segments=[Segment(index=0, source=f"Source {i}", target="旧译")])
        )
    store.save_manifest(
        {
            "title": "book",
            "fmt": "text",
            "source_sha256": source_sha256(str(source)),
            "source_lang": "en",
            "target_lang": "zh",
            "chapters": [{"index": i, "title": str(i), "status": "done"} for i in range(count)],
        }
    )
    return source, store


def autofix_disabled(root: Path) -> None:
    _, store = book(root)
    debug = ReviewRunStore(store.run_dir)
    debug.start(reviewed_content_digest="audit", metadata={})
    result = debug.finish(
        status="completed",
        termination="clean_confirmed",
        summary={"issue_count": 0, "change_count": 1},
        issues=[],
        changes=[{"chapter": 0, "index": 0, "suggested_target": "新译"}],
    )
    cfg = config(root)
    cfg.pipeline.review_autofix = True
    original = Orchestrator(cfg, client=FakeClient())
    with patch.object(original._review_autofix, "_apply_index", side_effect=RuntimeError("stop")):
        try:
            original._review_autofix.run(store, ReviewOutcome(debug.run_dir, result, {}), [])
        except RuntimeError:
            pass
    assert store.load_chapter(0).text_segments[0].target == "旧译"
    cfg.pipeline.review_autofix = False
    resumed = Orchestrator(cfg, client=FakeClient())
    with store.lock():
        resumed._run_review_locked(store, [], progress=None)
    assert store.load_chapter(0).text_segments[0].target == "新译"
    print("F01: review_autofix=false; pending publication still changed the formal target")


def overwrite_source(root: Path) -> None:
    source, store = book(root)
    before = source.read_bytes()
    orch = Orchestrator(config(root), client=FakeClient())
    error = "none"
    try:
        orch._assembly.assemble_snapshot(
            store,
            input_path=str(source),
            out_format="txt",
            out_path=str(source),
            pdf_engine="weasyprint",
            progress=None,
        )
    except ValueError:
        error = "ValueError"
    assert before != source.read_bytes()
    assert error == "ValueError"
    print("F02: source overwritten before post-export identity check raised ValueError")


def srt_order(root: Path) -> None:
    del root
    items = [(str(i), f"Source {i}") for i in range(1, 21)]
    jobs = [(0, {key: "A" for key, _ in items}), (10, {key: "B" for key, _ in items[10:]})]

    def merge(order):
        output = {}
        for start, response in order:
            _merge_batch_result(
                output,
                items,
                response,
                start_pos=start,
                is_first=start == 0,
                is_last=True,
            )
        return output

    forward, reverse = merge(jobs), merge(reversed(jobs))
    assert forward["16"] == "B" and reverse["16"] == "A"
    print("F03: cue 16 is B for completion order [0,10], A for [10,0]")


def srt_failed_done(root: Path) -> None:
    root.mkdir(parents=True)
    source = root / "demo.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello.\n", encoding="utf-8")

    def fail(*args):
        raise RuntimeError("offline simulated service failure")

    first = translate_srt(str(source), config(root), client=FakeClient(handler=fail))
    store = SrtRunStore(first["run_dir"])
    assert store.load_cues()["1"]["status"] == "done"
    assert store.load_cues()["1"]["target"] == "Hello."
    fresh = FakeClient(handler=lambda *_: '{"1":"你好。"}')
    translate_srt(str(source), config(root), client=fresh)
    assert len(fresh.calls) == 0
    empty_root = root / "empty"
    empty = translate_srt(
        str(source),
        config(empty_root),
        client=FakeClient(handler=lambda *_: '{"1":""}'),
    )
    assert SrtRunStore(empty["run_dir"]).load_cues()["1"]["status"] == "done"
    print(
        "F04: failed cue became done/source and was skipped on resume; empty target also became done"
    )


def review_cache(root: Path) -> None:
    _, store = book(root)
    cfg = config(root)
    clean = '{"issues":[],"reviewed_segments":1,"complete":true}'
    original = Orchestrator(cfg, client=FakeClient(handler=lambda *_: clean))
    term = GlossaryTerm(source="Source", target="译名", gender="male")
    first = original._review.run_session(store, [term])
    assert original.client.calls[0]["tier"] == "cheap"
    assert "male" in original.client.calls[0]["messages"][-1]["content"]
    cfg2 = config(root)
    cfg2.llm.tiers["cheap"].model = "audit-b"
    client = FakeClient(handler=lambda *_: clean)
    changed = Orchestrator(cfg2, client=client)
    term2 = GlossaryTerm(source="Source", target="译名", gender="female", aliases=["Alias"])
    assert original._review._review_config_snapshot() == changed._review._review_config_snapshot()
    assert original._review._review_glossary_fingerprint([term]) == (
        changed._review._review_glossary_fingerprint([term2])
    )
    second = changed._review.run_session(store, [term])
    assert first.run_dir == second.run_dir and len(client.calls) == 0
    evidence_client = FakeClient(handler=lambda *_: clean)
    evidence_changed = Orchestrator(config(root), client=evidence_client)
    third = evidence_changed._review.run_session(store, [term2])
    assert first.run_dir == third.run_dir and len(evidence_client.calls) == 0
    print("F05: changing model, gender and aliases reused completed Review with zero new calls")


def stale_synopsis(root: Path) -> None:
    _, store = book(root, count=2)
    orch = Orchestrator(config(root), client=FakeClient())
    synopsis = Mock(side_effect=lambda digests, _: "|".join(digests))
    orch._runtime.synopsizer.book_synopsis = synopsis
    orch._runtime.synopsizer.digest_chapter = Mock(side_effect=["chapter 0", "", "chapter 1"])
    first = orch._preparation.ensure_understanding(store)
    second = orch._preparation.ensure_understanding(store)
    assert store.load_chapter(1).meta["source_digest"] == "chapter 1"
    assert first == second == "chapter 0|" and synopsis.call_count == 1
    print("F06: missing chapter digest recovered; book synopsis still contains only chapter 0")


def resource_symlink(root: Path) -> None:
    directory = root / "html"
    directory.mkdir(parents=True)
    outside = root / "outside.txt"
    outside.write_text("benign audit fixture", encoding="utf-8")
    (directory / "linked.txt").symlink_to(outside)
    loaded = _load_html_resource("linked.txt", source_dir=str(directory))
    assert loaded is not None and loaded[1] == outside.read_bytes()
    print("F07: HTML resource symlink read a fixture outside source_dir")


def cli_errors(root: Path) -> None:
    root.mkdir(parents=True)
    source = root / "book.txt"
    source.write_text("Hello from the audit fixture.\n", encoding="utf-8")
    cfg = root / "config.yaml"
    cfg.write_text(
        f"llm:\n  provider: fake\npaths:\n  state_dir: {root / 'state'}\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["--config", str(cfg), "prepare", str(source)])
    assert isinstance(result.exception, RuntimeError)
    cfg.write_text("language: [\n", encoding="utf-8")
    malformed = CliRunner().invoke(app, ["--config", str(cfg), "status", str(source)])
    assert type(malformed.exception).__name__ == "ParserError"
    print("F08: CLI lets RuntimeError (language detection) and YAML ParserError escape")


def srt_write_race(root: Path) -> None:
    store1, store2 = SrtRunStore(str(root)), SrtRunStore(str(root))
    barrier = Barrier(2)
    real_replace = os.replace

    def synchronized_replace(src, dst):
        barrier.wait(timeout=5)
        return real_replace(src, dst)

    with patch("trans_novel.srt.store.os.replace", side_effect=synchronized_replace):
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(store.save_usage, {"value": i})
                for i, store in enumerate((store1, store2))
            ]
            errors = [future.exception() for future in futures]
    assert sum(isinstance(error, FileNotFoundError) for error in errors) == 1
    print("F09: two SRT state writers sharing .tmp cause one FileNotFoundError")


def main() -> None:
    with TemporaryDirectory(prefix="wenyi-review-", dir="/tmp") as directory:
        for probe in (
            autofix_disabled,
            overwrite_source,
            srt_order,
            srt_failed_done,
            review_cache,
            stale_synopsis,
            resource_symlink,
            cli_errors,
            srt_write_race,
        ):
            probe(Path(directory) / probe.__name__)


if __name__ == "__main__":
    main()
