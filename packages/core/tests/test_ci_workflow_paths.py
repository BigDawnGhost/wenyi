"""Every `.py` path named in a GitHub workflow must exist in the repository.

The Desktop windows CI job once ran `pytest` against a test file that a
refactor had split away, and xdist collected zero tests from the missing path:
exit code 5, which the pwsh wrapper folded into a plain failure. Nothing else
in the pipeline catches a stale path argument, so this guard scans the
workflow sources directly and fails when a referenced Python file is gone.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = ROOT / ".github" / "workflows"

# Ordered cleanups applied to each line before token extraction:
# `${{ expr }}` Actions expressions, shell `${var}` / `$var` references, and
# bare runner environment prefixes such as `RUNNER_TEMP/base/...` all name
# paths outside the checkout, so they must not be checked against the repo.
EXPRESSION = re.compile(r"\$\{\{.*?\}\}")
SHELL_VAR = re.compile(r"\$\{[^}]*\}|\$\w+")
RUNNER_ENV_PREFIX = re.compile(r"\b[A-Z][A-Z0-9_]*(?=/)")
# A repo-relative .py path token. The lookbehind keeps a suffix match like the
# "foo.py" inside "barfoo.py" from matching twice.
PY_TOKEN = re.compile(r"(?<![\w./-])([A-Za-z0-9_][A-Za-z0-9_./-]*\.py)\b")


def _strip_comment(line: str) -> str:
    """Cut the line at the first YAML comment start."""
    for index, char in enumerate(line):
        if char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def _repo_path_tokens(line: str) -> list[str]:
    """Extract the repo-relative .py path tokens mentioned on one line."""
    cleaned = EXPRESSION.sub(" ", line)
    cleaned = SHELL_VAR.sub(" ", cleaned)
    cleaned = RUNNER_ENV_PREFIX.sub(" ", cleaned)
    return [match.group(1) for match in PY_TOKEN.finditer(cleaned)]


def _referenced_python_paths() -> list[tuple[str, str]]:
    """Collect (workflow file, .py path) pairs from every workflow source."""
    references: list[tuple[str, str]] = []
    for workflow in sorted(WORKFLOW_DIR.glob("*.y*ml")):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            for token in _repo_path_tokens(_strip_comment(line)):
                references.append((workflow.name, token))
    return references


def test_workflow_python_paths_exist() -> None:
    references = _referenced_python_paths()
    assert references, "scan found no .py references; the guard is not seeing the workflows"
    missing = [
        f"{workflow}: {path}" for workflow, path in references if not (ROOT / path).is_file()
    ]
    assert not missing, "workflows reference Python files that do not exist: " + ", ".join(missing)
