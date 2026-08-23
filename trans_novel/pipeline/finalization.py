"""收尾服务：ReportService（术语库生命周期、报告生成）与 AssemblyService（成品导出）。

ReportService 负责术语库生命周期、build_report、report.json 和对应事件。
AssemblyService 提供“实时状态导出”和“只读快照导出”两个内部入口，负责
mono/bilingual 输出和所有格式参数传递。

独立 assemble 仍不获取长时间 run lock：在 assemble lock 下创建不可变 export
snapshot，释放短 state lock 后渲染，并在渲染前后验证源文件哈希。全流程中的
assemble 继续使用当前 run lock 内的实时状态，同时叠加 assemble lock，避免
多个导出写者互相覆盖。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from ..glossary.store import GlossaryStore

if TYPE_CHECKING:
    from .runstore import RunStore
    from .runtime import PipelineRuntime

ProgressFn = Callable[[int, int, str], None]


class ReportService:
    """术语库生命周期与报告生成的领域服务。"""

    def __init__(self, runtime: PipelineRuntime):
        self._runtime = runtime

    @contextmanager
    def glossary_scope(self, store: RunStore, needed: bool) -> Iterator[GlossaryStore | None]:
        """为收尾步骤打开术语库，并保证在 finally 中关闭。"""
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
        """生成报告并落盘 report.json，记录对应事件。"""
        from ..assemble.report import build_report

        if progress:
            progress(0, 0, "生成报告…")
        report = self._runtime.measure_stage_call(
            "report",
            build_report,
            store,
            glossary,
        )
        assert report is not None
        store.save_report(report)
        store.log_event("report_saved", path=store.report_path)
        return report


class AssemblyService:
    """实时状态导出与只读快照导出的领域服务。"""

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
        """从给定实时状态或只读快照生成配置要求的全部产物。"""
        from ..assemble.writer import assemble, bilingual_out_path

        if progress:
            progress(0, 0, "回填译文…")
        out_cfg = self._runtime.config.output
        do_mono, do_bilingual = out_cfg.mono, out_cfg.bilingual
        if not do_mono and not do_bilingual:
            do_mono = True

        outputs: list[str] = []
        if do_mono:
            outputs.append(
                self._runtime.measure_stage_call(
                    "assemble",
                    assemble,
                    store,
                    input_path,
                    out_path=out_path,
                    out_format=out_format,
                    bilingual=False,
                    about_page=out_cfg.about_page,
                    pdf_engine=pdf_engine,
                )
            )
        if do_bilingual:
            bi_out_path = bilingual_out_path(out_path) if out_path else None
            outputs.append(
                self._runtime.measure_stage_call(
                    "assemble",
                    assemble,
                    store,
                    input_path,
                    out_path=bi_out_path,
                    out_format=out_format,
                    bilingual=True,
                    order=out_cfg.bilingual_order,
                    preserve_source_style=out_cfg.bilingual_preserve_source_style,
                    about_page=out_cfg.about_page,
                    pdf_engine=pdf_engine,
                )
            )
        return outputs

    def assemble_live(
        self,
        store: RunStore,
        *,
        input_path: str,
        progress: ProgressFn | None,
        out_format: str,
        out_path: str | None,
        pdf_engine: str,
    ) -> list[str]:
        """在书级锁内的实时状态上导出，叠加 assemble lock 串行化导出写者。"""
        with store.assemble_lock():
            # 导出会重新读取源书模板；在读取前后都验证，避免运行期间替换文件。
            self._runtime.ensure_store_source(store, input_path)
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
        out_format: str,
        out_path: str | None,
        pdf_engine: str,
    ) -> list[str]:
        """不等待翻译锁：在 assemble lock 下创建不可变快照，渲染前后验证源文件哈希。"""
        with store.assemble_lock():
            snapshot = self._runtime.measure_stage_call(
                "prepare",
                store.create_export_snapshot,
                actual_sha256=self._runtime.source_sha256(input_path),
            )
            self._runtime.apply_manifest_languages(snapshot.load_manifest())
            self._runtime.capture_metrics_state(snapshot)
            # 等待另一个导出期间源文件也可能变化，因此真正渲染前再次确认。
            self._runtime.ensure_store_source(store, input_path)
            outputs = self.assemble_outputs(
                snapshot,
                input_path=input_path,
                progress=progress,
                out_format=out_format,
                out_path=out_path,
                pdf_engine=pdf_engine,
            )
            # 原模板也属于导出输入；渲染后再次核验，避免把中途替换的源文件记为成功。
            self._runtime.ensure_store_source(store, input_path)
        self._runtime.log_event(store, "assembled", outputs=outputs, out_format=out_format)
        return outputs
