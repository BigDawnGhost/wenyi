"""Preparation tests for language normalization and style-sample selection."""

from __future__ import annotations

import os
import tempfile
import unittest

from trans_novel.pipeline.language import normalize_lang
from trans_novel.pipeline.preparation import PreparationService


class TestSampleText(unittest.TestCase):
    def _long_doc(self, d):
        from trans_novel.ingest.segmenter import load_document

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
            from trans_novel.ingest.segmenter import load_document

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
        self.assertEqual(normalize_lang("Japanese"), "ja")
        self.assertEqual(normalize_lang("日语"), "ja")
        self.assertEqual(normalize_lang("RU"), "ru")
        self.assertEqual(normalize_lang("russian"), "ru")
        self.assertEqual(normalize_lang("fr"), "fr")
        self.assertEqual(normalize_lang("unknown"), "")
        self.assertEqual(normalize_lang(""), "")


if __name__ == "__main__":
    unittest.main()
