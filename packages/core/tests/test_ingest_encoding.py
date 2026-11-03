"""Source decoding ladder tests (audit A-6 / vision V3)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wenyi_core.ingest.encoding import decode_source
from wenyi_core.ingest.errors import IngestError
from wenyi_core.ingest.text_reader import read_text


class TestDecodeSource(unittest.TestCase):
    def test_utf8_passthrough(self):
        text, encoding = decode_source("第一章\n\n你好。".encode("utf-8"))
        self.assertEqual(encoding, "utf-8")
        self.assertIn("第一章", text)

    def test_utf8_bom_is_stripped(self):
        text, encoding = decode_source(b"\xef\xbb\xbf" + "标题".encode("utf-8"))
        self.assertEqual(encoding, "utf-8")
        self.assertEqual(text, "标题")

    def test_shift_jis_novel_decodes(self):
        # The audit A-6 sample: Shift-JIS Japanese text must not raise UnicodeDecodeError.
        sample = "第一章　序\n\nこれはシフトJISで書かれた日本語の小説です。囲碁の対局など。\n"
        text, encoding = decode_source(sample.encode("shift_jis"))
        self.assertEqual(encoding, "shift_jis")
        self.assertIn("シフトJIS", text)

    def test_gbk_text_decodes_as_cp936(self):
        sample = "第一章 开端\n\n这是用国标编码保存的中文段落。\n"
        text, encoding = decode_source(sample.encode("cp936"))
        self.assertEqual(encoding, "cp936")
        self.assertIn("第一章", text)

    def test_undecodable_bytes_raise_actionable_error(self):
        with self.assertRaises(IngestError) as caught:
            decode_source(b"\x91\xe6\x88\xea\xff\xfe\x81@\x00\x9d")
        self.assertIn("convert the file to UTF-8", str(caught.exception))

    def test_ambiguous_legacy_bytes_are_rejected_not_guessed(self):
        # Kanji-only byte sequences that decode differently under Shift-JIS and GBK with
        # no kana or CJK-majority signal must be rejected, not silently misread.
        data = "漢字".encode("shift_jis")
        decoded = {encoding: data.decode(encoding) for encoding in ("shift_jis", "cp936")}
        assert len(set(decoded.values())) == 2
        with self.assertRaises(IngestError) as caught:
            decode_source(data)
        self.assertIn("ambiguous", str(caught.exception))


class TestTextReaderLegacyEncodings(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def test_read_text_accepts_shift_jis_file(self):
        path = self.dir / "novel.txt"
        path.write_bytes("第一章　序\n\nこれは日本語の本文です。\n".encode("shift_jis"))
        document = read_text(str(path), "ja", "zh")
        self.assertEqual(len(document.chapters), 1)
        sources = [segment.source for segment in document.chapters[0].segments]
        self.assertIn("これは日本語の本文です。", sources)

    def test_read_text_still_accepts_utf8_and_markdown(self):
        path = self.dir / "book.md"
        path.write_bytes("# Title\n\nBody text.\n".encode("utf-8"))
        document = read_text(str(path), "en", "zh")
        self.assertEqual(document.chapters[0].title, "Title")


if __name__ == "__main__":
    unittest.main()
