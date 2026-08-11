"""应用服务包的依赖方向与公共导出合同。"""

from __future__ import annotations

import inspect
import subprocess
import sys

import trans_novel.services as services


def test_services_import_without_runtime_or_concrete_storage_dependencies() -> None:
    """干净解释器导入纯服务时不得加载旧编排器、存储实现或 LangGraph。"""
    script = """
import sys
import trans_novel.services

forbidden = (
    "trans_novel.cli",
    "trans_novel.pipeline.orchestrator",
    "trans_novel.pipeline.runstore",
    "trans_novel.storage",
    "trans_novel.storage.sqlite_workflows",
    "langgraph",
)
loaded = [name for name in forbidden if name in sys.modules]
if loaded:
    raise SystemExit(f"unexpected service dependencies: {loaded}")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_services_public_exports_are_explicit_and_documented() -> None:
    """首个服务块只公开稳定 DTO、最小端口和两个规划函数。"""
    expected = {
        "TranslationBatchPlan",
        "TranslationSegmentView",
        "plan_contiguous_batches",
        "plan_resumable_batches",
    }

    assert set(services.__all__) == expected
    for name in expected:
        assert inspect.getdoc(getattr(services, name))
