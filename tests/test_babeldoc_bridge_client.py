"""Tests for MIT-side BabelDOC bridge HTTP client (no babeldoc import)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from trans_novel.ingest.pdf_babeldoc import read_pdf_babeldoc
from trans_novel.pdf_bridge.client import BabeldocBridgeClient, BabeldocBridgeError


class BabeldocBridgeClientTests(unittest.TestCase):
    def test_health_wraps_connection_errors(self):
        client = BabeldocBridgeClient("http://127.0.0.1:9")
        with patch("trans_novel.pdf_bridge.client.httpx.get", side_effect=OSError("down")):
            with self.assertRaises(BabeldocBridgeError):
                client.health()

    def test_extract_posts_multipart(self):
        client = BabeldocBridgeClient("http://bridge.test")
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            handle.write(b"%PDF-1.4 mock")
            pdf_path = Path(handle.name)
        try:
            health = MagicMock()
            health.raise_for_status = MagicMock()
            health.json.return_value = {"ok": True}
            extract = MagicMock()
            extract.status_code = 200
            extract.json.return_value = {
                "session_id": "abc",
                "paragraphs": {"paragraphs": [{"id": "0:1", "source": "Hello world here"}]},
            }
            with (
                patch("trans_novel.pdf_bridge.client.httpx.get", return_value=health),
                patch("trans_novel.pdf_bridge.client.httpx.post", return_value=extract) as post,
            ):
                payload = client.extract(pdf_path, pages="15")
            self.assertEqual(payload["session_id"], "abc")
            args, kwargs = post.call_args
            self.assertTrue(str(args[0]).endswith("/extract"))
            self.assertEqual(kwargs["data"].get("pages"), "15")
        finally:
            pdf_path.unlink(missing_ok=True)

    def test_read_pdf_babeldoc_builds_segments(self):
        payload = {
            "session_id": "sess1",
            "paragraphs": {
                "paragraphs": [
                    {
                        "id": "14:1",
                        "source": "Organization",
                        "layout_label": "title",
                        "page": 14,
                        "index": 1,
                    },
                    {
                        "id": "14:2",
                        "source": "This chapter introduces methods.",
                        "layout_label": "plain text",
                        "page": 14,
                        "index": 2,
                    },
                ]
            },
        }
        fake = MagicMock()
        fake.extract.return_value = payload
        with (
            patch("trans_novel.ingest.pdf_babeldoc.BabeldocBridgeClient", return_value=fake),
            patch("trans_novel.ingest.pdf_babeldoc.toc_chapter_starts", return_value=[]),
            tempfile_directory() as cache_dir,
        ):
            doc = read_pdf_babeldoc(
                "book.pdf",
                "en",
                "zh",
                bridge_url="http://bridge.test",
                pages="15",
                cache_dir=cache_dir,
            )
            self.assertTrue(doc.meta.get("babeldoc"))
            self.assertEqual(doc.meta.get("babeldoc_session_id"), "sess1")
            self.assertEqual(len(doc.chapters), 1)
            self.assertEqual(len(doc.chapters[0].segments), 2)
            self.assertEqual(doc.chapters[0].segments[0].kind, "heading")
            self.assertEqual(doc.chapters[0].segments[0].meta.get("babeldoc_id"), "14:1")
            cache = Path(cache_dir) / "babeldoc_extract.json"
            self.assertTrue(cache.is_file())
            saved = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(saved["session_id"], "sess1")

    def test_toc_splits_chapters_by_page(self):
        from trans_novel.ingest.pdf_babeldoc import _chapters_from_paragraphs

        paragraphs = [
            {
                "id": "5:0",
                "source": "Author bio text here.",
                "page": 5,
                "layout_label": "plain text",
            },
            {
                "id": "6:0",
                "source": "Brief contents line.",
                "page": 6,
                "layout_label": "plain text",
            },
            {
                "id": "14:0",
                "source": "Preface body paragraph.",
                "page": 14,
                "layout_label": "plain text",
            },
            {
                "id": "14:1",
                "source": "More preface text here.",
                "page": 14,
                "layout_label": "plain text",
            },
        ]
        starts = [(5, "About the Author"), (6, "Brief Contents"), (14, "Preface")]
        chapters = _chapters_from_paragraphs(paragraphs, book_title="Book", toc_starts=starts)
        self.assertEqual(
            [c.title for c in chapters], ["About the Author", "Brief Contents", "Preface"]
        )
        self.assertEqual([len(c.segments) for c in chapters], [1, 1, 2])
        self.assertEqual(chapters[2].segments[1].meta.get("babeldoc_id"), "14:1")

    def test_toc_chapter_starts_reads_weaver(self):
        from trans_novel.ingest.pdf_babeldoc import toc_chapter_starts

        pdf = Path("Molecular Biology (5th Ed) (Robert F. Weaver) (z.pdf")
        if not pdf.is_file():
            self.skipTest("Weaver sample PDF not present")
        starts = toc_chapter_starts(pdf)
        self.assertGreater(len(starts), 5)
        titles = [t for _p, t in starts]
        self.assertTrue(any("Preface" in t for t in titles))
        self.assertTrue(any("About the Author" in t for t in titles))
        # Preface bookmark should land on 0-based page 14 for this book.
        preface_pages = [p for p, t in starts if "Preface" in t]
        self.assertIn(14, preface_pages)


class tempfile_directory:
    def __enter__(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        return self._tmp.name

    def __exit__(self, *args):
        self._tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
