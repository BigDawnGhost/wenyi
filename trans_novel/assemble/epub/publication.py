"""Durable EPUB publication transaction."""

from __future__ import annotations

import errno
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from trans_novel.assemble.epub.metadata import epub_language
from trans_novel.assemble.epub.verification import (
    EpubPublishError,
    EpubVerificationError,
    archive_model,
    verify,
)


def fsync_file(path: str) -> None:
    with open(path, "rb") as stream:
        os.fsync(stream.fileno())


def is_unsupported_dir_fsync(error: OSError) -> bool:
    return error.errno in {
        value for value in (errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", -1)) if value
    }


def persist_failure(store: Any, report: dict[str, Any], cause: BaseException | None = None) -> None:
    try:
        store.save_epub_verification(report)
        store.log_event_required(
            "epub_verification_failed",
            output=report["output_label"],
            assurance=report["assurance"],
            failure_count=len(report["failures"]),
            warning_count=len(report["warnings"]),
            published=bool(report["published"]),
        )
    except Exception as error:
        raise EpubPublishError(report, published=bool(report["published"]), cause=error) from error


def raise_preflight(
    store: Any,
    final: str,
    source_path: str | os.PathLike[str] | None,
    mode: str,
    code: str,
    detail: str,
    cause: BaseException | None = None,
) -> None:
    report = {
        "schema_version": 1,
        "mode": mode,
        "assurance": "verified",
        "passed": False,
        "published": False,
        "source_sha256": archive_model.sha256(Path(source_path)) if source_path else None,
        "output_sha256": "",
        "output_label": verify.output_label(final),
        "failures": [archive_model.item("publish", code, "<output>", detail)],
        "warnings": [],
        "checked": {},
        "authorized_differences": {
            "text_slots": 0,
            "toc_labels": 0,
            "language_fields": 0,
            "bilingual_nodes": 0,
        },
    }
    persist_failure(store, report, cause)
    raise EpubPublishError(report, published=False, cause=cause)


def prepare_publication(
    store: Any,
    temp: str,
    source_path: str | os.PathLike[str] | None,
    final: str,
    *,
    mode: str,
    bilingual: bool,
    target_lang: str | None,
    bilingual_order: str,
    writer: Callable[[str], object],
) -> dict[str, Any]:
    try:
        writer(temp)
        report = verify.verify_epub(
            temp,
            source_path=source_path,
            store=store,
            mode=mode,
            bilingual=bilingual,
            target_lang=target_lang,
            bilingual_order=bilingual_order,
        )
        report["output_label"] = verify.output_label(final)
    except Exception as cause:
        report = verify.verify_epub(
            temp,
            source_path=source_path,
            store=store,
            mode=mode,
            bilingual=bilingual,
            target_lang=target_lang,
            bilingual_order=bilingual_order,
        )
        report["output_label"] = verify.output_label(final)
        report["passed"] = False
        writer_detail = f"{type(cause).__name__}: {cause}"[:500]
        report["failures"] = archive_model.sort_items(
            report["failures"]
            + [archive_model.item("publish", "writer_failed", "<output>", writer_detail)]
        )
        persist_failure(store, report, cause)
        raise EpubVerificationError(report, cause=cause) from cause
    if not report["passed"]:
        report["published"] = False
        persist_failure(store, report)
        raise EpubVerificationError(report)
    report["published"] = False
    try:
        store.save_epub_verification(report)
    except Exception as cause:
        raise EpubPublishError(report, published=False, cause=cause) from cause
    return report


def _replace_and_fsync(
    temp: str, final: str, parent: str, report: dict[str, Any], replacement_state: list[bool]
) -> None:
    os.replace(temp, final)
    replacement_state[0] = True
    fsync_file(final)
    try:
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        if is_unsupported_dir_fsync(error):
            report["warnings"] = archive_model.sort_items(
                report["warnings"]
                + [
                    archive_model.item(
                        "publish", "directory_fsync_unsupported", "<output>", "unsupported"
                    )
                ]
            )
        else:
            raise


def _raise_publish_failure(
    store: Any, report: dict[str, Any], replaced: bool, cause: OSError
) -> None:
    if replaced:
        report["published"] = True
        report["passed"] = False
        report["failures"] = archive_model.sort_items(
            report["failures"]
            + [archive_model.item("publish", "durability_failed", "<output>", "fsync")]
        )
        persist_failure(store, report, cause)
        raise EpubPublishError(report, published=True, cause=cause) from cause
    report["passed"] = False
    report["published"] = False
    report["failures"] = archive_model.sort_items(
        report["failures"] + [archive_model.item("publish", "replace_failed", "<output>", "atomic")]
    )
    persist_failure(store, report, cause)
    raise EpubPublishError(report, published=False, cause=cause) from cause


def _cleanup_temp(temp: str) -> None:
    if os.path.exists(temp):
        with suppress(OSError):
            os.unlink(temp)


def _persist_published(store: Any, report: dict[str, Any]) -> None:
    try:
        store.save_epub_verification(report)
        store.log_event_required(
            "epub_verification_passed",
            output=report["output_label"],
            assurance=report["assurance"],
            failure_count=0,
            warning_count=len(report["warnings"]),
            published=True,
        )
    except Exception as cause:
        raise EpubPublishError(report, published=True, cause=cause) from cause


def publish_epub(
    store: Any,
    source_path: str | os.PathLike[str] | None,
    final_path: str | os.PathLike[str],
    *,
    mode: str,
    bilingual: bool = False,
    bilingual_order: str = "target_first",
    writer: Callable[[str], object],
    source_identity_path: str | os.PathLike[str] | None = None,
) -> str:
    """Run the only EPUB publication path: owned temp, reopen, verify, replace."""
    final = os.fspath(final_path)
    identity = source_identity_path if source_identity_path is not None else source_path
    if identity is not None:
        source = os.fspath(identity)
        try:
            aliases = os.path.realpath(source) == os.path.realpath(final)
            if os.path.exists(source) and os.path.exists(final):
                aliases = aliases or os.path.samefile(source, final)
        except OSError:
            aliases = False
        if aliases:
            raise_preflight(store, final, source_path, mode, "input_output_alias", "rejected")
    parent = os.path.dirname(os.path.abspath(final)) or "."
    if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
        raise_preflight(store, final, source_path, mode, "parent_unwritable", "parent")
    target_lang = None
    if store is not None:
        try:
            target_lang = epub_language(store.load_manifest().get("target_lang"))
        except Exception:
            target_lang = None
    if os.path.lexists(final):
        try:
            if os.path.islink(final) or os.path.isdir(final):
                raise_preflight(store, final, source_path, mode, "final_not_regular", "rejected")
        except EpubPublishError:
            raise
        except OSError as error:
            raise_preflight(
                store,
                final,
                source_path,
                mode,
                "final_unreadable",
                "rejected",
                error,
            )
    fd, temp = tempfile.mkstemp(
        prefix=f".{verify.output_label(final)}.epub-verify-", suffix=".tmp", dir=parent
    )
    os.close(fd)
    try:
        report = prepare_publication(
            store,
            temp,
            source_path,
            final,
            mode=mode,
            bilingual=bilingual,
            target_lang=target_lang,
            bilingual_order=bilingual_order,
            writer=writer,
        )
        replacement_state = [False]
        try:
            _replace_and_fsync(temp, final, parent, report, replacement_state)
        except OSError as cause:
            _raise_publish_failure(store, report, replacement_state[0], cause)
        report["published"] = True
        _persist_published(store, report)
        return final
    finally:
        _cleanup_temp(temp)
