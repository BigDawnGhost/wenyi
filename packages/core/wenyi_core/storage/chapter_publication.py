"""Redo record for publishing one file-backed chapter and its manifest status."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass

from ..models.document import Chapter
from .atomic_write import sync_directory, write_text_atomic
from .io_path import to_io_path

JOURNAL_NAME = ".chapter-publication.json"


def read_text(path: str) -> str:
    with open(to_io_path(path), encoding="utf-8") as handle:
        return handle.read()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PublicationImage:
    """A checked after-image and whether its canonical name still needs updating."""

    text: str
    value: dict
    needs_write: bool


@dataclass(frozen=True)
class ChapterPublication:
    """Both visible values for a committed chapter publication, under the state lock."""

    index: int
    chapter: PublicationImage
    manifest: PublicationImage

    @classmethod
    def commit(
        cls,
        journal_path: str,
        chapter_path: str,
        manifest_path: str,
        chapter: Chapter,
        status: str,
    ) -> None:
        """Write one redo record, then materialize the two verified images."""
        chapter_text = read_text(chapter_path)
        manifest_text = read_text(manifest_path)
        manifest = json.loads(manifest_text)
        source_hash = manifest.get("source_sha256")
        if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise ValueError("Cannot publish chapter without a valid manifest source_sha256")
        entries = [entry for entry in manifest["chapters"] if entry["index"] == chapter.index]
        if chapter.index < 0 or len(entries) != 1 or type(entries[0]["index"]) is not int:
            raise ValueError(f"Chapter {chapter.index} must appear once in the manifest")
        entries[0]["status"] = status

        chapter_image = _encode_image(chapter_text, chapter.model_dump())
        manifest_image = _encode_image(manifest_text, manifest)
        journal = {
            "version": 1,
            "index": chapter.index,
            "chapter": chapter_image,
            "manifest": manifest_image,
        }
        _materialize_images(
            journal_path,
            chapter_path,
            manifest_path,
            chapter_image["text"] if chapter_image["before"] != chapter_image["after"] else None,
            manifest_image["text"] if manifest_image["before"] != manifest_image["after"] else None,
            journal=journal,
        )

    @classmethod
    def read(
        cls, path: str, manifest_path: str, chapter_path: Callable[[int], str]
    ) -> ChapterPublication | None:
        try:
            raw = read_text(path)
        except FileNotFoundError:
            return None

        try:
            journal = json.loads(raw)
            index = journal["index"]
            if type(journal["version"]) is not int or journal["version"] != 1:
                raise ValueError("Unknown journal version")
            if type(index) is not int or index < 0:
                raise ValueError("Invalid chapter index")
            chapter, _ = cls._check_image(journal["chapter"], chapter_path(index))
            manifest, old_manifest = cls._check_image(journal["manifest"], manifest_path)
            source_hash = manifest.value["source_sha256"]
            if (
                type(chapter.value["index"]) is not int
                or Chapter.model_validate(chapter.value).index != index
                or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
                or json.loads(old_manifest).get("source_sha256") != source_hash
                or len([entry for entry in manifest.value["chapters"] if entry["index"] == index])
                != 1
            ):
                raise ValueError("Chapter or source identity mismatch")
        except (ValueError, KeyError, TypeError, AttributeError, FileNotFoundError) as error:
            raise ValueError(
                f"Cannot read pending chapter publication at {path}: invalid or mismatched state"
            ) from error
        return cls(index, chapter, manifest)

    @staticmethod
    def _check_image(image: dict, path: str) -> tuple[PublicationImage, str]:
        text = image["text"]
        after = _digest(text)
        if after != image["after"]:
            raise ValueError("Publication checksum mismatch")
        before = image["before"]
        if not re.fullmatch(r"[0-9a-f]{64}", before):
            raise ValueError("Invalid publication baseline checksum")
        current_text = read_text(path)
        current = _digest(current_text)
        if current not in (before, after):
            raise ValueError(f"State at {path} changed outside the publication")
        return PublicationImage(text, json.loads(text), current != after), current_text

    def materialize(self, journal_path: str, chapter_path: str, manifest_path: str) -> None:
        """Replay only unapplied images and retire the journal only after both persist."""
        _materialize_images(
            journal_path,
            chapter_path,
            manifest_path,
            self.chapter.text if self.chapter.needs_write else None,
            self.manifest.text if self.manifest.needs_write else None,
        )


def _encode_image(before: str, value: dict) -> dict[str, str]:
    """Bind a canonical after-image to its exact expected baseline (modulo newlines)."""
    after = json.dumps(value, ensure_ascii=False, indent=2)
    return {"before": _digest(before), "text": after, "after": _digest(after)}


def _materialize_images(
    journal_path: str,
    chapter_path: str,
    manifest_path: str,
    chapter: str | None,
    manifest: str | None,
    *,
    journal: dict | None = None,
) -> None:
    if journal is not None:
        write_text_atomic(journal_path, json.dumps(journal, ensure_ascii=False))
    run_dir = os.path.dirname(journal_path)
    # Also required on recovery: the old process may have stopped just after rename.
    sync_directory(run_dir)
    if chapter is not None:
        write_text_atomic(chapter_path, chapter)
    if manifest is not None:
        write_text_atomic(manifest_path, manifest)
    sync_directory(os.path.dirname(chapter_path))
    sync_directory(run_dir)
    os.unlink(to_io_path(journal_path))
    sync_directory(run_dir)
