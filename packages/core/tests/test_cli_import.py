"""The CLI must stay cheap to import: --help pays for no ingest or LLM stack."""

from __future__ import annotations

import subprocess
import sys

# Modules no `wenyi --help` needs. Listing them keeps the startup cost from
# creeping back: each one pulls a parser or an HTTP client behind it.
HEAVY = (
    "bs4",
    "lxml",
    "soupsieve",
    "ebooklib",
    "openai",
    "wenyi_core.ingest",
)


def test_importing_the_cli_app_pulls_no_heavy_stack():
    """A subprocess keeps the running test session's own imports out of the check."""
    probe = (
        "import sys, wenyi_cli.cli; "
        f"heavy = set({HEAVY!r}) & set(sys.modules); "
        "print(sorted(heavy)); "
        "sys.exit(1 if heavy else 0)"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, f"heavy modules imported at CLI startup: {result.stdout}"
