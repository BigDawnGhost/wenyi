"""Report and assembly finalization services.
ReportService owns glossary lifetime, build_report, report.json and related events.
AssemblyService exports monolingual/bilingual products from live state or immutable
snapshots and forwards format options.
Standalone assembly avoids the long run lock: capture a snapshot under the assembly/state
locks, release the short state lock before rendering, and validate source hashes before and
after. Full-workflow assembly uses live state under its existing run lock plus the assembly
lock to serialize output writers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from ..glossary.store import GlossaryStore
from .runstore import source_sha256

if TYPE_CHECKING:
    from .runstore import RunStore
    from .runtime import PipelineRuntime

ProgressFn = Callable[[int, int, str], None]


class ReportService:
    """Domain service for glossary lifetime and report generation."""

    def __init__(self, runtime: PipelineRuntime):
        self._runtime = runtime

    @contextmanager
    def glossary_scope(self, store: RunStore, needed: bool) -> Iterator[GlossaryStore | None]:
        """Open the glossary for finalization and guarantee closure in finally."""
        glossary = GlossaryStore(store.glossary_path) if needed else None
        try:
            yield glossary
        finally:
            if glossary is not None:
                glossary.close()

    def build_and_save(
        self,
        store: RunStore,
        glossary: GlossaryStore,
        *,
        progress: ProgressFn | None = None,
    ) -> dict[str, Any]:
        """Generate and persist report.json and record the corresponding event."""
        from ..assemble.report import build_report

        if progress:
            progress(0, 0, "Generating report…")
        report = build_report(store, glossary)
        assert report is not None
        store.save_report(report)
        store.log_event("report_saved", path=store.report_path)
        return report


class AssemblyService:
    """Domain service for live-state and read-only snapshot exports."""

    def __init__(self, runtime: PipelineRuntime):
        self._runtime = runtime

    def assemble_outputs(
        self,
        store: RunStore,
        *,
        input_path: str,
        progress: ProgressFn | None,
        out_format: str,
        out_path: str | None,
        pdf_engine: str,
    ) -> list[str]:
        """Generate every configured artifact from live state or a read-only snapshot."""
        from ..assemble.writer import assemble
        from ..assemble.writer_common import bilingual_out_path

        if progress:
            progress(0, 0, "Assembling translation…")
        out_cfg = self._runtime.config.output
        do_mono, do_bilingual = out_cfg.mono, out_cfg.bilingual
        if not do_mono and not do_bilingual:
            do_mono = True

        outputs: list[str] = []
        if do_mono:
            outputs.append(
                assemble(
                    store,
                    input_path,
                    out_path=out_path,
                    out_format=out_format,
                    bilingual=False,
                    about_page=out_cfg.about_page,
                    pdf_engine=pdf_engine,
                    babeldoc_timeout=self._runtime.config.pipeline.babeldoc_timeout,
                    punctuation_normalize=self._runtime.export_punctuation_enabled(),
                )
            )
        if do_bilingual:
            bi_out_path = bilingual_out_path(out_path) if out_path else None
            outputs.append(
                assemble(
                    store,
                    input_path,
                    out_path=bi_out_path,
                    out_format=out_format,
                    bilingual=True,
                    order=out_cfg.bilingual_order,
                    preserve_source_style=out_cfg.bilingual_preserve_source_style,
                    about_page=out_cfg.about_page,
                    pdf_engine=pdf_engine,
                    babeldoc_timeout=self._runtime.config.pipeline.babeldoc_timeout,
                    punctuation_normalize=self._runtime.export_punctuation_enabled(),
                )
            )
        return outputs

    def assemble_live(
        self,
        store: RunStore,
        *,
        input_path: str,
        progress: ProgressFn | None,
        out_format: str | None,
        out_path: str | None,
        pdf_engine: str,
    ) -> list[str]:
        """Export under the book run lock, adding the assembly lock to serialize output
        writers.
        """
        from ..assemble.writer_common import default_output_format

        with store.assemble_lock():
            # Export rereads the source template; validate before and after to detect replacement during the run.
            self._runtime.ensure_store_source(store, input_path)
            if out_format is None:
                out_format = default_output_format(store.load_manifest())
            outputs = self.assemble_outputs(
                store,
                input_path=input_path,
                progress=progress,
                out_format=out_format,
                out_path=out_path,
                pdf_engine=pdf_engine,
            )
            self._runtime.ensure_store_source(store, input_path)
        self._runtime.log_event(store, "assembled", outputs=outputs, out_format=out_format)
        return outputs

    def assemble_snapshot(
        self,
        store: RunStore,
        *,
        input_path: str,
        progress: ProgressFn | None,
        out_format: str | None,
        out_path: str | None,
        pdf_engine: str,
    ) -> list[str]:
        """Capture an immutable snapshot under the assembly lock and validate source hashes
        around rendering.
        """
        from ..assemble.writer_common import default_output_format

        with store.assemble_lock():
            snapshot = store.create_export_snapshot(actual_sha256=source_sha256(input_path))
            self._runtime.apply_manifest_languages(snapshot.load_manifest())
            if out_format is None:
                out_format = default_output_format(snapshot.load_manifest())

            # The source may change while waiting for another export; validate again immediately before rendering.
            self._runtime.ensure_store_source(store, input_path)
            outputs = self.assemble_outputs(
                snapshot,
                input_path=input_path,
                progress=progress,
                out_format=out_format,
                out_path=out_path,
                pdf_engine=pdf_engine,
            )
            # The source template is an export input; validate afterward so mid-render replacement cannot succeed.
            self._runtime.ensure_store_source(store, input_path)
        self._runtime.log_event(store, "assembled", outputs=outputs, out_format=out_format)
        return outputs
