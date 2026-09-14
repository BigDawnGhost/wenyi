"""先验证全部 EPUB，再逐文件持久发布。"""

from __future__ import annotations

import errno
import os
import tempfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trans_novel.assemble.epub.metadata import epub_language
from trans_novel.assemble.epub.verification import (
    EpubPublishError,
    EpubVerificationError,
    ThemeError,
    ThemePlan,
    archive_model,
    verify,
)


@dataclass(frozen=True, slots=True)
class EpubOutput:
    final_path: str | os.PathLike[str]
    mode: str
    bilingual: bool
    writer: Callable[[str], object]


@dataclass(frozen=True, slots=True)
class _PreparedOutput:
    temp: str
    final: str
    bilingual: bool
    report: dict[str, Any]
    theme_plan: ThemePlan | None


def fsync_file(path: str) -> None:
    with open(path, "r+b") as stream:
        os.fsync(stream.fileno())


def is_unsupported_dir_fsync(error: OSError) -> bool:
    return error.errno in {
        value for value in (errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", -1)) if value
    } or (os.name == "nt" and error.errno == errno.EACCES)


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
    output_digest: str | None = None,
) -> tuple[dict[str, Any], ThemePlan | None]:
    theme_plan = None
    try:
        result = writer(temp)
        theme_plan = result if isinstance(result, ThemePlan) else None
        report = verify.verify_epub(
            temp,
            source_path=source_path,
            store=store,
            mode=mode,
            bilingual=bilingual,
            target_lang=target_lang,
            bilingual_order=bilingual_order,
            theme_plan=theme_plan,
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
            theme_plan=theme_plan,
        )
        report["output_label"] = verify.output_label(final)
        report["passed"] = False
        report["output_digest"] = output_digest
        failure = (
            archive_model.item("resources", cause.code, cause.resource or "<archive>", str(cause))
            if isinstance(cause, ThemeError)
            else archive_model.item(
                "publish", "writer_failed", "<output>", f"{type(cause).__name__}: {cause}"[:500]
            )
        )
        if isinstance(cause, ThemeError) and cause.node_id is not None:
            failure["node_id"] = str(cause.node_id)
        report["failures"] = archive_model.sort_items(report["failures"] + [failure])
        persist_failure(store, report, cause)
        raise EpubVerificationError(report, cause=cause) from cause
    report["output_digest"] = output_digest
    if not report["passed"]:
        report["published"] = False
        persist_failure(store, report)
        raise EpubVerificationError(report)
    report["published"] = False
    try:
        store.save_epub_verification(report)
    except Exception as cause:
        raise EpubPublishError(report, published=False, cause=cause) from cause
    return report, theme_plan


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


def _check_destination(
    store: Any,
    source_path: str | os.PathLike[str] | None,
    final: str,
    mode: str,
    source_identity_path: str | os.PathLike[str] | None,
) -> str:
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
    if os.path.lexists(final) and (os.path.islink(final) or not os.path.isfile(final)):
        raise_preflight(store, final, source_path, mode, "final_not_regular", "rejected")
    return parent


def _prepare_output(
    store: Any,
    source_path: str | os.PathLike[str] | None,
    output: EpubOutput,
    parent: str,
    target_lang: str | None,
    bilingual_order: str,
    output_digest: str | None,
) -> _PreparedOutput:
    final = os.fspath(output.final_path)
    fd, temp = tempfile.mkstemp(
        prefix=f".{verify.output_label(final)}.epub-verify-", suffix=".tmp", dir=parent
    )
    os.close(fd)
    try:
        report, plan = prepare_publication(
            store,
            temp,
            source_path,
            final,
            mode=output.mode,
            bilingual=output.bilingual,
            target_lang=target_lang,
            bilingual_order=bilingual_order,
            writer=output.writer,
            output_digest=output_digest,
        )
        return _PreparedOutput(temp, final, output.bilingual, report, plan)
    except BaseException:
        _cleanup_temp(temp)
        raise


def _record_triplet(
    store: Any,
    source_path: str | os.PathLike[str] | None,
    prepared: Sequence[_PreparedOutput],
    *,
    target_lang: str | None,
    bilingual_order: str,
) -> None:
    if source_path is None or len(prepared) != 2:
        return
    variants = {output.bilingual: output for output in prepared}
    if len(variants) != 2:
        return
    from trans_novel.assemble.epub.verification import validate_epub_triplet

    mono, bilingual = variants[False], variants[True]
    triplet = validate_epub_triplet(
        source_path,
        mono.temp,
        bilingual.temp,
        mono_theme_plan=mono.theme_plan,
        bilingual_theme_plan=bilingual.theme_plan,
        store=store,
        target_lang=target_lang,
        bilingual_order=bilingual_order,
    )
    for output in prepared:
        part = triplet["bilingual" if output.bilingual else "mono"]
        if output.theme_plan is not None and part.get("theme") is None:
            output.report["passed"] = False
            output.report["failures"] = archive_model.sort_items(
                output.report["failures"]
                + [archive_model.item("resources", "theme_verify", "<archive>", "invalid")]
            )
            persist_failure(store, output.report)
            raise EpubVerificationError(output.report)
        output.report["triplet"] = triplet


def publish_epubs(
    store: Any,
    source_path: str | os.PathLike[str] | None,
    outputs: Sequence[EpubOutput],
    *,
    bilingual_order: str = "target_first",
    source_identity_path: str | os.PathLike[str] | None = None,
    output_digest: str | None = None,
) -> list[str]:
    """所有临时输出验证通过后逐个替换；不承诺跨文件原子性。"""
    paths: set[str] = set()
    labels: set[str] = set()
    parents: list[str] = []
    for output in outputs:
        final = os.fspath(output.final_path)
        path, label = os.path.realpath(final), verify.output_label(final)
        if path in paths or label in labels:
            raise_preflight(store, final, source_path, output.mode, "duplicate_output", "rejected")
        paths.add(path)
        labels.add(label)
        parents.append(
            _check_destination(store, source_path, final, output.mode, source_identity_path)
        )
    target_lang = None
    if store is not None:
        try:
            target_lang = epub_language(store.load_manifest().get("target_lang"))
        except Exception:
            target_lang = None
    prepared: list[_PreparedOutput] = []
    try:
        for output, parent in zip(outputs, parents, strict=True):
            prepared.append(
                _prepare_output(
                    store,
                    source_path,
                    output,
                    parent,
                    target_lang,
                    bilingual_order,
                    output_digest,
                )
            )
        _record_triplet(
            store,
            source_path,
            prepared,
            target_lang=target_lang,
            bilingual_order=bilingual_order,
        )
        for output, parent in zip(prepared, parents, strict=True):
            replacement_state = [False]
            try:
                _replace_and_fsync(
                    output.temp, output.final, parent, output.report, replacement_state
                )
            except OSError as cause:
                _raise_publish_failure(store, output.report, replacement_state[0], cause)
            output.report["published"] = True
            _persist_published(store, output.report)
        return [output.final for output in prepared]
    finally:
        for output in prepared:
            _cleanup_temp(output.temp)


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
    output_digest: str | None = None,
) -> str:
    """单输出使用相同的暂存、独立验证与持久发布边界。"""
    return publish_epubs(
        store,
        source_path,
        [EpubOutput(final_path, mode, bilingual, writer)],
        bilingual_order=bilingual_order,
        source_identity_path=source_identity_path,
        output_digest=output_digest,
    )[0]
