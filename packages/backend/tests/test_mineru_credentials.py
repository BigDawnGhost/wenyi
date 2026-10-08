"""Shared workers resolve external credentials from their own execution context."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from tests.fake_llm import routing_handler
from wenyi_backend.context import current_context, use_context
from wenyi_backend.workers import tasks
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.storage.file import FileStorage


@pytest.mark.parametrize("operation", ["parse", "prepare", "parse_then_prepare", "translation"])
def test_worker_conversion_resolves_execution_credentials_only(
    backend_context, monkeypatch, tmp_path, operation
):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fake pdf")
    monkeypatch.setattr(tasks, "_resolve_source", lambda pid: str(source))
    monkeypatch.setattr(tasks.dal, "get_project", lambda pid: {"fmt": "pdf"})
    monkeypatch.setattr("wenyi_core.ingest.pdf_reader._check_deps", lambda: None)
    seen = []

    def convert(source, output, *, api_token):
        seen.append(api_token)
        Path(output).write_text("<html><body><p>Sample source text.</p></body></html>")

    monkeypatch.setattr("wenyi_core.ingest.pdf_to_html.convert_pdf_to_html", convert)
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"book_understanding": False, "review": False, "polish": False},
        }
    )
    params = {"config_snapshot": config.model_dump(mode="json")}
    context = replace(backend_context, mineru_token_resolver=lambda: "execution-only-token")
    storage = FileStorage(str(tmp_path / "state"))
    with use_context(context):
        if operation in {"parse", "parse_then_prepare"}:
            tasks._parse_source("p", storage, config, lambda *args: None)
            tasks._parse_source("p", storage, config, lambda *args: None)
        if operation != "parse":
            tasks._book_operation(
                "translation" if operation == "translation" else "prepare",
                "p",
                storage,
                config,
                FakeClient(handler=routing_handler),
                lambda *args: None,
                params,
            )
    assert seen == ["execution-only-token"]
    assert "execution-only-token" not in str(params)
    for path in (tmp_path / "state").rglob("*"):
        if path.is_file():
            assert b"execution-only-token" not in path.read_bytes()


def test_pdf_resolvers_remain_context_local(backend_context, monkeypatch, tmp_path):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fake pdf")
    monkeypatch.setattr(tasks, "_resolve_source", lambda pid: str(source))
    monkeypatch.setattr(tasks.dal, "get_project", lambda pid: {"fmt": "pdf"})
    monkeypatch.setattr("wenyi_core.ingest.pdf_reader._check_deps", lambda: None)
    seen = {}

    def convert(source, output, *, api_token):
        seen[current_context().data_dir] = api_token
        Path(output).write_text("<html><body><p>Sample source text.</p></body></html>")

    monkeypatch.setattr("wenyi_core.ingest.pdf_to_html.convert_pdf_to_html", convert)

    def parse(index):
        context = replace(
            backend_context,
            data_dir=f"app-{index}",
            mineru_token_resolver=lambda: f"token-{index}",
        )
        with use_context(context):
            tasks._parse_source(
                "p",
                FileStorage(str(tmp_path / f"state-{index}")),
                Config.from_dict({"language": {"source": "en", "target": "zh"}}),
                lambda *args: None,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(parse, range(2)))
    assert seen == {"app-0": "token-0", "app-1": "token-1"}
    assert current_context() is backend_context
