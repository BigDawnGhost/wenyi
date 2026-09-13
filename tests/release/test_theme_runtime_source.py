from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.build_theme_runtime import SourceBuildError, unpack_source


def _archive(entries: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for member, content in entries:
            archive.addfile(member, io.BytesIO(content) if content is not None else None)
    return output.getvalue()


def _directory(name: str) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    return member


def _file(name: str, content: bytes) -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.size = len(content)
    return member, content


class TestThemeRuntimeSource(unittest.TestCase):
    def test_hash_mismatch_does_not_mutate_destination(self) -> None:
        blob = _archive([(_directory("source"), None), _file("source/file", b"content")])
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "source"
            sentinel = Path(directory) / "sentinel"
            sentinel.write_bytes(b"unchanged")

            with self.assertRaisesRegex(SourceBuildError, "SHA256 mismatch"):
                unpack_source(blob, "0" * 64, destination)

            self.assertFalse(destination.exists())
            self.assertEqual(sentinel.read_bytes(), b"unchanged")

    def test_unsafe_members_do_not_escape_destination(self) -> None:
        symlink = tarfile.TarInfo("source/link")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "../../outside"
        hardlink = tarfile.TarInfo("source/hardlink")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "../../outside"
        fifo = tarfile.TarInfo("source/fifo")
        fifo.type = tarfile.FIFOTYPE
        cases = {
            "traversal": [_file("source/../../outside", b"escape")],
            "absolute": [_file("/outside", b"escape")],
            "backslash": [_file("source\\outside", b"escape")],
            "symlink": [(symlink, None)],
            "hardlink": [(hardlink, None)],
            "special": [(fifo, None)],
            "duplicate": [_file("source/file", b"one"), _file("source/file", b"two")],
            "file-parent": [_file("source/file", b"one"), _file("source/file/child", b"two")],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, unsafe_entries in cases.items():
                with self.subTest(name=name):
                    destination = root / name
                    outside = root / "outside"
                    blob = _archive([(_directory("source"), None), *unsafe_entries])

                    with self.assertRaises(SourceBuildError):
                        unpack_source(blob, hashlib.sha256(blob).hexdigest(), destination)

                    self.assertFalse(destination.exists())
                    self.assertFalse(outside.exists())

    def test_valid_archive_preserves_file_bytes(self) -> None:
        content = b"\x00complete pinned source\xff"
        blob = _archive(
            [
                (_directory("source"), None),
                (_directory("source/nested"), None),
                _file("source/nested/file.bin", content),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "unpacked"

            source = unpack_source(blob, hashlib.sha256(blob).hexdigest(), destination)

            self.assertEqual(source, destination / "source")
            self.assertEqual((source / "nested/file.bin").read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
