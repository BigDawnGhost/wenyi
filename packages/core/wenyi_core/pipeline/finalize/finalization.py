"""Assembly finalization service.
AssemblyService exports monolingual/bilingual products from live state or immutable
snapshots and forwards format options.
Standalone assembly avoids the long run lock: capture a snapshot under the assembly/state
locks, release the short state lock before rendering, and validate source hashes before and
after. Full-workflow assembly uses live state under its existing run lock plus the assembly
lock to serialize output writers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...events import ProgressFn
from ...export_paths import NumberedOutput, bilingual_out_path
from ...models.errors import ExportPartialError, ExportProductOutcome
from ...storage.protocol import Storage
from ...storage.run_store import source_sha256
from ..run.language_policies import persist_plan

if TYPE_CHECKING:
    from ..run.runtime import PipelineRuntime


class AssemblyService:
    """Domain service for live-state and read-only snapshot exports."""

    def __init__(self, runtime: PipelineRuntime):
        self._runtime = runtime

    def assemble_outputs(
        self,
        store: Storage,
        *,
        input_path: str,
        progress: ProgressFn | None,
        out_format: str,
        out_path: str | NumberedOutput | None,
        policy_store: Storage,
    ) -> list[str]:
        """Generate every configured artifact from live state or a read-only snapshot.

        Every configured product is attempted even when an earlier one fails; when any
        product fails, ``ExportPartialError`` carries the per-product manifest so
        callers can report which outputs succeeded and which failed.
        """
        from ...assemble.policy import export_plan
        from ...assemble.writer import assemble, validate_output_path

        if progress:
            progress(0, 0, "progress.stage.assembling")
        out_cfg = self._runtime.config.output
        do_mono, do_bilingual = out_cfg.mono, out_cfg.bilingual
        if not do_mono and not do_bilingual:
            do_mono = True
        plans = {}
        for bilingual in ([False] if do_mono else []) + ([True] if do_bilingual else []):
            plans[bilingual] = export_plan(
                store,
                out_format,
                bilingual=bilingual,
                order=out_cfg.bilingual_order if bilingual else "target_first",
                preserve_source_style=out_cfg.bilingual_preserve_source_style
                if bilingual
                else False,
                about_page=out_cfg.about_page,
            )
        for plan in plans.values():
            persist_plan(policy_store, plan)

        products: list[tuple[bool, str | None]] = []
        base = out_path.path if isinstance(out_path, NumberedOutput) else out_path
        if do_mono:
            products.append((False, base))
        if do_bilingual:
            products.append((True, bilingual_out_path(base) if base else None))
        outputs: list[str] = []
        outcomes: list[ExportProductOutcome] = []
        first_error: Exception | None = None
        for bilingual, product_out_path in products:
            label = "bilingual" if bilingual else "monolingual"

            def render(destination: str | None) -> str:
                return assemble(
                    store,
                    input_path,
                    out_path=destination,
                    out_format=out_format,
                    bilingual=bilingual,
                    order=out_cfg.bilingual_order if bilingual else "target_first",
                    preserve_source_style=out_cfg.bilingual_preserve_source_style
                    if bilingual
                    else False,
                    about_page=out_cfg.about_page,
                    babeldoc_timeout=self._runtime.config.pipeline.babeldoc_timeout,
                    language_policy=plans[bilingual],
                )

            try:
                if isinstance(out_path, NumberedOutput) and product_out_path is not None:
                    validate_output_path(store, store.load_manifest(), input_path, product_out_path)
                    path = out_path.publish(
                        render,
                        bilingual=bilingual,
                        include_bilingual=do_bilingual,
                    )
                else:
                    path = render(product_out_path)
            except Exception as error:
                outcomes.append(
                    ExportProductOutcome(
                        label, out_format, error=str(error) or type(error).__name__
                    )
                )
                if first_error is None:
                    first_error = error
                continue
            outputs.append(path)
            outcomes.append(ExportProductOutcome(label, out_format, output=path))
        if first_error is not None:
            raise ExportPartialError(outcomes) from first_error
        return outputs

    def assemble_live(
        self,
        store: Storage,
        *,
        input_path: str,
        progress: ProgressFn | None,
        out_format: str | None,
        out_path: str | NumberedOutput | None,
    ) -> list[str]:
        """Export under the book run lock, adding the assembly lock to serialize output
        writers.
        """
        from ...assemble.writer_common import default_output_format

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
                policy_store=store,
            )
            self._runtime.ensure_store_source(store, input_path)
        self._runtime.log_event(store, "assembled", outputs=outputs, out_format=out_format)
        return outputs

    def assemble_snapshot(
        self,
        store: Storage,
        *,
        input_path: str,
        progress: ProgressFn | None,
        out_format: str | None,
        out_path: str | NumberedOutput | None,
    ) -> list[str]:
        """Capture an immutable snapshot under the assembly lock and validate source hashes
        around rendering.
        """
        from ...assemble.writer_common import default_output_format

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
                policy_store=store,
            )
            # The source template is an export input; validate afterward so mid-render replacement cannot succeed.
            self._runtime.ensure_store_source(store, input_path)
        self._runtime.log_event(store, "assembled", outputs=outputs, out_format=out_format)
        return outputs
