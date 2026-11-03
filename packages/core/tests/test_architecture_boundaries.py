"""Lightweight architecture tests protecting the thin orchestrator facade.
Forbid direct agents/ingest/glossary/assemble/postprocess/llm imports, thread pools and
direct parsing/model/export calls. Require every extracted service to be assembled.
Forbid lower-layer imports of orchestrator and agent imports of pipeline; agents may use
top-level review models. Platform adapters reach orchestration through injected ports,
never the facade class.

Run these checks with pytest; the module holds no unittest suite.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import pathlib
import sys
from functools import lru_cache

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TRANS_NOVEL_DIR = REPO_ROOT / "packages" / "core" / "wenyi_core"
CLI_DIR = REPO_ROOT / "packages" / "cli" / "wenyi_cli"
BACKEND_DIR = REPO_ROOT / "packages" / "backend" / "wenyi_backend"
API_DIR = REPO_ROOT / "apps" / "api" / "wenyi_api"
DESKTOP_BACKEND_DIR = REPO_ROOT / "apps" / "desktop" / "backend" / "wenyi_desktop"
PIPELINE_DIR = TRANS_NOVEL_DIR / "pipeline"
AGENTS_DIR = TRANS_NOVEL_DIR / "agents"
MODELS_DIR = TRANS_NOVEL_DIR / "models"

# Third-party packages a pure data model may use. Anything that opens a socket, a file or
# a database would make importing a model observable at run time, which defeats the point.
MODELS_ALLOWED_THIRD_PARTY = frozenset({"pydantic", "pydantic_core", "typing_extensions"})

# Standard-library roots that are data-only and therefore safe in a pure model module.
_STDLIB_DATA_MODULES = frozenset(
    {"__future__", "collections", "dataclasses", "enum", "json", "sqlite3", "typing", "typing_extensions"}
)

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

PIPELINE_MODULE = "wenyi_core.pipeline"
ORCHESTRATOR_MODULE = f"{PIPELINE_MODULE}.orchestrator"

# Production surfaces that must reach orchestration through ports. The CLI is absent on
# purpose: driving the orchestrator is its documented role. Test trees are skipped below.
ORCHESTRATOR_SCAN_ROOTS = (BACKEND_DIR, API_DIR, DESKTOP_BACKEND_DIR)

# The one module allowed to bind the concrete facade to the WorkflowRunner port. Every
# other platform module receives the port, so no legacy import can hide behind it.
ORCHESTRATOR_COMPOSITION_BOUNDARIES = ("packages/backend/wenyi_backend/execution/bindings.py",)

# Repository-relative path -> why the facade import is still accepted. Empty since B5
# replaced the operations.py import with the narrow execution port.
ORCHESTRATOR_IMPORT_EXEMPTIONS: dict[str, str] = {}

# Agent source path (relative to agents/) -> why a pipeline import is still accepted.
AGENT_PIPELINE_EXEMPTIONS: dict[str, str] = {}


@lru_cache(maxsize=None)
def _parsed(path: pathlib.Path) -> ast.Module:
    """Parse each scanned module once; the suites below never rewrite product sources."""
    return ast.parse(path.read_text(encoding="utf-8"))


def _module_source(name: str) -> str:
    return (PIPELINE_DIR / f"{name}.py").read_text(encoding="utf-8")


def _module_package(root: pathlib.Path, path: pathlib.Path) -> str:
    """Return the dotted package that owns a module file."""
    return ".".join((root.name, *path.relative_to(root).parts[:-1]))


def _imported_modules_from_source(source: str, package: str) -> set[str]:
    """Expand import statements into every absolute module name they can reach.

    Relative imports resolve against the owning package. Each alias is also appended to
    the enclosing module, so "from .. import agents" exposes both wenyi_core and
    wenyi_core.agents instead of only the parent package.
    """
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            imported.add(module)
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    return imported


def _imported_modules(path: pathlib.Path, root: pathlib.Path = TRANS_NOVEL_DIR) -> set[str]:
    """Return the absolute module names a file under root imports."""
    return _imported_modules_from_source(
        path.read_text(encoding="utf-8"), _module_package(root, path)
    )


def _matches(module: str, names: tuple[str, ...]) -> bool:
    """True when a module name is one of names or lives below it."""
    return any(module == name or module.startswith(f"{name}.") for name in names)


def _domain_families() -> tuple[str, ...]:
    return tuple(f"wenyi_core.{name}" for name in FORBIDDEN_TOP_LEVEL)


def _forbidden_orchestrator_imports(source: str) -> set[str]:
    """Return the imports of a source text that the orchestrator boundary rejects."""
    return {
        module
        for module in _imported_modules_from_source(source, PIPELINE_MODULE)
        if _matches(module, _domain_families())
    }


def _repo_relative(path: pathlib.Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _is_test_module(path: pathlib.Path, root: pathlib.Path) -> bool:
    """Tests may compose the facade directly; only production code is scanned."""
    return path.name.startswith("test_") or "tests" in path.relative_to(root).parts


def test_language_policy_has_no_runtime_or_storage_dependencies():
    paths = list((TRANS_NOVEL_DIR / "i18n" / "policy").rglob("*.py"))
    assert paths, "The policy boundary check must inspect actual modules"
    forbidden = ("pipeline", "agents", "glossary", "assemble", "storage", "llm")
    families = tuple(f"wenyi_core.{name}" for name in forbidden)
    for path in paths:
        for module in _imported_modules(path):
            assert not _matches(module, families), f"{path}: {module}"


def test_command_modules_do_not_import_cli_entry_point():
    """Command helpers receive dependencies instead of importing application globals."""
    paths = list((CLI_DIR / "commands").rglob("*.py"))
    assert paths, "The command architecture check must inspect actual CLI modules"
    for path in paths:
        for module in _imported_modules(path, CLI_DIR):
            assert not _matches(module, ("wenyi_cli.cli",)), f"{path}: {module}"


def test_orchestrator_has_no_domain_imports():
    """Allow the orchestrator to depend only on config and sibling pipeline services."""
    rejected = _forbidden_orchestrator_imports(_module_source("orchestrator"))
    assert not rejected, f"orchestrator.py 不得直接导入 {sorted(rejected)}"


def test_parser_flags_relative_domain_import():
    """A relative agents import resolves to the forbidden package and its member."""
    imported = _imported_modules_from_source("from ..agents import prompts", PIPELINE_MODULE)
    assert imported == {"wenyi_core.agents", "wenyi_core.agents.prompts"}
    assert _forbidden_orchestrator_imports("from ..agents import prompts") == {
        "wenyi_core.agents",
        "wenyi_core.agents.prompts",
    }


def test_parser_flags_absolute_domain_import():
    """An absolute import names the forbidden package through its alias."""
    assert _forbidden_orchestrator_imports("import wenyi_core.agents") == {"wenyi_core.agents"}


def test_parser_flags_package_relative_domain_import():
    """A module-less relative import must expand each alias, not only its parent package."""
    imported = _imported_modules_from_source("from .. import agents", PIPELINE_MODULE)
    assert imported == {"wenyi_core", "wenyi_core.agents"}
    assert _forbidden_orchestrator_imports("from .. import agents") == {"wenyi_core.agents"}


def test_parser_flags_aliased_package_relative_domain_import():
    """An aliased module-less relative import resolves exactly like the unaliased form."""
    assert _forbidden_orchestrator_imports("from .. import agents as A") == {"wenyi_core.agents"}


def test_orchestrator_has_no_thread_pool():
    """Thread pools belong to domain services, not the orchestrator."""
    source = _module_source("orchestrator")
    assert "concurrent.futures" not in source
    assert not any(
        "futures" in module for module in _imported_modules(PIPELINE_DIR / "orchestrator.py")
    )
    for node in ast.walk(ast.parse(source)):
        assert not (isinstance(node, ast.Name) and node.id == "ThreadPoolExecutor"), (
            "orchestrator.py 不得直接使用 ThreadPoolExecutor"
        )


def test_orchestrator_does_not_call_domain_functions():
    """Forbid direct parsing, model and export calls in the orchestrator."""
    source = _module_source("orchestrator")
    for forbidden in ("load_document(", "complete_json("):
        assert forbidden not in source
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "assemble"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("load_document", "complete_json")


def test_orchestrator_does_not_touch_glossary_store_directly():
    """Storage owns glossary lifetime; the orchestrator cannot reference it directly."""
    assert "GlossaryStore" not in _module_source("orchestrator")


def test_orchestrator_wires_all_services():
    """Require the orchestrator to assemble every extracted service."""
    source = _module_source("orchestrator")
    for name in SERVICE_MODULES:
        assert f"from .{name} import" in source, f"缺少 {name} 的装配"


def test_no_lower_module_imports_orchestrator():
    """Forbid reverse imports of orchestrator from lower layers."""
    for path in PIPELINE_DIR.rglob("*.py"):
        if path.name == "orchestrator.py":
            continue
        name = str(path.relative_to(PIPELINE_DIR))
        for module in _imported_modules(path):
            assert "orchestrator" not in module.split("."), (
                f"{name}.py 不得反向导入编排器: {module}"
            )


def test_runtime_uses_neutral_language_module():
    """Shared Runtime cannot depend on the preparation service."""
    imported = _imported_modules(PIPELINE_DIR / "runtime.py")
    assert "wenyi_core.i18n.languages" in imported
    assert "wenyi_core.pipeline.preparation" not in imported


def test_preparation_usage_and_file_migration_have_independent_owners():
    for name in ("book_understanding", "runtime_usage"):
        path = PIPELINE_DIR / f"{name}.py"
        assert path.is_file()
        imports = _imported_modules(path)
        assert "wenyi_core.pipeline.runtime" not in imports
        assert "wenyi_core.pipeline.preparation" not in imports
        assert "self._runtime" not in path.read_text(encoding="utf-8")
    runtime = _module_source("runtime")
    assert "UsageCoordinator(self.client)" in runtime
    assert "prepare_usage_commit(" not in runtime
    preparation = _module_source("preparation")
    assert "BookUnderstandingService(" in preparation
    assert "ThreadPoolExecutor" not in preparation
    command = (CLI_DIR / "model_commands.py").read_text(encoding="utf-8")
    assert "migrate_usage_ledgers(run_dir)" in command
    assert "_write_json" not in command
    assert "RunStore" not in command


def test_services_exist_as_pure_modules():
    """Extracted modules must import independently and expose their service classes."""
    classes = {
        "runtime": "PipelineRuntime",
        "preparation": "PreparationService",
        "annotations": "AnnotationService",
        "translation": "TranslationService",
        "review_workflow": "ReviewService",
        "review_autofix": "ReviewAutofixService",
        "finalization": "AssemblyService",
    }
    for module_name, class_name in classes.items():
        module = importlib.import_module(f"wenyi_core.pipeline.{module_name}")
        assert hasattr(module, class_name), f"{module_name}.{class_name} 缺失"


def test_agents_do_not_import_pipeline_orchestration():
    """Agents may use pure review models, never pipeline orchestration or state."""
    violations: dict[str, set[str]] = {}
    scanned = 0
    for path in sorted(AGENTS_DIR.rglob("*.py")):
        scanned += 1
        hits = {
            module for module in _imported_modules(path) if _matches(module, (PIPELINE_MODULE,))
        }
        if hits:
            violations[path.relative_to(AGENTS_DIR).as_posix()] = hits
    assert scanned, "The agent boundary check must inspect actual modules"
    unexpected = {
        name: hits for name, hits in violations.items() if name not in AGENT_PIPELINE_EXEMPTIONS
    }
    assert not unexpected, f"agents must not import {PIPELINE_MODULE}: {unexpected}"
    stale = sorted(set(AGENT_PIPELINE_EXEMPTIONS) - set(violations))
    assert not stale, f"Unused exemptions; delete them: {stale}"


def test_platform_layers_do_not_import_the_orchestrator():
    """Platform adapters receive orchestration through ports instead of the facade."""
    importers: dict[str, set[str]] = {}
    scanned = 0
    for root in ORCHESTRATOR_SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if _is_test_module(path, root):
                continue
            scanned += 1
            if _repo_relative(path) in ORCHESTRATOR_COMPOSITION_BOUNDARIES:
                continue
            hits = {
                module
                for module in _imported_modules(path, root)
                if _matches(module, (ORCHESTRATOR_MODULE,))
            }
            if hits:
                importers[_repo_relative(path)] = hits
    assert scanned, "The platform boundary check must inspect actual modules"
    unexpected = {
        path: hits for path, hits in importers.items() if path not in ORCHESTRATOR_IMPORT_EXEMPTIONS
    }
    assert not unexpected, f"Platform code must not import {ORCHESTRATOR_MODULE}: {unexpected}"
    stale = sorted(set(ORCHESTRATOR_IMPORT_EXEMPTIONS) - set(importers))
    assert not stale, f"Unused exemptions; delete them: {stale}"
    # A composition boundary that stopped importing the facade would silently stop
    # exercising the port, so the declaration has to stay true.
    for boundary in ORCHESTRATOR_COMPOSITION_BOUNDARIES:
        source = (REPO_ROOT / boundary).read_text(encoding="utf-8")
        assert ORCHESTRATOR_MODULE in source, f"{boundary} must bind {ORCHESTRATOR_MODULE}"


def test_glossary_never_imports_agents():
    """Agents format prompts and call models; glossary stores terms and matches text.

    The extractor used to live in `glossary`, which made this a two-way edge. It is now an
    agent, so `agents -> glossary` is the only direction and the store stays model-free.
    """
    scanned = 0
    for path in sorted((TRANS_NOVEL_DIR / "glossary").rglob("*.py")):
        scanned += 1
        for module in _imported_modules(path):
            assert not _matches(module, ("wenyi_core.agents",)), (
                f"{path.relative_to(TRANS_NOVEL_DIR)} imports {module}"
            )
    assert scanned, "The glossary boundary check must inspect actual modules"


def test_glossary_extractor_is_an_agent():
    """The extractor belongs with the model tasks, not with the term store."""
    assert not (TRANS_NOVEL_DIR / "glossary" / "extractor.py").exists()
    module = importlib.import_module("wenyi_core.agents.glossary_extractor")
    assert hasattr(module, "GlossaryExtractor")
    assert hasattr(module, "TranslatedSegmentEvidence")


def test_review_agents_use_contracts_instead_of_concrete_storage_or_evidence():
    """Agents cannot acquire book state or bypass the evidence-query boundary."""
    forbidden = (
        "wenyi_core.pipeline",
        "wenyi_core.review.run_store",
        "wenyi_core.review.evidence",
    )
    for path in AGENTS_DIR.rglob("*.py"):
        for module in _imported_modules(path):
            assert not _matches(module, forbidden), (
                f"{path.relative_to(TRANS_NOVEL_DIR)} imports {module}"
            )


def test_review_conflicts_and_contracts_have_no_execution_dependencies():
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
            assert not _matches(module, forbidden), f"{name} imports {module}"


def test_review_chunk_and_arbiter_depend_only_on_the_shared_action_protocol():
    """Neither caller may borrow the other private validation or prompt logic."""
    for name, other in (("review_loop", "review_arbiter"), ("review_arbiter", "review_loop")):
        imported = _imported_modules(AGENTS_DIR / f"{name}.py")
        assert "wenyi_core.agents.review_actions" in imported
        assert not any(_matches(module, (f"wenyi_core.agents.{other}",)) for module in imported)


def test_review_package_exports_core_types():
    """The top-level review package exposes pure types without storage exports."""
    review = importlib.import_module("wenyi_core.review")
    names = {
        "SegmentRef",
        "ReviewOutcome",
        "ReviewLoopOutcome",
        "review_candidate_id",
    }
    assert set(review.__all__) == names
    for name in names:
        assert hasattr(review, name), f"wenyi_core.review.{name} 缺失"
    assert not hasattr(review, "ReviewRunStore")


def test_review_models_have_no_application_dependencies():
    """Shared value types must not load agents, providers or persistent stores."""
    path = TRANS_NOVEL_DIR / "review" / "models.py"
    for node in ast.walk(_parsed(path)):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, "review models use absolute imports only"
    for module in _imported_modules(path):
        assert not _matches(module, ("wenyi_core",)), f"review models import {module}"


def test_shared_markup_has_no_workflow_or_archive_dependencies():
    """The shared DOM layer depends only on markup and the pure data models."""
    for path in (TRANS_NOVEL_DIR / "markup").rglob("*.py"):
        for module in _imported_modules(path):
            if not module.startswith("wenyi_core."):
                continue
            assert _matches(module, ("wenyi_core.markup", "wenyi_core.models")), (
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


def test_models_package_is_zero_dependency():
    """A model must be importable without another wenyi_core subpackage behind it.

    This is what lets markup, storage and pipeline share one Segment without importing each
    other; a single sibling import here would silently restore the cycle the split removed.
    `wenyi_core.errors` is the one allowed exception: it holds the product-wide base class
    and imports nothing, so it cannot reintroduce a cycle.
    """
    modules = sorted(MODELS_DIR.glob("*.py"))
    assert modules, "The models boundary check must inspect actual modules"
    for path in modules:
        for module in _imported_modules(path):
            if not module.startswith("wenyi_core"):
                continue
            assert _matches(module, ("wenyi_core.models", "wenyi_core.errors")), (
                f"{path.name} imports {module}"
            )


def test_models_import_only_data_libraries():
    """Every import that runs at import time must be data-only.

    A whitelist rather than a blacklist: a new dependency has to be justified in this test,
    so a model cannot quietly start opening sockets or files.
    """
    scanned = 0
    for path in sorted(MODELS_DIR.glob("*.py")):
        scanned += 1
        for node in ast.walk(_parsed(path)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if not root or not sys.stdlib_module_names.__contains__(root):
                    assert root in MODELS_ALLOWED_THIRD_PARTY, f"{path.name} imports {name}"
                else:
                    assert root in _STDLIB_DATA_MODULES, f"{path.name} imports {name}"
    assert scanned, "The models boundary check must inspect actual modules"


def test_models_package_exports_the_shared_records():
    """The records every layer names must be reachable from the package root."""
    models = importlib.import_module("wenyi_core.models")
    expected = {
        "Chapter",
        "Document",
        "GlossaryTerm",
        "IngestError",
        "Segment",
    }
    assert expected <= set(models.__all__), sorted(expected - set(models.__all__))
    for name in expected:
        assert hasattr(models, name), f"wenyi_core.models.{name} is missing"


def test_ingest_keeps_the_forwarding_paths():
    """Old import paths keep working, so the move stays reviewable as a pure relocation."""
    ingest_models = importlib.import_module("wenyi_core.ingest.models")
    ingest_errors = importlib.import_module("wenyi_core.ingest.errors")
    models = importlib.import_module("wenyi_core.models")
    for name in ("KIND_HEADING", "KIND_TEXT", "Chapter", "Document", "Segment"):
        assert getattr(ingest_models, name) is getattr(models, name), name
    for name in ("IngestError", "MinerUError", "MinerUTimeoutError"):
        assert getattr(ingest_errors, name) is getattr(models, name), name


def test_pdf_bridge_does_not_import_the_ingest_pipeline():
    """The bridge raises IngestError from models, so it never loads a parser."""
    for path in sorted((TRANS_NOVEL_DIR / "pdf_bridge").rglob("*.py")):
        for module in _imported_modules(path):
            assert not _matches(module, ("wenyi_core.ingest",)), f"{path.name} imports {module}"


STORAGE_DIR = TRANS_NOVEL_DIR / "storage"

# Undeclared state paths FileStorage exposes to tests and CLI presentation. Shrinking this
# set is the remaining work of D2-A-04; growing it is a new contract leak.
FILE_STORAGE_PATH_GAPS = {
    "chapter_path",
    "event_log_path",
    "glossary_path",
    "initialization_path",
    "manifest_path",
    "usage_path",
}


def test_storage_layer_never_imports_pipeline():
    """Storage owns the run-state primitives; it cannot reach up into workflow code."""
    scanned = 0
    for path in sorted(STORAGE_DIR.rglob("*.py")):
        scanned += 1
        for module in _imported_modules(path):
            assert not _matches(module, (PIPELINE_MODULE,)), (
                f"{path.relative_to(TRANS_NOVEL_DIR)} imports {module}"
            )
    assert scanned, "The storage boundary check must inspect actual modules"


def _declared_storage_names() -> set[str]:
    """Every name the Storage port spells out, including documented path gaps."""
    protocol = (STORAGE_DIR / "protocol.py").read_text(encoding="utf-8")
    return {
        node.name
        for node in ast.walk(ast.parse(protocol))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_file_storage_has_no_catch_all_attribute_forwarding():
    """A __getattr__ would satisfy any future typo with a RunStore method instead of failing."""
    source = (STORAGE_DIR / "file.py").read_text(encoding="utf-8")
    assert "__getattr__" not in source
    defined = {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    undeclared = sorted(
        name
        for name in defined - _declared_storage_names()
        if not name.startswith("_") and not name.isupper()
    )
    # State-file paths the CLI presents but the port does not declare yet. They are a
    # measured contract gap, not evidence that forwarding is acceptable.
    assert set(undeclared) <= FILE_STORAGE_PATH_GAPS, undeclared
    assert FILE_STORAGE_PATH_GAPS == {
        "chapter_path",
        "event_log_path",
        "glossary_path",
        "initialization_path",
        "manifest_path",
        "usage_path",
    }
