from __future__ import annotations

import errno
import hashlib
import shutil
import struct
import tempfile
import unittest
import zipfile
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.fixtures.books import write_phase9_epub, write_sample_epub
from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.assemble.epub.publication import (
    EpubOutput,
    EpubPublishError,
    EpubVerificationError,
    publish_epub,
    publish_epubs,
)
from trans_novel.assemble.epub.verification import verify_epub
from trans_novel.config import Config
from trans_novel.llm import FakeClient
from trans_novel.pipeline import Application
from trans_novel.pipeline.state import RunStore


def _config(state_dir: str, output: dict | None = None):
    raw = {"llm": fake_llm_dict()}
    if output is not None:
        raw["output"] = output
    config = Config.from_dict(raw)
    config.source_lang = "ja"
    config.state_dir = state_dir
    return config


def _run(input_path, state_dir, output=None):
    cfg = _config(state_dir, output)
    orch = Application(cfg, client=FakeClient(handler=routing_handler))
    store = orch.run(input_path)
    _stamp_formal_prereqs(store)
    return store, cfg


def _stamp_formal_prereqs(store):
    """Direct writer tests stamp title, QA, Repair, and report prerequisites."""
    from trans_novel.pipeline.state import NODE_DETERMINISTIC_QA, NODE_REPAIR, NodeState

    state = store.load_state()
    for node_id in ("titles", NODE_DETERMINISTIC_QA, NODE_REPAIR, "report"):
        state.nodes.setdefault(node_id, NodeState(node_id=node_id, status="succeeded"))
    store.save_state(state)
    return store


class TestEpubStage2(unittest.TestCase):
    def test_exhausted_polish_protocol_still_publishes_verified_epub(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            write_sample_epub(str(source))
            config = Config.from_dict({"llm": fake_llm_dict(), "quality": "quality"})
            config.source_lang = "ja"
            config.state_dir = str(root / "state")

            def handler(messages, agent, operation, json_mode):
                if operation == "polish.batch":
                    return '{"polished": []}'
                return routing_handler(messages, agent, operation, json_mode)

            result = Application(config, client=FakeClient(handler=handler)).run_all(
                str(source), out_format="epub"
            )

            output = Path(result["output"])
            self.assertTrue(output.is_file())
            report = result["store"].load_epub_verification()
            self.assertIsNotNone(report)
            self.assertTrue(report["passed"])

    def test_failed_verification_preserves_existing_final_and_persists_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = RunStore(str(root / "state"))
            final = root / "book.epub"
            final.write_bytes(b"previous-final")

            def corrupt(path: str) -> None:
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("mimetype", b"not-an-epub", zipfile.ZIP_STORED)

            with self.assertRaises(EpubVerificationError) as raised:
                publish_epub(state, None, final, mode="generated", writer=corrupt)
            self.assertFalse(raised.exception.published)
            self.assertEqual(str(raised.exception), "EPUB verification failed")
            self.assertEqual(final.read_bytes(), b"previous-final")
            report = state.load_epub_verification()
            self.assertIsNotNone(report)
            assert report is not None
            self.assertFalse(report["passed"])
            self.assertFalse(report["published"])
            self.assertEqual(report["output_label"], "book.epub")
            self.assertEqual(raised.exception.report, report)
            self.assertFalse(list(root.glob(".book.epub.epub-verify-*.tmp")))
            self.assertTrue(state.report_path != state.epub_verification_path)

    def test_writer_failure_report_includes_bounded_cause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RunStore(str(root / "state"))
            cause = ValueError("x" * 600)

            def writer(path: str) -> None:
                raise cause

            with self.assertRaises(EpubVerificationError) as raised:
                publish_epub(
                    store,
                    None,
                    root / "published.epub",
                    mode="generated",
                    writer=writer,
                )

            expected_detail = f"{type(cause).__name__}: {cause}"[:500]
            failure = next(
                item
                for item in raised.exception.report["failures"]
                if item["code"] == "writer_failed"
            )
            self.assertEqual(failure["detail"], expected_detail)
            self.assertEqual(str(raised.exception), f"EPUB verification failed: {cause}")

    def test_success_publishes_report_and_stable_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            write_phase9_epub(str(source))
            state = RunStore(str(root / "state"))
            final = root / "published.epub"
            publish_epub(
                state,
                None,
                final,
                mode="generated",
                writer=lambda path: shutil.copyfile(source, path),
            )
            report = state.load_epub_verification()
            self.assertIsNotNone(report)
            assert report is not None
            self.assertTrue(report["passed"])
            self.assertTrue(report["published"])
            self.assertEqual(report["mode"], "generated")
            self.assertIsNone(report["source_sha256"])
            self.assertEqual(report["output_label"], "published.epub")
            event = Path(state.event_log_path).read_text(encoding="utf-8").splitlines()[-1]
            self.assertIn('"event": "epub_verification_passed"', event)
            self.assertIn('"output": "published.epub"', event)

    def test_input_output_aliases_are_rejected_before_any_writer(self) -> None:
        from trans_novel.assemble import assemble

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            write_phase9_epub(str(source))
            before = source.read_bytes()
            store = SimpleNamespace(
                load_manifest=lambda: {"fmt": "text", "target_lang": "zh"},
            )
            with (
                patch("trans_novel.pipeline.execution.ensure_assemble_ready"),
                self.assertRaises(ValueError),
            ):
                assemble(store, str(source), str(source), out_format="txt")
            self.assertEqual(source.read_bytes(), before)

            generated_store = RunStore(str(root / "generated-state"))
            called = []
            with self.assertRaises(EpubPublishError) as raised:
                publish_epub(
                    generated_store,
                    None,
                    source,
                    mode="generated",
                    source_identity_path=source,
                    writer=lambda path: called.append(path),
                )
            self.assertEqual(raised.exception.report["failures"][0]["code"], "input_output_alias")
            self.assertEqual(called, [])
            self.assertEqual(source.read_bytes(), before)

    def test_generated_assemble_alias_is_rejected_before_writer(self) -> None:
        from trans_novel.assemble import assemble

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.txt"
            source.write_text("source", encoding="utf-8")

            class Store:
                def load_manifest(self):
                    return {"fmt": "text", "target_lang": "zh"}

            with (
                patch("trans_novel.pipeline.execution.ensure_assemble_ready"),
                self.assertRaisesRegex(ValueError, "paths must differ"),
            ):
                assemble(Store(), str(source), str(source), out_format="epub")

    def test_source_assemble_rejects_unsafe_zip_before_output_write(self) -> None:
        from trans_novel.assemble.epub.rendering import (
            assemble_source_epub as _assemble_source_epub,
        )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "unsafe.epub"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("META-INF/x/../container.xml", b"<container/>")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()

            class Store:
                def load_manifest(self):
                    return {
                        "meta": {
                            "epub_schema": 4,
                            "epub_sha256": digest,
                            "epub_resources": [],
                        },
                        "source_lang": "en",
                        "chapters": [],
                    }

            output = Path(directory) / "output.epub"
            with self.assertRaisesRegex(ValueError, "unsafe_entry"):
                _assemble_source_epub(Store(), str(source), str(output), target_lang="zh")
            self.assertFalse(output.exists())

    def test_identical_bytes_have_relocation_stable_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "one"
            second = Path(directory) / "two"
            first.mkdir()
            second.mkdir()
            one = first / "book.epub"
            two = second / "book.epub"
            write_phase9_epub(str(one))
            shutil.copyfile(one, two)
            report_one = verify_epub(one, mode="generated")
            report_two = verify_epub(two, mode="generated")
            self.assertEqual(report_one, report_two)

    def test_tools_assemble_uses_the_same_publication_gate(self) -> None:
        from typer.testing import CliRunner

        from tests.fixtures.books import write_sample_txt
        from trans_novel.cli import app

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "novel.txt"
            write_sample_txt(str(source))
            _, config = _run(str(source), str(Path(directory) / "state"))
            with patch("trans_novel.cli.common.load_config", return_value=config):
                result = CliRunner().invoke(app, ["tools", "assemble", str(source), "--mono"])
            self.assertEqual(result.exit_code, 0, result.output)
            output = Path(directory) / "novel.zh.epub"
            self.assertTrue(output.is_file())
            verification_files = list((Path(directory) / "state").rglob("epub_verification.json"))
            self.assertEqual(len(verification_files), 1)
            report = __import__("json").loads(verification_files[0].read_text(encoding="utf-8"))
            self.assertTrue(report["passed"])
            event_files = list((Path(directory) / "state").rglob("events.jsonl"))
            self.assertTrue(event_files)
            self.assertIn(
                '"event": "epub_verification_passed"',
                event_files[0].read_text(encoding="utf-8"),
            )

    def test_normal_finish_assemble_node_reaches_the_publication_gate(self) -> None:
        from tests.fixtures.books import write_sample_txt
        from trans_novel.pipeline.contracts import NodeRequest
        from trans_novel.pipeline.nodes import AssembleNode

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "novel.txt"
            write_sample_txt(str(source))
            store, config = _run(str(source), str(Path(directory) / "state"))
            outcome = AssembleNode(output=config.output, out_format="epub").execute(
                NodeRequest(
                    store=store,
                    node_id="assemble",
                    key="assemble",
                    ci=None,
                    scope="book",
                    input_path=str(source),
                    progress=None,
                )
            )
            self.assertEqual(len(outcome.artifacts["outputs"]), 2)
            self.assertTrue(all(Path(path).is_file() for path in outcome.artifacts["outputs"]))
            report = store.load_epub_verification()
            self.assertIsNotNone(report)
            assert report is not None
            self.assertTrue(report["passed"])

    def test_report_persistence_failure_is_the_exception_cause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            write_phase9_epub(str(source))
            store = RunStore(str(root / "state"))
            persistence_error = RuntimeError("persist failed")
            with (
                patch.object(store, "save_epub_verification", side_effect=persistence_error),
                self.assertRaises(EpubPublishError) as raised,
            ):
                publish_epub(
                    store,
                    None,
                    root / "published.epub",
                    mode="generated",
                    writer=lambda path: shutil.copyfile(source, path),
                )
            self.assertIs(raised.exception.__cause__, persistence_error)
            self.assertFalse(raised.exception.published)

    def test_preflight_report_persistence_failure_is_chained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RunStore(str(root / "state"))
            persistence_error = RuntimeError("persist failed")
            with (
                patch.object(store, "save_epub_verification", side_effect=persistence_error),
                self.assertRaises(EpubPublishError) as raised,
            ):
                publish_epub(
                    store,
                    None,
                    root / "missing" / "published.epub",
                    mode="generated",
                    writer=lambda path: None,
                )
            self.assertIs(raised.exception.__cause__, persistence_error)
            self.assertFalse(raised.exception.published)

    def test_replace_failure_persists_failed_event_and_preserves_final(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            write_phase9_epub(str(source))
            final = root / "published.epub"
            final.write_bytes(b"existing")
            state = RunStore(str(root / "state"))
            with (
                patch(
                    "trans_novel.assemble.epub.publication.os.replace", side_effect=OSError("no")
                ),
                self.assertRaisesRegex(Exception, "EPUB publication failed") as raised,
            ):
                publish_epub(
                    state,
                    None,
                    final,
                    mode="generated",
                    writer=lambda path: shutil.copyfile(source, path),
                )
            self.assertEqual(final.read_bytes(), b"existing")
            report = getattr(raised.exception, "report", None)
            assert report is not None
            self.assertFalse(report["published"])

    def test_post_replace_fsync_failure_reports_published_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            write_phase9_epub(str(source))
            final = root / "published.epub"
            state = RunStore(str(root / "state"))
            with (
                patch(
                    "trans_novel.assemble.epub.publication.fsync_file",
                    side_effect=OSError("fsync"),
                ),
                self.assertRaises(Exception) as raised,
            ):
                publish_epub(
                    state,
                    None,
                    final,
                    mode="generated",
                    writer=lambda path: shutil.copyfile(source, path),
                )
            self.assertTrue(raised.exception.published)
            report = state.load_epub_verification()
            assert report is not None
            self.assertTrue(report["published"])
            self.assertFalse(report["passed"])
            self.assertTrue(any(item["code"] == "durability_failed" for item in report["failures"]))

    def test_descriptor_flags_publish_and_secondary_opf_stays_identical(self) -> None:
        from trans_novel.assemble import assemble

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            write_phase9_epub(str(source))
            with zipfile.ZipFile(source) as zin:
                entries = {info.filename: zin.read(info.filename) for info in zin.infolist()}
            backup = (
                b'<package xmlns="http://www.idpf.org/2007/opf"><metadata '
                b'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:language>ja</dc:language>'
                b"</metadata></package>"
            )
            entries["META-INF/backup.opf"] = backup
            with zipfile.ZipFile(source, "w") as zout:
                for name, data in entries.items():
                    zout.writestr(
                        name,
                        data,
                        zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED,
                    )
            raw = bytearray(source.read_bytes())
            with zipfile.ZipFile(source) as zin:
                info = zin.getinfo("OEBPS/text/chapter-1.xhtml")
            descriptor = struct.unpack_from("<H", raw, info.header_offset + 6)[0] | 0x08
            struct.pack_into("<H", raw, info.header_offset + 6, descriptor)
            marker = b"OEBPS/text/chapter-1.xhtml"
            central_offset = raw.find(b"PK\x01\x02", info.header_offset + 30)
            while central_offset >= 0:
                name_length = struct.unpack_from("<H", raw, central_offset + 28)[0]
                if raw[central_offset + 46 : central_offset + 46 + name_length] == marker:
                    break
                central_offset = raw.find(b"PK\x01\x02", central_offset + 4)
            self.assertGreaterEqual(central_offset, 0)
            struct.pack_into("<H", raw, central_offset + 8, descriptor)
            store, _ = _run(str(source), str(root / "state"))
            output = root / "output.epub"
            assemble(store, str(source), str(output), out_format="epub")
            with zipfile.ZipFile(source) as source_zip, zipfile.ZipFile(output) as output_zip:
                self.assertEqual(
                    [info.flag_bits for info in source_zip.infolist()],
                    [info.flag_bits for info in output_zip.infolist()],
                )
                self.assertTrue(output_zip.read("OEBPS/text/chapter-1.xhtml"))
                self.assertEqual(output_zip.read("META-INF/backup.opf"), backup)


class TestEpubBatchPublication(unittest.TestCase):
    def test_file_sync_uses_writable_handle_without_truncating_output(self) -> None:
        from trans_novel.assemble.epub import publication

        real_fsync = publication.os.fsync

        def require_writable_handle(descriptor: int) -> None:
            publication.os.write(descriptor, b"")
            real_fsync(descriptor)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.epub"
            path.write_bytes(b"published archive bytes")
            with patch.object(publication.os, "fsync", side_effect=require_writable_handle):
                publication.fsync_file(str(path))
            self.assertEqual(path.read_bytes(), b"published archive bytes")

    def _requests(self, root: Path, *, note_theme: bool = False):
        from trans_novel.assemble.epub.layout import build_layout_inventory
        from trans_novel.assemble.epub.rendering import assemble_epub
        from trans_novel.assemble.epub.rendering.theme import resolve_theme
        from trans_novel.assemble.epub.rendering.theme.service import ThemeService
        from trans_novel.epub.layout import LayoutAssignment, LayoutProfile

        source = root / "source.epub"
        write_phase9_epub(str(source))
        store, _ = _run(str(source), str(root / "state"))
        if note_theme:
            chapters = [
                store.load_chapter(item["index"]) for item in store.load_manifest()["chapters"]
            ]
            inventory = build_layout_inventory(str(source), chapters)
            profile = LayoutProfile(
                source_sha256=inventory.source_sha256,
                inventory_digest=inventory.digest,
                policy_version=inventory.policy_version,
                assignments=tuple(
                    LayoutAssignment(
                        node.node_id,
                        node.resource_href,
                        node.path,
                        node.source_sha256,
                        "body",
                    )
                    for node in inventory.nodes
                ),
                provenance={},
            )
            theme = ThemeService(
                resolve_theme("builtin:chinese-reading", "builtin:bilingual"),
                layout=profile,
            )
        else:
            theme = ThemeService(resolve_theme(None, "builtin:bilingual"))
        requests = [
            EpubOutput(
                root / name,
                "bilingual" if bilingual else "monolingual",
                bilingual,
                partial(assemble_epub, store, str(source), bilingual=bilingual, theme=theme),
            )
            for name, bilingual in (("mono.epub", False), ("bilingual.epub", True))
        ]
        return source, store, requests

    def test_directory_access_failure_is_only_unsupported_on_windows(self) -> None:
        from trans_novel.assemble.epub import publication

        for platform in ("nt", "posix"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, store, requests = self._requests(root)
                platform_os = SimpleNamespace(**vars(publication.os))
                platform_os.name = platform
                with (
                    patch.object(publication, "os", platform_os),
                    patch.object(
                        platform_os,
                        "open",
                        side_effect=PermissionError(errno.EACCES, "directory handle unavailable"),
                    ),
                ):
                    if platform == "nt":
                        publish_epubs(store, source, requests)
                    else:
                        with self.assertRaises(EpubPublishError) as raised:
                            publish_epubs(store, source, requests)
                        self.assertTrue(raised.exception.published)
                report = store.load_epub_verification()
                if platform == "nt":
                    for request in requests:
                        output = Path(request.final_path)
                        receipt = report["published_outputs"][output.name]
                        self.assertTrue(receipt["passed"])
                        self.assertTrue(receipt["published"])
                        self.assertEqual(
                            receipt["output_sha256"],
                            hashlib.sha256(output.read_bytes()).hexdigest(),
                        )
                        self.assertIn(
                            "directory_fsync_unsupported",
                            {warning["code"] for warning in receipt["warnings"]},
                        )
                else:
                    self.assertFalse(report["passed"])
                    self.assertFalse(Path(requests[1].final_path).exists())

    def test_second_verification_failure_preserves_both_previous_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, store, requests = self._requests(root)
            for request in requests:
                Path(request.final_path).write_bytes(b"previous")
            second = requests[1]
            requests[1] = EpubOutput(
                second.final_path,
                second.mode,
                second.bilingual,
                lambda path: Path(path).write_bytes(b"invalid EPUB"),
            )
            with self.assertRaises(EpubVerificationError):
                publish_epubs(store, source, requests, output_digest="a" * 64)
            for request in requests:
                self.assertEqual(Path(request.final_path).read_bytes(), b"previous")
            self.assertFalse(list(root.glob(".*.epub-verify-*.tmp")))
            self.assertFalse(store.load_epub_verification()["published"])

    def test_second_replace_failure_keeps_first_physical_publication_receipt(self) -> None:
        from trans_novel.assemble.epub import publication

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, store, requests = self._requests(root)
            mono, bilingual = (Path(request.final_path) for request in requests)
            mono.write_bytes(b"previous mono")
            bilingual.write_bytes(b"previous bilingual")
            replace = publication.os.replace

            def fail_second(source_path, target_path):
                if Path(target_path) == bilingual:
                    raise OSError("replacement unavailable")
                return replace(source_path, target_path)

            with (
                patch("trans_novel.assemble.epub.publication.os.replace", side_effect=fail_second),
                self.assertRaises(EpubPublishError) as raised,
            ):
                publish_epubs(store, source, requests, output_digest="b" * 64)
            self.assertTrue(zipfile.is_zipfile(mono))
            self.assertEqual(bilingual.read_bytes(), b"previous bilingual")
            report = store.load_epub_verification()
            self.assertEqual(raised.exception.report, report)
            self.assertFalse(report["published"])
            first = report["published_outputs"][mono.name]
            self.assertTrue(first["published"])
            self.assertTrue(first["passed"])
            self.assertEqual(first["output_sha256"], hashlib.sha256(mono.read_bytes()).hexdigest())
            self.assertFalse(list(root.glob(".*.epub-verify-*.tmp")))

    def test_note_themed_pair_runs_real_triplet_proof_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, store, requests = self._requests(root, note_theme=True)

            mono, bilingual = publish_epubs(store, source, requests)

            self.assertTrue(Path(mono).is_file())
            self.assertTrue(Path(bilingual).is_file())
            report = store.load_epub_verification()
            self.assertTrue(report["triplet"]["structural_pass"], report["triplet"])
            self.assertTrue(report["triplet"]["mono"]["theme"]["note_change_count"])
            self.assertTrue(report["triplet"]["bilingual"]["theme"]["note_change_count"])

    def test_published_pair_proof_rejects_missing_evidence_and_changed_bytes(self) -> None:
        from trans_novel.benchmark.epub_check import validate_epub_triplet

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, store, requests = self._requests(root)
            mono, bilingual = publish_epubs(store, source, requests, output_digest="c" * 64)
            report = store.load_epub_verification()
            proof = validate_epub_triplet(
                source, mono, bilingual, publication_report=report, output_digest="c" * 64
            )
            self.assertTrue(proof["structural_pass"], proof)
            missing = {key: value for key, value in report.items() if key != "triplet"}
            with self.assertRaisesRegex(ValueError, "publication proof"):
                validate_epub_triplet(
                    source, mono, bilingual, publication_report=missing, output_digest="c" * 64
                )
            with open(bilingual, "ab") as stream:
                stream.write(b"changed after verification")
            with self.assertRaisesRegex(ValueError, "publication proof"):
                validate_epub_triplet(
                    source, mono, bilingual, publication_report=report, output_digest="c" * 64
                )
