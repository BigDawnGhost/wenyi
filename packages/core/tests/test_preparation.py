"""Preparation tests for language normalization and style-sample selection."""

from __future__ import annotations

import os
import tempfile
import unittest

import pytest
from wenyi_core.config import Config
from wenyi_core.i18n.languages import normalize_language
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.preparation import PreparationService
from wenyi_core.storage.file import FileStorage

from tests.fake_llm import routing_handler
from tests.sample_data import write_sample_txt


def _preparation_inputs(tmp_path, book_understanding=True):
    source = tmp_path / "book.txt"
    write_sample_txt(str(source))
    config = Config.from_dict(
        {
            "language": {"source": "ja", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {
                "book_understanding": book_understanding,
                "prescan_concurrency": 2,
                "review": False,
                "polish": False,
            },
            "paths": {"state_dir": str(tmp_path / "state")},
        }
    )
    return source, config, FileStorage(str(tmp_path / "run"))


@pytest.mark.parametrize("entry_point", ["prepare", "prepare_for_translation", "run"])
@pytest.mark.parametrize("book_understanding", [False, True])
def test_chapter_prescan_finishes_before_style_analysis(tmp_path, entry_point, book_understanding):
    source, config, store = _preparation_inputs(tmp_path, book_understanding)
    digest = "DIGEST-ONLY-CONTENT"

    def handler(messages, tier, json_mode):
        system = messages[0]["content"]
        if "chapter digest writer" in system:
            return digest
        if "pre-translation analyst" in system:
            assert not store.exists(), "The initialization manifest must commit last"
            chapter = store.load_chapter(0)
            assert bool(chapter.meta.get("source_digest")) == book_understanding
            assert digest not in messages[-1]["content"], (
                "Style analysis still reads source samples"
            )
        return routing_handler(messages, tier, json_mode)

    client = FakeClient(handler=handler)
    getattr(Orchestrator(config, client=client, storage=store), entry_point)(str(source))
    operations = [call["operation"] for call in client.calls]
    style_position = operations.index("analysis.style")
    chapter_calls = [i for i, operation in enumerate(operations) if operation == "synopsis.chapter"]
    if book_understanding:
        assert len(chapter_calls) == len(store.load_manifest()["chapters"])
        assert max(chapter_calls) < style_position
        if entry_point != "prepare":
            assert style_position < operations.index("synopsis.book")
    else:
        assert chapter_calls == []
        assert "synopsis.book" not in operations
    assert store.exists()


def test_synopsis_interrupt_resumes_without_repeating_prescan_or_style(tmp_path):
    source, config, store = _preparation_inputs(tmp_path)

    def handler(messages, tier, json_mode):
        if "whole-book synopsis writer" in messages[0]["content"]:
            raise KeyboardInterrupt("during book synopsis")
        return routing_handler(messages, tier, json_mode)

    with pytest.raises(KeyboardInterrupt, match="book synopsis"):
        Orchestrator(
            config, client=FakeClient(handler=handler), storage=store
        ).prepare_for_translation(str(source))
    assert store.exists()
    analysis = store.load_analysis()
    chapters = [store.load_chapter(row["index"]) for row in store.load_manifest()["chapters"]]
    assert all(chapter.meta.get("source_digest") for chapter in chapters)

    client = FakeClient(handler=routing_handler)
    Orchestrator(config, client=client, storage=store).prepare_for_translation(str(source))
    assert [call["operation"] for call in client.calls] == ["synopsis.book"]
    assert store.load_analysis() == {
        **(analysis or {}),
        "book_synopsis": "全书概览：主线与人物关系，整体基调。",
    }
    assert [store.load_chapter(chapter.index) for chapter in chapters] == chapters


def test_style_failure_after_prescan_keeps_initialization_uncommitted(tmp_path):
    source, config, store = _preparation_inputs(tmp_path)

    def handler(messages, tier, json_mode):
        if "pre-translation analyst" in messages[0]["content"]:
            assert store.load_chapter(0).meta.get("source_digest")
            raise RuntimeError("style analysis failed")
        return routing_handler(messages, tier, json_mode)

    with pytest.raises(RuntimeError, match="style analysis failed"):
        Orchestrator(config, client=FakeClient(handler=handler), storage=store).prepare(str(source))
    assert not store.exists()
    assert store.load_analysis() is None

    client = FakeClient(handler=routing_handler)
    Orchestrator(config, client=client, storage=store).prepare_for_translation(str(source))
    operations = [call["operation"] for call in client.calls]
    assert operations.count("synopsis.chapter") == len(store.load_manifest()["chapters"])
    assert operations.index("synopsis.chapter") < operations.index("analysis.style")
    assert store.exists()


class TestSampleText(unittest.TestCase):
    def _long_doc(self, d):
        from wenyi_core.ingest.segmenter import load_document

        txt = os.path.join(d, "long.txt")
        chapters = []
        for i in range(3):
            # Avoid chapter-like prefixes so the TXT reader does not misclassify body paragraphs as headings.
            body = "\n\n".join(f"章{i}の段落{j}です。" + "あ" * 60 for j in range(8))
            chapters.append(f"# 第{i}章\n\n{body}")
        with open(txt, "w", encoding="utf-8") as f:
            f.write("\n\n".join(chapters))
        return load_document(txt, "ja", "zh")

    def test_sample_text_multipoint(self):
        """Labeled sampling returns three labeled positions; unlabeled sampling returns pure
        source text.
        """
        with tempfile.TemporaryDirectory() as d:
            doc = self._long_doc(d)
            labeled = PreparationService.sample_text(doc)
            for tag in ("【Opening sample】", "【Middle sample】", "【Ending sample】"):
                self.assertIn(tag, labeled)
            plain = PreparationService.sample_text(doc, labeled=False)
            self.assertNotIn("样章】", plain)
            self.assertIn("章0の段落0です", plain)

    def test_sample_text_short_book_dedup(self):
        """Deduplicate all three sampling positions for a one-chapter book."""
        with tempfile.TemporaryDirectory() as d:
            from wenyi_core.ingest.segmenter import load_document

            txt = os.path.join(d, "short.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write("# 唯一章\n\n" + "长段落。" + "あ" * 300)
            doc = load_document(txt, "ja", "zh")
            sample = PreparationService.sample_text(doc)
            self.assertEqual(sample.count("【Opening sample】"), 1)
            self.assertNotIn("【Middle sample】", sample)
            self.assertNotIn("【Ending sample】", sample)


class TestLangNormalize(unittest.TestCase):
    def test_normalize_lang(self):
        self.assertEqual(normalize_language("Japanese"), "ja")
        self.assertEqual(normalize_language("日语"), "ja")
        self.assertEqual(normalize_language("RU"), "ru")
        self.assertEqual(normalize_language("russian"), "ru")
        self.assertEqual(normalize_language("fr"), "fr")
        self.assertEqual(normalize_language("Vietnamese"), "vi")
        self.assertEqual(normalize_language("vi-VN"), "vi")
        self.assertEqual(normalize_language("unknown"), "")
        self.assertEqual(normalize_language(""), "")


if __name__ == "__main__":
    unittest.main()
