"""EPUB 物理发布收据的持久化回归测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trans_novel.pipeline.state import RunStore


class TestEpubPublicationReceipts(unittest.TestCase):
    def test_failed_attempt_keeps_prior_publication_and_true_replaces_its_label(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            first = {
                "output_label": "mono.epub",
                "published": True,
                "passed": True,
                "output_sha256": "a" * 64,
            }
            store.save_epub_verification(first)
            first_report = {
                key: value for key, value in first.items() if key != "published_outputs"
            }
            self.assertEqual(first["published_outputs"], {"mono.epub": first_report})
            failed = {
                "output_label": "bilingual.epub",
                "published": False,
                "passed": False,
                "published_outputs": {"forged": {}},
            }
            store.save_epub_verification(failed)
            saved = store.load_epub_verification()
            assert saved is not None
            self.assertEqual(saved["published_outputs"], {"mono.epub": first_report})
            self.assertEqual(failed["published_outputs"], saved["published_outputs"])

            replacement = {
                "output_label": "mono.epub",
                "published": True,
                "passed": False,
                "output_sha256": "b" * 64,
                "published_outputs": {"forged": {}},
            }
            store.save_epub_verification(replacement)
            saved = store.load_epub_verification()
            assert saved is not None
            self.assertEqual(
                saved["published_outputs"]["mono.epub"],
                {key: value for key, value in replacement.items() if key != "published_outputs"},
            )

    def test_malformed_persisted_receipts_fail_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            store.write_json(
                store.epub_verification_path,
                {"published_outputs": {"mono.epub": "not-a-report"}},
            )
            before = Path(store.epub_verification_path).read_bytes()

            with self.assertRaisesRegex(ValueError, "invalid EPUB verification report"):
                store.save_epub_verification(
                    {"output_label": "mono.epub", "published": True, "passed": True}
                )

            self.assertEqual(Path(store.epub_verification_path).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
