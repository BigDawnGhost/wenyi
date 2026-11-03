"""Export guard, resource-resolver and OPF-fallback tests (worklist-r2 X-5).

These pin the failure paths that implement AGENTS.md's invariant: source/state
disagreement must fail loudly instead of exporting a silent mismatch.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from wenyi_core.assemble.epub_navigation import _rewrite_opf_metadata
from wenyi_core.assemble.html_resources import _load_html_resource
from wenyi_core.assemble.writer import assemble
from wenyi_core.config import Config
from wenyi_core.ingest.models import KIND_TEXT  # noqa: F401 - documents segment kinds used below
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator

from tests.fake_llm import routing_handler
from tests.pipeline_fixtures import require_file_storage
from tests.sample_data import write_inline_sample_epub


def _run_full_pipeline(input_path: str, state_dir: str):
    """Local duplicate of test_assemble._run: collected test modules must not be imported."""
    cfg = Config.from_dict(
        {
            "language": {"source": "ja", "target": "zh"},
            "llm": {
                "preset": "fake",
                "models": {
                    "default_strong": {"provider": "default", "model": "p"},
                    "default_cheap": {"provider": "default", "model": "f"},
                },
            },
            "pipeline": {"review": True, "review_autofix": False, "polish": True},
            "paths": {"state_dir": state_dir},
        }
    )
    orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
    return require_file_storage(orch.run(input_path)), cfg


def _rewrite_zip_excluding(src_path: str, dest_path: str, drop: str) -> None:
    with zipfile.ZipFile(src_path) as src, zipfile.ZipFile(dest_path, "w") as dest:
        for info in src.infolist():
            if info.filename == drop:
                continue
            dest.writestr(info, src.read(info.filename))


class TestEpubResourceGuards(unittest.TestCase):
    def _prepared(self, directory: str):
        epub = os.path.join(directory, "inline.epub")
        write_inline_sample_epub(epub)
        store, _ = _run_full_pipeline(epub, os.path.join(directory, "state"))
        return epub, store

    def test_undeclared_body_resource_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            epub, store = self._prepared(directory)
            chapter = store.load_chapter(0)
            chapter.segments[0].resource_href = "OEBPS/ghost.xhtml"
            store.save_chapter(chapter)

            with self.assertRaisesRegex(ValueError, "undeclared body resources"):
                assemble(store, epub, out_format="epub", about_page=False)

    def test_missing_body_resource_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            epub, store = self._prepared(directory)
            trimmed = os.path.join(directory, "trimmed.epub")
            _rewrite_zip_excluding(epub, trimmed, "OEBPS/ch1.xhtml")

            with self.assertRaisesRegex(ValueError, "body resource not found"):
                assemble(store, trimmed, out_format="epub", about_page=False)

    def test_stripped_backfill_anchors_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            epub, store = self._prepared(directory)
            with zipfile.ZipFile(epub) as src:
                ch1 = src.read("OEBPS/ch1.xhtml").decode("utf-8")
                entries = {
                    info.filename: (info, src.read(info.filename)) for info in src.infolist()
                }
            # Anchors are re-derived from the source at export time; removing the
            # anchored element entirely makes the fresh annotation miss the anchors the
            # state requires (robust under both id-based and ordinal anchor schemes).
            anchored_span = '<span id="kobo.1.1">Je suis là, sous le pommier.</span>'
            self.assertIn(anchored_span, ch1)
            rewritten = ch1.replace(anchored_span, "")
            trimmed = os.path.join(directory, "stripped.epub")
            with zipfile.ZipFile(trimmed, "w") as dest:
                for info, data in entries.values():
                    dest.writestr(
                        info,
                        rewritten.encode("utf-8") if info.filename == "OEBPS/ch1.xhtml" else data,
                    )

            with self.assertRaisesRegex(ValueError, "lacks backfill anchors"):
                assemble(store, trimmed, out_format="epub", about_page=False)


class TestHtmlResourceResolver(unittest.TestCase):
    def test_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            inside = Path(directory) / "keep.png"
            inside.write_bytes(b"inside")
            outside_dir = Path(directory).parent
            outside = outside_dir / "escape.png"
            outside.write_bytes(b"outside")
            try:
                loaded = _load_html_resource("keep.png", source_dir=directory)
                self.assertIsNotNone(loaded)
                escaped = _load_html_resource(
                    "../escape.png", source_dir=str(Path(directory) / "sub")
                )
                self.assertIsNone(escaped)
            finally:
                outside.unlink(missing_ok=True)

    def test_invalid_base64_data_uri_is_rejected(self):
        self.assertIsNone(
            _load_html_resource("data:image/png;base64,!!!not-base64!!!", source_dir=".")
        )


class TestOpfMetadataFallbacks(unittest.TestCase):
    def test_opf_without_title_and_language_gets_language_created(self):
        opf = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="bookid"><metadata/></package>'
        )
        encoded = _rewrite_opf_metadata(
            opf.encode("utf-8"), book_title="T", lang="zh-Hans", force_horizontal=False
        )
        text = encoded.decode("utf-8")
        self.assertIn("<dc:language>zh-Hans</dc:language>", text)
        # No dc:title/title element exists, so the title branch leaves the OPF otherwise
        # untouched (the fallback exists for damaged metadata, not synthesis).
        self.assertNotIn("<dc:title>", text)


if __name__ == "__main__":
    unittest.main()
