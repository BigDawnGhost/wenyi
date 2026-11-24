"""CLI ``--out`` collision avoidance: existing exports are kept unless --overwrite."""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\].*?\x07|\r")


def plain_text(value: str) -> str:
    """Strip ANSI/control sequences so CLI assertions stay stable under Rich."""
    return _ANSI_RE.sub("", value)


class FakeStore:
    run_dir = "state/book"

    def lock(self):
        return nullcontext()

    def state_lock(self):
        return nullcontext()

    def load_manifest(self):
        return {"chapters": [{"index": 0, "status": "done"}]}

    def stats(self):
        return {"terms": 0}

    def load_usage(self):
        return None


class OutCollisionTests(unittest.TestCase):
    """Explicit ``--out`` targets that exist move to a numbered name unless --overwrite."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name

    def _invoke_assemble(self, args):
        cfg = Config.from_dict({"llm": {"preset": "fake"}})
        captured = {}

        class FakeOrchestrator:
            def __init__(self, config, client=None):
                del config
                self.client = FakeClient()
                del client

            def run_assemble(self, input_path, **kwargs):
                del input_path
                captured["out_path"] = kwargs.get("out_path")
                return {"store": FakeStore(), "outputs": [kwargs.get("out_path") or "out.epub"]}

        with (
            patch("wenyi_cli.commands.context.CommandContext.load_config", return_value=cfg),
            patch("wenyi_core.pipeline.orchestrator.Orchestrator", FakeOrchestrator),
            patch("wenyi_cli.commands.validation.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(app, ["assemble", "input.epub", *args])
        return result, captured

    def test_existing_out_moves_to_numbered_alternative(self):
        target = os.path.join(self.tmp, "book.epub")
        with open(target, "wb") as handle:
            handle.write(b"original export")
        result, captured = self._invoke_assemble(["--out", target])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured["out_path"], os.path.join(self.tmp, "book (1).epub"))
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"original export")
        text = plain_text(result.output)
        self.assertIn("Output exists", text)
        self.assertIn("book (1).epub", text)

    def test_numbering_skips_already_taken_names(self):
        for name in ("book.epub", "book (1).epub"):
            with open(os.path.join(self.tmp, name), "wb") as handle:
                handle.write(b"keep")
        result, captured = self._invoke_assemble(["--out", os.path.join(self.tmp, "book.epub")])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured["out_path"], os.path.join(self.tmp, "book (2).epub"))

    def test_overwrite_flag_restores_replacement(self):
        target = os.path.join(self.tmp, "book.epub")
        with open(target, "wb") as handle:
            handle.write(b"original export")
        result, captured = self._invoke_assemble(["--out", target, "--overwrite"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured["out_path"], target)
        self.assertNotIn("Output exists", plain_text(result.output))

    def test_fresh_out_path_is_unchanged(self):
        target = os.path.join(self.tmp, "book.epub")
        result, captured = self._invoke_assemble(["--out", target])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured["out_path"], target)
        self.assertNotIn("Output exists", plain_text(result.output))

    def test_bilingual_companion_collision_is_avoided(self):
        with open(os.path.join(self.tmp, "book-bi.epub"), "wb") as handle:
            handle.write(b"bilingual export")
        result, captured = self._invoke_assemble(
            ["--out", os.path.join(self.tmp, "book.epub"), "--bilingual"]
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured["out_path"], os.path.join(self.tmp, "book (1).epub"))

    def test_out_on_source_keeps_core_protection(self):
        result, captured = self._invoke_assemble(["--out", "input.epub"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured["out_path"], "input.epub")
        self.assertNotIn("Output exists", plain_text(result.output))

    def test_translate_existing_out_moves_to_numbered_alternative(self):
        target = os.path.join(self.tmp, "book.epub")
        with open(target, "wb") as handle:
            handle.write(b"original export")
        cfg = Config.from_dict({"llm": {"preset": "fake"}})
        captured = {}

        class FakeOrchestrator:
            def __init__(self, config, *, allow_glossary_notes=False):
                del config, allow_glossary_notes
                self.client = FakeClient()

            def run_all(self, input_path, **kwargs):
                del input_path
                captured["out_path"] = kwargs.get("out_path")
                return {
                    "store": FakeStore(),
                    "output": kwargs.get("out_path"),
                    "outputs": [kwargs.get("out_path")],
                    "review_dir": None,
                }

        with (
            patch("wenyi_cli.commands.context.CommandContext.load_config", return_value=cfg),
            patch("wenyi_core.pipeline.orchestrator.Orchestrator", FakeOrchestrator),
            patch("wenyi_cli.commands.validation.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(app, ["translate", "input.txt", "--out", target])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured["out_path"], os.path.join(self.tmp, "book (1).epub"))
        self.assertIn("Output exists", plain_text(result.output))
