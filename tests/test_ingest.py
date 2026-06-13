"""摄取与切分的冒烟测试。"""

from __future__ import annotations

import os
import tempfile
import unittest

from trans_novel.ingest.segmenter import load_document, chapter_batches
from trans_novel.ingest.models import KIND_HEADING
from tests.sample_data import write_sample_txt, write_sample_epub


class TestTextIngest(unittest.TestCase):
    def test_text_chapters_and_segments(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "novel.txt")
            write_sample_txt(p)
            doc = load_document(p, "ja", "zh")

        self.assertEqual(doc.fmt, "text")
        self.assertEqual(len(doc.chapters), 2)
        ch1 = doc.chapters[0]
        self.assertEqual(ch1.title, "第一章　出会い")
        # 标题 heading + 3 段正文
        self.assertEqual(ch1.segments[0].kind, KIND_HEADING)
        self.assertEqual(len(ch1.text_segments), 4)

    def test_batching(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "novel.txt")
            write_sample_txt(p)
            doc = load_document(p, "ja", "zh")
        batches = chapter_batches(doc.chapters[0], max_chars=60)
        # 总段数守恒
        total = sum(len(b) for b in batches)
        self.assertEqual(total, len(doc.chapters[0].text_segments))
        self.assertGreater(len(batches), 1)  # 60 字符预算应切出多批


class TestEpubIngest(unittest.TestCase):
    def test_epub_chapters_and_anchors(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "novel.epub")
            write_sample_epub(p)
            doc = load_document(p, "ja", "zh")

        self.assertEqual(doc.fmt, "epub")
        self.assertEqual(len(doc.chapters), 2)
        ch1 = doc.chapters[0]
        self.assertEqual(ch1.title, "第一章　出会い")
        self.assertEqual(len(ch1.text_segments), 3)  # h1 + 2 p
        # 每个 segment 都有回填锚点，且模板里含该锚点
        for s in ch1.text_segments:
            self.assertIsNotNone(s.anchor)
            self.assertIn(s.anchor, ch1.template)
        self.assertIsNotNone(ch1.href)


if __name__ == "__main__":
    unittest.main()
