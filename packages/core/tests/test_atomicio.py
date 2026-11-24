"""Atomic publication helper tests (audit A-1 / vision V2)."""

from __future__ import annotations

import os
import threading
import unittest
from pathlib import Path

from wenyi_core.atomicio import PublishError, publish_bytes, publish_target
from wenyi_core.export_paths import NumberedOutput
from wenyi_core.storage.io_path import plain_path, to_io_path


def test_numbered_output_does_not_replace_a_late_file_at_a_long_path(tmp_path, monkeypatch):
    target = tmp_path / ("a" * 180) / "book.pdf"
    Path(to_io_path(target.parent)).mkdir()
    actual_exists = os.path.lexists
    outputs = []

    def render(path):
        Path(path).write_bytes(b"new output")
        return path

    def another_writer_wins_after_probe(path):
        if path == to_io_path(target):
            assert not actual_exists(path)
            Path(path).write_bytes(b"other output")
            return False
        return actual_exists(path)

    monkeypatch.setattr(os.path, "lexists", another_writer_wins_after_probe)
    output = NumberedOutput(str(target), lambda _, actual: outputs.append(actual)).publish(
        render, bilingual=False, include_bilingual=False
    )
    assert Path(to_io_path(target)).read_bytes() == b"other output"
    assert output == str(target.with_name("book (1).pdf"))
    assert Path(to_io_path(output)).read_bytes() == b"new output"
    assert outputs == [output]


class TestPublishBytes(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def _residue(self) -> list[str]:
        return [name for name in os.listdir(self.dir) if name.endswith(".tmp")]

    def test_publish_bytes_replaces_target_atomically(self):
        target = self.dir / "book.pdf"
        target.write_bytes(b"old")
        out = publish_bytes(target, b"new-content")
        self.assertEqual(out, target)
        self.assertEqual(target.read_bytes(), b"new-content")
        self.assertEqual(self._residue(), [])

    def test_publish_to_long_destination_uses_short_sibling(self):
        target = self.dir / ("a" * 230 + ".zh.epub")
        Path(to_io_path(target)).write_bytes(b"old")
        with publish_target(target) as temp:
            self.assertEqual(Path(plain_path(temp.parent)), target.parent)
            self.assertLessEqual(len(temp.name.encode("utf-8")), 255)
            temp.write_bytes(b"new")
            self.assertEqual(Path(to_io_path(target)).read_bytes(), b"old")
        self.assertEqual(Path(to_io_path(target)).read_bytes(), b"new")
        self.assertEqual(self._residue(), [])

    def test_failure_keeps_previous_target_and_cleans_temp(self):
        target = self.dir / "book.epub"
        target.write_bytes(b"previous")
        with self.assertRaises(ValueError):
            with publish_target(target) as temp:
                temp.write_bytes(b"partial")
                raise ValueError("disk full mid-write")
        self.assertEqual(target.read_bytes(), b"previous")
        self.assertEqual(self._residue(), [])

    def test_creates_missing_parent_directories(self):
        target = self.dir / "nested" / "deep" / "out.txt"
        publish_bytes(target, b"data")
        self.assertEqual(target.read_bytes(), b"data")

    def test_concurrent_publishes_never_interleave(self):
        target = self.dir / "book.epub"
        payloads = [bytes([number]) * 4096 for number in range(8)]
        errors: list[Exception] = []

        def worker(payload: bytes) -> None:
            try:
                for _ in range(20):
                    publish_bytes(target, payload)
            except Exception as error:  # noqa: BLE001 - collected for assertion
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(payload,)) for payload in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        final = target.read_bytes()
        self.assertIn(final, payloads)  # one complete payload, never a mix
        self.assertEqual(self._residue(), [])

    def test_pinned_target_fails_with_readable_error(self):
        if os.name != "nt":
            self.skipTest("Windows keeps the target locked while a handle is open")
        target = self.dir / "book.pdf"
        target.write_bytes(b"old")
        with open(target, "rb") as pinned:
            self.assertIsNotNone(pinned)
            with self.assertRaises(PublishError) as caught:
                publish_bytes(target, b"new")
            self.assertIn("stays in use", str(caught.exception))
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(self._residue(), [])


if __name__ == "__main__":
    unittest.main()
