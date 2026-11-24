"""Glossary extraction overlaps the next batch's translation without changing its results.

Each extraction's model call runs while the next batch translates; its terms are stored on
the run's thread before the following extraction is prepared. Interrupted or failed jobs
leave no checkpoint, so a resumed run extracts them again.
"""

from __future__ import annotations

import json
import re
import threading

import pytest
from wenyi_core.config import Config
from wenyi_core.llm.limits import RequestStopped
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.run_store import STATUS_DONE

from tests.fake_llm import routing_handler

_NUMBERED = re.compile(r"^\[(\d+)\] (.*)$", re.MULTILINE)
WAIT = 5


def _batch(user: str, marker: str) -> list[str]:
    block = user.split(marker, 1)[1].split("\n\n", 1)[0]
    return [text for _, text in _NUMBERED.findall(block)]


def _translated(user: str) -> list[str]:
    return _batch(user, "paragraphs to translate]")


def _is_extraction(system: str) -> bool:
    return "terminology" in system and "extractor" in system


def _is_translation(system: str, user: str) -> bool:
    return "literary translator" in system and "previous JSON response" not in user


class Book:
    """A fake model: paragraph ``Pn`` names ``Namen``; extraction learns names it sees."""

    def __init__(self, tmp_path, text: str, **pipeline):
        self.config = Config.from_dict(
            {
                "language": {"source": "en", "target": "zh"},
                "llm": {"preset": "fake"},
                "paths": {"state_dir": str(tmp_path / "state")},
                "segment": {"max_tokens_per_batch": 1, "max_tokens_per_segment": 0},
                "pipeline": {
                    "review": False,
                    "polish": False,
                    "book_understanding": False,
                    "annotation_alignment": False,
                    "align_retry_limit": 1,
                    **pipeline,
                },
            }
        )
        self.source = tmp_path / "book.txt"
        self.source.write_text(text, encoding="utf-8")
        self.on_extract = lambda user: None
        self.on_translate = lambda sources: None

    def handler(self, messages, tier, json_mode):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if _is_extraction(system):
            self.on_extract(user)
            names = sorted(set(re.findall(r"Name\d+", user.split("Glossary", 1)[-1])))
            terms = [
                {"source": name, "target": f"名{name[4:]}", "type": "person", "scope": "stable"}
                for name in names
            ]
            return json.dumps({"terms": terms}, ensure_ascii=False)
        if _is_translation(system, user):
            sources = _translated(user)
            self.on_translate(sources)
            targets = [re.sub(r"Name(\d+)", r"名\1", text) for text in sources]
            return json.dumps({"translations": targets}, ensure_ascii=False)
        return routing_handler(messages, tier, json_mode)

    def orchestrator(self) -> tuple[Orchestrator, FakeClient]:
        client = FakeClient(handler=self.handler)
        return Orchestrator(self.config, client=client), client


# Every name recurs, so extraction keeps it (single-occurrence terms are dropped).
BOOK = "\n\n".join(["Name1 came.", "Name2 met Name1.", "Name3 met Name2.", "Name3 left."])


def _prompt_glossaries(client) -> list[list[str]]:
    blocks = []
    for call in client.calls:
        if call["operation"] != "translation.body":
            continue
        system = call["messages"][0]["content"]
        block = system.split("<glossary_data>\n", 1)[1].split("\n</glossary_data>", 1)[0]
        blocks.append(re.findall(r"^- (\S+) ", block, re.MULTILINE))
    return blocks


def _extraction_keys(store) -> set[str]:
    store._batch_glossary_event_cache = None
    return store.completed_batch_glossary_keys(0)


def test_extraction_runs_while_the_next_batch_translates(tmp_path):
    book = Book(tmp_path, BOOK)
    second_started = threading.Event()
    overlapped: list[bool] = []

    def on_extract(user: str) -> None:
        if not overlapped and "Name1 came." in user:
            # Serial extraction would wait here until the timeout, with nothing translating.
            overlapped.append(second_started.wait(WAIT))

    def on_translate(sources: list[str]) -> None:
        if sources == ["Name2 met Name1."]:
            second_started.set()

    book.on_extract, book.on_translate = on_extract, on_translate
    orchestrator, _ = book.orchestrator()
    store = orchestrator.run(str(book.source))
    assert overlapped == [True]
    assert store.load_manifest()["chapters"][0]["status"] == STATUS_DONE


def test_the_next_batch_lacks_only_the_previous_batchs_new_entries(tmp_path):
    book = Book(tmp_path, BOOK)
    extraction_glossaries: list[list[str]] = []

    def on_extract(user: str) -> None:
        # Each extraction is prepared after the previous one is stored, as in serial order.
        extraction_glossaries.append(sorted(set(re.findall(r"^- (Name\d+) ", user, re.M))))

    book.on_extract = on_extract
    orchestrator, client = book.orchestrator()
    store = orchestrator.run(str(book.source))
    # Batch n's prompt has the entries extracted from batches before n - 1.
    assert _prompt_glossaries(client) == [[], [], ["Name1"], ["Name1", "Name2"]]
    assert extraction_glossaries[:4] == [
        [],
        ["Name1"],
        ["Name1", "Name2"],
        ["Name1", "Name2", "Name3"],
    ]
    assert {term.source for term in store.all_terms()} == {"Name1", "Name2", "Name3"}
    assert _extraction_keys(store) == {"0:1", "1:1", "2:1", "3:1"}


def test_interrupted_run_leaves_the_pending_extraction_to_resume(tmp_path):
    book = Book(tmp_path, BOOK)

    def on_translate(sources: list[str]) -> None:
        if sources == ["Name3 met Name2."]:
            raise KeyboardInterrupt("while the second batch's extraction is pending")

    book.on_translate = on_translate
    orchestrator, _ = book.orchestrator()
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run(str(book.source))
    store = orchestrator._preparation.prepare(str(book.source))
    targets = [segment.target for segment in store.load_chapter(0).text_segments]
    assert targets == ["名1 came.", "名2 met 名1.", None, None]
    # The discarded job stored nothing and wrote no checkpoint.
    assert _extraction_keys(store) == {"0:1"}
    assert {term.source for term in store.all_terms()} == {"Name1"}

    book.on_translate = lambda sources: None
    resumed, client = book.orchestrator()
    store = resumed.run(str(book.source))
    translated = [
        _translated(call["messages"][-1]["content"])
        for call in client.calls
        if call["operation"] == "translation.body"
    ]
    assert translated == [["Name3 met Name2."], ["Name3 left."]]
    assert {term.source for term in store.all_terms()} == {"Name1", "Name2", "Name3"}
    assert _extraction_keys(store) == {"0:1", "1:1", "2:1", "3:1"}


def test_a_stopped_extraction_stops_the_run_and_is_retried_on_resume(tmp_path):
    # Ordinary model failures fall back to no terms; a budget stop or cancellation does not.
    book = Book(tmp_path, BOOK)

    def on_extract(user: str) -> None:
        if "Name2 met Name1." in user:
            raise RequestStopped("budget exhausted")

    book.on_extract = on_extract
    orchestrator, _ = book.orchestrator()
    with pytest.raises(RequestStopped, match="budget exhausted"):
        orchestrator.run(str(book.source))
    store = orchestrator._preparation.prepare(str(book.source))
    assert store.load_manifest()["chapters"][0]["status"] != STATUS_DONE
    # The stop surfaces once the next batch is translated and saved.
    targets = [segment.target for segment in store.load_chapter(0).text_segments]
    assert targets == ["名1 came.", "名2 met 名1.", "名3 met 名2.", None]
    assert _extraction_keys(store) == {"0:1"}

    book.on_extract = lambda user: None
    resumed, _ = book.orchestrator()
    store = resumed.run(str(book.source))
    assert store.load_manifest()["chapters"][0]["status"] == STATUS_DONE
    assert _extraction_keys(store) == {"0:1", "1:1", "2:1", "3:1"}


@pytest.mark.parametrize("polish_concurrency", [1, 2])
def test_a_chapter_is_done_only_after_its_chapter_extraction_is_stored(
    tmp_path, polish_concurrency
):
    text = "# One\n\nName1 came.\n\nName1 left.\n\n# Two\n\nName2 came.\n\nName2 left.\n"
    book = Book(tmp_path, text, polish=True, polish_concurrency=polish_concurrency)
    chapter_extractions: list[int] = []

    def on_extract(user: str) -> None:
        if "Name1 came.\nName1 left." in user:
            chapter_extractions.append(1)

    def on_translate(sources: list[str]) -> None:
        # Chapter two's heading is its first batch.
        if sources == ["Two"]:
            raise KeyboardInterrupt("while chapter one's chapter extraction is pending")

    book.on_extract, book.on_translate = on_extract, on_translate
    orchestrator, _ = book.orchestrator()
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run(str(book.source))
    store = orchestrator._preparation.prepare(str(book.source))
    first = store.load_manifest()["chapters"][0]
    assert first["status"] != STATUS_DONE
    assert all(segment.target for segment in store.load_chapter(first["index"]).text_segments)

    book.on_translate = lambda sources: None
    resumed, client = book.orchestrator()
    store = resumed.run(str(book.source))
    assert all(row["status"] == STATUS_DONE for row in store.load_manifest()["chapters"])
    # Chapter one is not translated again; only its chapter extraction is repeated.
    assert len(chapter_extractions) == 2
    translated = [
        _translated(call["messages"][-1]["content"])
        for call in client.calls
        if call["operation"] == "translation.body"
    ]
    assert translated == [["Two"], ["Name2 came."], ["Name2 left."]]


def test_a_checkpointed_batch_between_pending_ones_does_not_widen_the_lag(tmp_path):
    book = Book(tmp_path, BOOK)
    orchestrator, client = book.orchestrator()
    store = orchestrator.prepare(str(book.source))
    chapter = store.load_chapter(0)
    chapter.text_segments[1].target = "名2 met 名1."
    store.save_chapter(chapter)
    store.log_event(
        "batch_glossary_extracted", chapter=0, start_index=1, count=1, summary={"inserted": 0}
    )
    store._batch_glossary_event_cache = None
    orchestrator.run(str(book.source))
    # Batch 2 comes two batches after batch 0, so it has batch 0's entries.
    assert _prompt_glossaries(client) == [[], ["Name1"], ["Name1"]]
