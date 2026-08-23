"""准备服务私有测试：语言归一化、风格分析样本选择。"""

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
            # 段落勿以「第N章」开头，避免被 TXT reader 的章标题启发式误判
            body = "\n\n".join(f"章{i}の段落{j}です。" + "あ" * 60 for j in range(8))
            chapters.append(f"# 第{i}章\n\n{body}")
        with open(txt, "w", encoding="utf-8") as f:
            f.write("\n\n".join(chapters))
        return load_document(txt, "ja", "zh")

    def test_sample_text_multipoint(self):
        """labeled=True 多点采样带三个标注；labeled=False 为纯源文单段。"""
        with tempfile.TemporaryDirectory() as d:
            doc = self._long_doc(d)
            labeled = PreparationService.sample_text(doc)
            for tag in ("【开头样章】", "【中部样章】", "【结尾样章】"):
                self.assertIn(tag, labeled)
            plain = PreparationService.sample_text(doc, labeled=False)
            self.assertNotIn("样章】", plain)
            self.assertIn("章0の段落0です", plain)

    def test_sample_text_short_book_dedup(self):
        """单章书：三个采样点重合，只取一次、不重复。"""
        with tempfile.TemporaryDirectory() as d:
            from trans_novel.ingest.segmenter import load_document

            txt = os.path.join(d, "short.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write("# 唯一章\n\n" + "长段落。" + "あ" * 300)
            doc = load_document(txt, "ja", "zh")
            sample = PreparationService.sample_text(doc)
            self.assertEqual(sample.count("【开头样章】"), 1)
            self.assertNotIn("【中部样章】", sample)
            self.assertNotIn("【结尾样章】", sample)


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
