"""Lossless and crash-safe EPUB note-slot migration regressions."""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.fixtures.books import write_sample_epub
from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.config import Config
from trans_novel.epub.slots import (
    EpubSegmentState,
    EpubTextSlot,
    normalized_source_text,
    normalized_target_text,
    slot_contract_digest,
)
from trans_novel.ingest import Chapter, Document, Segment, chapter_source_digest
from trans_novel.ingest.epub.note_migration import reconcile_note_slots
from trans_novel.ingest.epub.reader import read_epub
from trans_novel.llm import FakeClient
from trans_novel.pipeline import Application
from trans_novel.pipeline.composition import AgentBundle, RunContext
from trans_novel.pipeline.composition.note_compatibility import (
    ensure_epub_note_compatibility,
)
from trans_novel.pipeline.contracts import GOAL_RUN_ALL, GOAL_TRANSLATE, ExecutionGoal
from trans_novel.pipeline.planning.note_fingerprints import _canonical_note_fingerprints
from trans_novel.pipeline.state import (
    IdentityMismatchError,
    RunIdentity,
    RunStore,
    source_bytes_hash,
)
from trans_novel.pipeline.state.models import TRANSLATION_POLICY_VERSION
from trans_novel.pipeline.state.note_migration import commit_note_migration


def _state(slots: list[EpubTextSlot]) -> EpubSegmentState:
    return EpubSegmentState(
        resource_href="text/chapter.xhtml",
        resource_sha256="a" * 64,
        block_path=(1,),
        block_fingerprint="b" * 64,
        parse_mode="xml",
        slots=slots,
        slot_contract_sha256=slot_contract_digest(slots),
    )


def _documents(marker_target: str) -> tuple[Document, list[Chapter]]:
    old_slots = [
        EpubTextSlot(
            id="tn0_0:s1",
            element_path=(0,),
            field="text",
            source_value="†",
            target_value=marker_target,
        ),
        EpubTextSlot(
            id="tn0_0:s2",
            element_path=(0,),
            field="tail",
            source_value=" body",
            target_value="正文",
        ),
    ]
    new_slots = [EpubTextSlot(id="tn0_0:s1", element_path=(0,), field="tail", source_value=" body")]
    old = Chapter(
        index=7,
        href="text/chapter.xhtml",
        segments=[
            Segment(
                index=11,
                source="† body",
                target=f"{marker_target}正文",
                resource_href="text/chapter.xhtml",
                anchor="tn0_0",
                epub_state=_state(old_slots),
            )
        ],
    )
    fresh = Chapter(
        index=0,
        href="text/chapter.xhtml",
        segments=[
            Segment(
                index=0,
                source="body",
                resource_href="text/chapter.xhtml",
                anchor="tn0_0",
                epub_state=_state(new_slots),
            )
        ],
    )
    doc = Document(
        title="Book",
        source_lang="en",
        target_lang="zh",
        fmt="epub",
        chapters=[fresh],
        meta={
            "epub_schema": 4,
            "epub_sha256": "c" * 64,
            "epub_notes": {
                "version": 1,
                "markers": [
                    {
                        "resource_href": "text/chapter.xhtml",
                        "path": [1, 0],
                        "kind": "noteref",
                        "label": "†",
                        "target_resource": "text/chapter.xhtml",
                        "target_path": [2],
                    }
                ],
                "targets": [],
            },
        },
    )
    return doc, [old]


def _write_note_epub(path: Path) -> None:
    write_sample_epub(str(path))
    with zipfile.ZipFile(path) as archive:
        members = [(info, archive.read(info)) for info in archive.infolist()]
    with zipfile.ZipFile(path, "w") as archive:
        for info, data in members:
            if info.filename == "OEBPS/ch1.xhtml":
                data = (
                    b'<html xmlns="http://www.w3.org/1999/xhtml"><head/><body>'
                    b'<p>Lead <a id="ref"/><sup><a href="#note"><span>'
                    b"\xe2\x80\xa0</span></a></sup> tail</p>"
                    b'<p id="note"><a href="#ref">\xe2\x80\xa0</a> Note tail</p>'
                    b"</body></html>"
                )
            archive.writestr(info, data)


def _legacy_slot_document(path: Path) -> tuple[Document, Document]:
    current = read_epub(str(path), "en", "zh")
    legacy = current.model_copy(deep=True)
    legacy.meta.pop("epub_note_slots_version")
    forward = current.meta["epub_notes"]["markers"][0]
    segment = legacy.chapters[0].segments[0]
    state = segment.epub_state
    relative_anchor = tuple(forward["path"][len(state.block_path) :])
    marker = EpubTextSlot(
        id="legacy:s2",
        element_path=(*relative_anchor, 0),
        field="text",
        source_value=forward["label"],
    )
    slots = [
        state.slots[0].model_copy(update={"id": "legacy:s1"}),
        marker,
        *[
            slot.model_copy(update={"id": f"legacy:s{index}"})
            for index, slot in enumerate(state.slots[1:], 3)
        ],
    ]
    segment.epub_state = state.model_copy(
        update={"slots": slots, "slot_contract_sha256": slot_contract_digest(slots)}
    )
    segment.source = "Lead † tail"
    return legacy, current


class TestPureNoteMigration(unittest.TestCase):
    def test_corrupted_marker_payload_moves_into_following_body_slot(self):
        doc, persisted = _documents("我")
        migrated = reconcile_note_slots(doc, persisted)
        segment = migrated[0].segments[0]
        self.assertEqual((migrated[0].index, segment.index), (7, 11))
        self.assertEqual(segment.source, "body")
        self.assertEqual(segment.target, "我正文")
        self.assertEqual(segment.epub_state.slots[0].target_value, "我正文")

    def test_marker_payload_skips_whitespace_only_surviving_slot(self):
        doc, persisted = _documents("我")
        whitespace = EpubTextSlot(
            id="old:s2",
            element_path=(1,),
            field="text",
            source_value=" ",
            target_value=" ",
        )
        body = EpubTextSlot(
            id="old:s3",
            element_path=(2,),
            field="text",
            source_value="body",
            target_value="正文",
        )
        old_segment = persisted[0].segments[0]
        old_slots = [old_segment.epub_state.slots[0], whitespace, body]
        old_segment.epub_state = _state(old_slots)
        old_segment.source = normalized_source_text(old_slots)
        old_segment.target = normalized_target_text(old_slots)

        fresh_slots = [
            whitespace.model_copy(update={"id": "new:s1", "target_value": None}),
            body.model_copy(update={"id": "new:s2", "target_value": None}),
        ]
        fresh_segment = doc.chapters[0].segments[0]
        fresh_segment.epub_state = _state(fresh_slots)
        fresh_segment.source = normalized_source_text(fresh_slots)

        slots = reconcile_note_slots(doc, persisted)[0].segments[0].epub_state.slots
        self.assertEqual([slot.target_value for slot in slots], [" ", "我正文"])

    def test_exact_original_marker_is_removed_without_duplication(self):
        doc, persisted = _documents("†")
        segment = reconcile_note_slots(doc, persisted)[0].segments[0]
        self.assertEqual(segment.target, "正文")
        self.assertEqual(segment.epub_state.slots[0].target_value, "正文")

    def test_non_marker_slot_difference_is_rejected(self):
        doc, persisted = _documents("我")
        persisted[0].segments[0].epub_state.slots.append(
            EpubTextSlot(
                id="tn0_0:s3",
                element_path=(2,),
                field="text",
                source_value="unrelated",
                target_value="无关",
            )
        )
        persisted[0].segments[0].epub_state.slot_contract_sha256 = slot_contract_digest(
            persisted[0].segments[0].epub_state.slots
        )
        persisted[0].segments[0].source = "† bodyunrelated"
        persisted[0].segments[0].target = "我正文无关"
        with self.assertRaisesRegex(ValueError, "^EPUB note migration rejected:"):
            reconcile_note_slots(doc, persisted)

    def test_rejected_difference_leaves_persisted_files_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "book.epub")
            with open(source, "wb") as stream:
                stream.write(b"source")
            doc, persisted = _documents("我")
            doc.source_path = source
            old = persisted[0].segments[0]
            old.epub_state.slots = [old.epub_state.slots[0]]
            old.epub_state.slot_contract_sha256 = slot_contract_digest(old.epub_state.slots)
            old.source = "†"
            old.target = "我"
            doc.chapters[0].segments = []
            store = RunStore(os.path.join(root, "run"))
            manifest = store.stage_document(
                Document(
                    title="Book",
                    source_lang="en",
                    target_lang="zh",
                    fmt="epub",
                    source_path=source,
                    chapters=persisted,
                    meta={"epub_schema": 4, "epub_sha256": "c" * 64},
                ),
                RunIdentity(
                    source_bytes_sha256=source_bytes_hash(source),
                    source_lang="en",
                    target_lang="zh",
                ),
            )
            store.save_manifest(manifest)
            with open(store.manifest_path, "rb") as stream:
                before_manifest = stream.read()
            with open(store.chapter_path(7), "rb") as stream:
                before_chapter = stream.read()
            with self.assertRaisesRegex(
                ValueError, "^EPUB note migration rejected: marker-only segment"
            ):
                ensure_epub_note_compatibility(
                    store,
                    doc,
                    identity_path=source,
                    source_lang="en",
                    target_lang="zh",
                    allow_migration=True,
                    fingerprint_updates=lambda *_args: self.fail(
                        "rejected migration must not compute content fingerprints"
                    ),
                )
            with open(store.manifest_path, "rb") as stream:
                self.assertEqual(stream.read(), before_manifest)
            with open(store.chapter_path(7), "rb") as stream:
                self.assertEqual(stream.read(), before_chapter)
            self.assertFalse(os.path.exists(store.path_for("note_migration.json")))


class TestApplicationNoteMigration(unittest.TestCase):
    def test_current_policy_run_all_migrates_once_and_replays_without_client_calls(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "book.epub"
            _write_note_epub(source)
            config = Config.from_dict({"llm": fake_llm_dict(), "quality": "economy"})
            config.target_lang = "zh"
            config.state_dir = str(Path(root) / "state")
            first = FakeClient(handler=routing_handler)
            result = Application(config, client=first).run_all(
                str(source), out_path=str(Path(root) / "initial.epub")
            )
            store = result["store"]
            self.assertEqual(config.source_lang, "auto")
            saved_source_lang = store.load_state().identity.source_lang
            self.assertNotIn(saved_source_lang, {"", "auto"})
            current = read_epub(str(source), saved_source_lang, "zh")
            before = [store.load_chapter(item.index) for item in store.load_state().chapters]
            legacy = [chapter.model_copy(deep=True) for chapter in before]
            relation = current.meta["epub_notes"]["markers"][0]
            segment = next(
                item
                for item in legacy[0].segments
                if item.resource_href == relation["resource_href"]
                and tuple(relation["path"][: len(item.epub_state.block_path)])
                == item.epub_state.block_path
            )
            state = segment.epub_state
            relative_anchor = tuple(relation["path"][len(state.block_path) :])
            old_slots = [
                state.slots[0].model_copy(update={"id": "legacy:s1"}),
                EpubTextSlot(
                    id="legacy:s2",
                    element_path=(*relative_anchor, 0),
                    field="text",
                    source_value=relation["label"],
                    target_value="我",
                ),
                *[
                    slot.model_copy(update={"id": f"legacy:s{index}"})
                    for index, slot in enumerate(state.slots[1:], 3)
                ],
            ]
            segment.epub_state = state.model_copy(
                update={
                    "slots": old_slots,
                    "slot_contract_sha256": slot_contract_digest(old_slots),
                }
            )
            segment.source = normalized_source_text(old_slots)
            segment.target = normalized_target_text(old_slots)
            if legacy[0].processing is not None:
                legacy[0].processing = legacy[0].processing.model_copy(
                    update={"source_sha256": chapter_source_digest(legacy[0])}
                )
            context = RunContext(
                store=store,
                config=config,
                doc=current,
                agent_builder=lambda src, tgt: AgentBundle(first, config, src=src, tgt=tgt),
                output=config.output.model_copy(deep=True),
            )
            try:
                legacy_fingerprints = _canonical_note_fingerprints(config, store, context, legacy)
                current_fingerprints = _canonical_note_fingerprints(config, store, context, before)
            finally:
                context.close()
            manifest = store.load_state()
            for key, old_fingerprint in legacy_fingerprints.items():
                new_fingerprint = current_fingerprints.get(key)
                if old_fingerprint == new_fingerprint:
                    continue
                self.assertEqual(manifest.nodes[key].input_fingerprint, new_fingerprint)
                manifest.nodes[key].input_fingerprint = old_fingerprint
            manifest.meta.pop("epub_note_slots_version")
            manifest.chapters[0].processing = legacy[0].processing
            for chapter in legacy:
                store.save_chapter(chapter)
            store.save_state(manifest)
            paid = [segment.target for segment in legacy[0].segments]

            for attempt in range(2):
                offline = FakeClient(
                    handler=lambda *_args: self.fail(
                        "note migration and compatible replay must stay offline"
                    )
                )
                replay = Application(config, client=offline).run_all(
                    str(source), out_path=str(Path(root) / f"replay-{attempt}.epub")
                )
                store = replay["store"]
                self.assertEqual(offline.calls, [])
                self.assertEqual(
                    [segment.target for segment in store.load_chapter(legacy[0].index).segments],
                    paid,
                )
            self.assertEqual(store.load_manifest()["meta"]["epub_note_slots_version"], 1)
            self.assertIn(
                "我",
                "".join(
                    slot.target_value or ""
                    for segment in store.load_chapter(legacy[0].index).segments
                    for slot in segment.epub_state.slots
                ),
            )

    def test_invalid_entry_conditions_do_not_write_or_call_clients(self):
        cases = (
            "wrong_source",
            "future_policy",
            "incomplete_legacy",
            "incomplete_current_output",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as root:
                source = Path(root) / "book.epub"
                _write_note_epub(source)
                legacy, current = _legacy_slot_document(source)
                config = Config.from_dict({"llm": fake_llm_dict(), "quality": "economy"})
                config.source_lang = "en"
                config.target_lang = "zh"
                config.state_dir = str(Path(root) / "state")
                _, store = Application(
                    config, client=FakeClient(handler=routing_handler)
                ).run_document_goal(legacy, str(source), GOAL_TRANSLATE)
                state = store.load_state()
                identity_path = str(source)
                goal = GOAL_TRANSLATE
                expected_error: type[Exception] = IdentityMismatchError
                expected_pattern = "源文件内容与运行状态不一致"
                direct_output_preflight = False
                if case == "wrong_source":
                    wrong = Path(root) / "wrong.epub"
                    wrong.write_bytes(b"wrong")
                    identity_path = str(wrong)
                elif case == "future_policy":
                    state.identity.translation_policy_version = TRANSLATION_POLICY_VERSION + 1
                    store.save_state(state)
                    expected_error = ValueError
                    expected_pattern = "EPUB note migration rejected: future translation policy"
                elif case == "incomplete_current_output":
                    state.progress[state.chapters[0].index].status = "pending"
                    store.save_state(state)
                    expected_error = ValueError
                    expected_pattern = "EPUB note migration rejected: run is not complete"
                    direct_output_preflight = True
                else:
                    state.identity.translation_policy_version = TRANSLATION_POLICY_VERSION - 1
                    state.progress[state.chapters[0].index].status = "pending"
                    store.save_state(state)
                    goal = ExecutionGoal(
                        name="run_all", phases=GOAL_RUN_ALL.phases, out_format="txt"
                    )
                    expected_error = ValueError
                    expected_pattern = "EPUB note migration rejected: run is not complete"
                before_manifest = Path(store.manifest_path).read_bytes()
                before_chapters = {
                    item.index: Path(store.chapter_path(item.index)).read_bytes()
                    for item in state.chapters
                }
                offline = FakeClient(
                    handler=lambda *_args: self.fail(
                        "invalid note migration must fail before client calls"
                    )
                )
                with self.assertRaisesRegex(expected_error, expected_pattern):
                    if direct_output_preflight:
                        ensure_epub_note_compatibility(
                            store,
                            current.model_copy(deep=True),
                            identity_path=identity_path,
                            source_lang="en",
                            target_lang="zh",
                            allow_migration=True,
                            require_completed_content=True,
                            fingerprint_updates=lambda *_args: self.fail(
                                "incomplete output migration must not compute fingerprints"
                            ),
                        )
                    else:
                        Application(config, client=offline).run_document_goal(
                            current.model_copy(deep=True), identity_path, goal
                        )
                self.assertEqual(offline.calls, [])
                self.assertEqual(Path(store.manifest_path).read_bytes(), before_manifest)
                self.assertEqual(
                    {
                        item.index: Path(store.chapter_path(item.index)).read_bytes()
                        for item in state.chapters
                    },
                    before_chapters,
                )


class TestNoteMigrationTransaction(unittest.TestCase):
    def test_interrupted_commit_recovers_forward_without_translation_journal(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "book.epub")
            with open(source, "wb") as stream:
                stream.write(b"source")
            doc, persisted = _documents("我")
            doc.source_path = source
            store = RunStore(os.path.join(root, "run"))
            manifest = store.stage_document(
                Document(
                    title="Book",
                    source_lang="en",
                    target_lang="zh",
                    fmt="epub",
                    source_path=source,
                    chapters=persisted,
                    meta={"epub_schema": 4, "epub_sha256": "c" * 64},
                ),
                RunIdentity(
                    source_bytes_sha256=source_bytes_hash(source),
                    source_lang="en",
                    target_lang="zh",
                ),
            )
            store.save_manifest(manifest)
            migrated = reconcile_note_slots(doc, persisted)
            original_write = store.write_json

            def interrupt_manifest(path, data):
                if path == store.manifest_path and os.path.isfile(
                    store.path_for("note_migration.json")
                ):
                    raise RuntimeError("crash")
                original_write(path, data)

            store.write_json = interrupt_manifest
            with self.assertRaisesRegex(RuntimeError, "crash"), store.lock():
                commit_note_migration(
                    store,
                    chapters=migrated,
                    meta={
                        **manifest["meta"],
                        "epub_notes": doc.meta["epub_notes"],
                        "epub_note_slots_version": 1,
                    },
                )
            recovered = RunStore(store.run_dir)
            self.assertEqual(recovered.load_chapter(7).segments[0].target, "我正文")
            self.assertEqual(recovered.load_manifest()["meta"]["epub_note_slots_version"], 1)
            self.assertFalse(os.path.exists(recovered.path_for("note_migration.json")))
            self.assertFalse(os.path.exists(recovered.journal_path))


if __name__ == "__main__":
    unittest.main()
