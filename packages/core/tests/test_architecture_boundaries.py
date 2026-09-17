"""Lightweight architecture tests protecting the thin orchestrator facade.
Forbid direct agents/ingest/glossary/assemble/postprocess/llm imports, thread pools and
direct parsing/model/report/export calls. Require every extracted service to be assembled.
Forbid lower-layer imports of orchestrator and agent imports of pipeline; agents may use
top-level review models.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import unittest

TRANS_NOVEL_DIR = pathlib.Path(__file__).resolve().parent.parent / "wenyi_core"
CLI_DIR = pathlib.Path(__file__).resolve().parents[2] / "cli" / "wenyi_cli"
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
        (str(path.relative_to(AGENTS_DIR)), path.read_text(encoding="utf-8"))
        for path in sorted(AGENTS_DIR.rglob("*.py"))
    ]


def _imported_modules(path: pathlib.Path) -> set[str]:
    """Resolve absolute and relative imports, including imports in nested packages."""
    package = ".".join(("wenyi_core", *path.relative_to(TRANS_NOVEL_DIR).parts[:-1]))
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            imported.add(module)
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    return imported


class TestArchitectureBoundaries(unittest.TestCase):
    def test_command_modules_do_not_import_cli_entry_point(self):
        """Command helpers receive dependencies instead of importing application globals."""
        paths = list((CLI_DIR / "commands").rglob("*.py"))
        self.assertTrue(paths, "The command architecture check must inspect actual CLI modules")
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    self.assertFalse(
                        (node.level == 2 and module == "cli")
                        or module == "wenyi_cli.cli"
                        or (
                            node.level == 2
                            and not module
                            and any(alias.name == "cli" for alias in node.names)
                        ),
                        str(path),
                    )
                elif isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name == "wenyi_cli.cli" for alias in node.names))

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
        for path in PIPELINE_DIR.rglob("*.py"):
            if path.name == "orchestrator.py":
                continue
            name = str(path.relative_to(PIPELINE_DIR))
            tree = ast.parse(path.read_text(encoding="utf-8"))
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
            module = importlib.import_module(f"wenyi_core.pipeline.{module_name}")
            self.assertTrue(hasattr(module, class_name), f"{module_name}.{class_name} 缺失")
        finalization = importlib.import_module("wenyi_core.pipeline.finalization")
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
                if node.level == 0 and module.startswith("wenyi_core.pipeline"):
                    parts = module.split(".")
                    if len(parts) >= 3:
                        self.assertNotIn(
                            parts[2],
                            FORBIDDEN_PIPELINE_MODULES_FOR_AGENTS,
                            f"{filename} 不得反向依赖 {module}",
                        )

    def test_review_agents_use_contracts_instead_of_concrete_storage_or_evidence(self):
        """Agents cannot acquire book state or bypass the evidence-query boundary."""
        forbidden = (
            "wenyi_core.pipeline",
            "wenyi_core.review.run_store",
            "wenyi_core.review.evidence",
        )
        for path in AGENTS_DIR.rglob("*.py"):
            for module in _imported_modules(path):
                self.assertFalse(
                    any(module == name or module.startswith(name + ".") for name in forbidden),
                    f"{path.relative_to(TRANS_NOVEL_DIR)} imports {module}",
                )

    def test_review_conflicts_and_contracts_have_no_execution_dependencies(self):
        """Decision rules and ports do not import model calls or concrete Review I/O."""
        forbidden = (
            "wenyi_core.agents",
            "wenyi_core.llm",
            "wenyi_core.pipeline",
            "wenyi_core.review.run_store",
            "wenyi_core.review.evidence",
        )
        for name in ("conflicts", "contracts", "session", "autofix_models"):
            for module in _imported_modules(TRANS_NOVEL_DIR / "review" / f"{name}.py"):
                self.assertFalse(
                    any(module == item or module.startswith(item + ".") for item in forbidden),
                    f"{name} imports {module}",
                )

    def test_review_chunk_and_arbiter_depend_only_on_the_shared_action_protocol(self):
        """Neither caller may borrow the other's private validation or prompt logic."""
        for name, other in (("review_loop", "review_arbiter"), ("review_arbiter", "review_loop")):
            imported = _imported_modules(AGENTS_DIR / f"{name}.py")
            self.assertIn("wenyi_core.agents.review_actions", imported)
            self.assertFalse(
                any(module.startswith(f"wenyi_core.agents.{other}") for module in imported)
            )

    def test_review_package_exports_core_types(self):
        """The top-level review package exposes pure types without storage exports."""
        import importlib

        review = importlib.import_module("wenyi_core.review")
        names = {
            "SegmentRef",
            "ReviewOutcome",
            "ReviewLoopOutcome",
            "review_candidate_id",
        }
        self.assertEqual(set(review.__all__), names)
        for name in names:
            self.assertTrue(hasattr(review, name), f"wenyi_core.review.{name} 缺失")
        self.assertFalse(hasattr(review, "ReviewRunStore"))

    def test_review_models_have_no_application_dependencies(self):
        """Shared value types must not load agents, providers or persistent stores."""
        path = TRANS_NOVEL_DIR / "review" / "models.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                self.assertNotEqual((node.module or "").split(".")[0], "wenyi_core")
            elif isinstance(node, ast.Import):
                self.assertFalse(any(alias.name.startswith("wenyi_core") for alias in node.names))


if __name__ == "__main__":
    unittest.main()


def test_shared_markup_has_no_workflow_or_archive_dependencies():
    """The shared DOM layer depends only on markup and pure document models."""
    for path in (TRANS_NOVEL_DIR / "markup").rglob("*.py"):
        for module in _imported_modules(path):
            if not module.startswith("wenyi_core."):
                continue
            assert module.startswith(("wenyi_core.markup", "wenyi_core.ingest.models")), (
                path,
                module,
            )


def test_writers_do_not_import_reader_private_helpers():
    for path in (TRANS_NOVEL_DIR / "assemble").rglob("*.py"):
        for module in _imported_modules(path):
            assert ".epub_reader" not in module, (path, module)
            assert ".docx_reader._" not in module, (path, module)
            assert "pipeline.docx_styles" not in module, (path, module)


def test_autofix_publisher_has_no_candidate_or_model_dependencies():
    for module in _imported_modules(PIPELINE_DIR / "autofix_publish.py"):
        assert not module.startswith(("wenyi_core.agents", "wenyi_core.llm")), module
        assert "autofix_candidates" not in module
        assert "autofix_verification" not in module


def test_document_style_policy_is_independent_of_workflows():
    for path in (TRANS_NOVEL_DIR / "document_styles").rglob("*.py"):
        for module in _imported_modules(path):
            assert not module.startswith(("wenyi_core.pipeline", "wenyi_core.agents", "docx")), (
                path,
                module,
            )
