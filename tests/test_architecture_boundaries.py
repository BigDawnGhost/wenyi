"""轻量架构边界测试：防止 orchestrator.py 再次直接依赖领域实现。

校验：
  * orchestrator.py 不导入 agents / ingest / glossary / assemble / postprocess / llm；
  * 不使用 ThreadPoolExecutor / concurrent.futures；
  * 不直接调用 load_document / complete_json / build_report / assemble；
  * 装配全部六个拆分出的服务模块；
  * 任何下层模块都不得反向导入 orchestrator.py。
"""

from __future__ import annotations

import ast
import pathlib
import unittest

PIPELINE_DIR = pathlib.Path(__file__).resolve().parent.parent / "trans_novel" / "pipeline"

SERVICE_MODULES = (
    "runtime",
    "preparation",
    "annotations",
    "translation",
    "review_workflow",
    "finalization",
)

# 不得反向导入 orchestrator 的下层模块（含 language 这类共享工具）。
LOWER_MODULES = SERVICE_MODULES + ("language",)

FORBIDDEN_TOP_LEVEL = (
    "agents",
    "ingest",
    "glossary",
    "assemble",
    "postprocess",
    "llm",
)


def _module_source(name: str) -> str:
    return (PIPELINE_DIR / f"{name}.py").read_text(encoding="utf-8")


class TestArchitectureBoundaries(unittest.TestCase):
    def test_orchestrator_has_no_domain_imports(self):
        """编排器只允许依赖 config 与同级流水线服务模块。"""
        source = _module_source("orchestrator")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != 2:
                continue
            parts = (node.module or "").split(".")
            self.assertNotIn(
                parts[0],
                FORBIDDEN_TOP_LEVEL,
                f"orchestrator.py 不得直接导入 ..{parts[0]}",
            )

    def test_orchestrator_has_no_thread_pool(self):
        """线程池属于各领域服务，编排器不得直接使用。"""
        source = _module_source("orchestrator")
        self.assertNotIn("concurrent.futures", source)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "ThreadPoolExecutor":
                self.fail("orchestrator.py 不得直接使用 ThreadPoolExecutor")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    self.assertNotIn("futures", alias.name)

    def test_orchestrator_does_not_call_domain_functions(self):
        """编排器不得直接调用解析、LLM、报告或导出实现。"""
        source = _module_source("orchestrator")
        for forbidden in ("load_document(", "complete_json(", "build_report("):
            self.assertNotIn(forbidden, source)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual(node.func.id, "assemble")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, ("load_document", "complete_json", "build_report"))

    def test_orchestrator_does_not_touch_glossary_store_directly(self):
        """术语库生命周期归 ReportService / ReviewService，编排器不得直接引用。"""
        source = _module_source("orchestrator")
        self.assertNotIn("GlossaryStore", source)

    def test_orchestrator_wires_all_services(self):
        """编排器必须装配全部六个拆分出的服务模块。"""
        source = _module_source("orchestrator")
        for name in SERVICE_MODULES:
            self.assertIn(f"from .{name} import", source, f"缺少 {name} 的装配")

    def test_no_lower_module_imports_orchestrator(self):
        """依赖方向固定：任何下层模块都不得反向导入 orchestrator.py。"""
        for name in LOWER_MODULES:
            tree = ast.parse(_module_source(name))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(
                            "orchestrator",
                            alias.name.split("."),
                            f"{name}.py 不得反向导入编排器",
                        )
                if isinstance(node, ast.ImportFrom):
                    module_parts = (node.module or "").split(".")
                    self.assertNotIn(
                        "orchestrator",
                        module_parts,
                        f"{name}.py 不得反向导入编排器",
                    )
                    for alias in node.names:
                        self.assertNotEqual(
                            alias.name,
                            "orchestrator",
                            f"{name}.py 不得反向导入编排器",
                        )

    def test_runtime_uses_neutral_language_module(self):
        """共享 Runtime 不得反向依赖具体准备阶段。"""
        tree = ast.parse(_module_source("runtime"))
        relative_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 1
        }
        self.assertIn("language", relative_imports)
        self.assertNotIn("preparation", relative_imports)

    def test_services_exist_as_pure_modules(self):
        """拆分出的模块可独立导入，且提供对应服务类。"""
        import importlib

        classes = {
            "runtime": "PipelineRuntime",
            "preparation": "PreparationService",
            "annotations": "AnnotationService",
            "translation": "TranslationService",
            "review_workflow": "ReviewService",
            "finalization": "ReportService",
        }
        for module_name, class_name in classes.items():
            module = importlib.import_module(f"trans_novel.pipeline.{module_name}")
            self.assertTrue(hasattr(module, class_name), f"{module_name}.{class_name} 缺失")
        finalization = importlib.import_module("trans_novel.pipeline.finalization")
        self.assertTrue(hasattr(finalization, "AssemblyService"))


if __name__ == "__main__":
    unittest.main()
