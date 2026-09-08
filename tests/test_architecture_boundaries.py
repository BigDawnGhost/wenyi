"""Lightweight architecture tests protecting the thin orchestrator facade.
Forbid direct agents/ingest/glossary/assemble/postprocess/llm imports, thread pools and
direct parsing/model/report/export calls. Require every extracted service to be assembled.
Forbid lower-layer imports of orchestrator and agent imports of pipeline; agents may use
top-level review models.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

TRANS_NOVEL_DIR = pathlib.Path(__file__).resolve().parent.parent / "trans_novel"
PIPELINE_DIR = TRANS_NOVEL_DIR / "pipeline"
AGENTS_DIR = TRANS_NOVEL_DIR / "agents"

SERVICE_MODULES = (
    "runtime",
    "preparation",
    "annotations",
    "translation",
    "review_workflow",
    "review_autofix",
    "finalization",
)

# Lower pipeline modules must not import orchestrator.
LOWER_MODULES = SERVICE_MODULES + ("runstore", "context")

FORBIDDEN_TOP_LEVEL = (
    "agents",
    "ingest",
    "glossary",
    "assemble",
    "postprocess",
    "llm",
)

# Agents cannot import pipeline orchestration/state machines; pure review models live at top level.
FORBIDDEN_PIPELINE_MODULES_FOR_AGENTS = (
    "orchestrator",
    "runtime",
    "preparation",
    "annotations",
    "translation",
    "review_workflow",
    "review_autofix",
    "finalization",
    "runstore",
    "context",
)


def _module_source(name: str) -> str:
    return (PIPELINE_DIR / f"{name}.py").read_text(encoding="utf-8")


def _agent_sources() -> list[tuple[str, str]]:
    return [
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted(AGENTS_DIR.glob("*.py"))
        if path.name != "__init__.py"
    ]


class TestArchitectureBoundaries(unittest.TestCase):
    def test_orchestrator_has_no_domain_imports(self):
        """Allow the orchestrator to depend only on config and sibling pipeline services."""
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
        """Thread pools belong to domain services, not the orchestrator."""
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
        """Forbid direct parsing, model, report and export calls in the orchestrator."""
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
        """Report/Review services own glossary lifetime; the orchestrator cannot reference it
        directly.
        """
        source = _module_source("orchestrator")
        self.assertNotIn("GlossaryStore", source)

    def test_orchestrator_wires_all_services(self):
        """Require the orchestrator to assemble every extracted service."""
        source = _module_source("orchestrator")
        for name in SERVICE_MODULES:
            self.assertIn(f"from .{name} import", source, f"缺少 {name} 的装配")

    def test_no_lower_module_imports_orchestrator(self):
        """Forbid reverse imports of orchestrator from lower layers."""
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
        """Shared Runtime cannot depend on the preparation service."""
        tree = ast.parse(_module_source("runtime"))
        relative_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level > 0
        }
        self.assertIn("i18n.languages", relative_imports)
        self.assertNotIn("preparation", relative_imports)

    def test_services_exist_as_pure_modules(self):
        """Extracted modules must import independently and expose their service classes."""
        import importlib

        classes = {
            "runtime": "PipelineRuntime",
            "preparation": "PreparationService",
            "annotations": "AnnotationService",
            "translation": "TranslationService",
            "review_workflow": "ReviewService",
            "review_autofix": "ReviewAutofixService",
            "finalization": "ReportService",
        }
        for module_name, class_name in classes.items():
            module = importlib.import_module(f"trans_novel.pipeline.{module_name}")
            self.assertTrue(hasattr(module, class_name), f"{module_name}.{class_name} 缺失")
        finalization = importlib.import_module("trans_novel.pipeline.finalization")
        self.assertTrue(hasattr(finalization, "AssemblyService"))

    def test_agents_do_not_import_pipeline_orchestration(self):
        """Agents may use pure top-level review models, never pipeline orchestration."""
        for filename, source in _agent_sources():
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                module = node.module or ""
                parts = module.split(".")
                if node.level == 2 and parts and parts[0] == "pipeline":
                    rest = parts[1:] if len(parts) > 1 else []
                    if not rest:
                        self.fail(f"{filename} 不得 import ..pipeline")
                    self.assertNotIn(
                        rest[0],
                        FORBIDDEN_PIPELINE_MODULES_FOR_AGENTS,
                        f"{filename} 不得反向依赖 pipeline.{rest[0]}",
                    )
                if node.level == 0 and module.startswith("trans_novel.pipeline"):
                    parts = module.split(".")
                    if len(parts) >= 3:
                        self.assertNotIn(
                            parts[2],
                            FORBIDDEN_PIPELINE_MODULES_FOR_AGENTS,
                            f"{filename} 不得反向依赖 {module}",
                        )

    def test_review_package_exports_core_types(self):
        """The top-level review package provides evidence and run-storage models."""
        import importlib

        review = importlib.import_module("trans_novel.review")
        for name in (
            "BookEvidenceIndex",
            "SegmentRef",
            "ReviewRunStore",
            "ReviewOutcome",
            "review_candidate_id",
        ):
            self.assertTrue(hasattr(review, name), f"trans_novel.review.{name} 缺失")


if __name__ == "__main__":
    unittest.main()
