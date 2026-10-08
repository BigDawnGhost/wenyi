"""Invocation-scoped MinerU credentials never enter configuration or cached state."""

import os
import traceback
from pathlib import Path

import pytest
from wenyi_core.config import Config
from wenyi_core.ingest.errors import MinerUError
from wenyi_core.ingest.segmenter import load_document
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.input_preparation import parse_document
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.file import FileStorage

from tests.fake_llm import routing_handler


def _conversion(monkeypatch, seen):
    monkeypatch.setattr("wenyi_core.ingest.pdf_reader._check_deps", lambda: None)

    def convert(source, output, *, api_token):
        seen.append(api_token)
        Path(output).write_text("<html><body><p>Sample source text.</p></body></html>")

    monkeypatch.setattr("wenyi_core.ingest.pdf_to_html.convert_pdf_to_html", convert)


def test_load_document_resolves_only_uncached_pdf(monkeypatch, tmp_path):
    monkeypatch.setenv("MINERU_API_KEY", "ambient")
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fake pdf")
    seen = []
    resolutions = []
    _conversion(monkeypatch, seen)

    def resolver():
        resolutions.append(True)
        return "manual"

    for _ in range(2):
        load_document(
            str(source),
            "en",
            "zh",
            cache_dir=str(tmp_path / "cache"),
            mineru_token_resolver=resolver,
        )
    assert seen == ["manual"]
    assert resolutions == [True]
    assert os.environ["MINERU_API_KEY"] == "ambient"


@pytest.mark.parametrize("token", [None, ""])
def test_resolved_absence_does_not_reread_environment(monkeypatch, tmp_path, token):
    monkeypatch.setenv("MINERU_API_KEY", "ambient")
    monkeypatch.setattr("wenyi_core.ingest.pdf_reader._check_deps", lambda: None)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fake pdf")
    with pytest.raises(MinerUError, match="MINERU_API_KEY"):
        load_document(
            str(source),
            "en",
            "zh",
            cache_dir=str(tmp_path / "cache"),
            mineru_token_resolver=lambda: token,
        )


def test_resolver_error_never_exposes_secret(monkeypatch, tmp_path):
    monkeypatch.setattr("wenyi_core.ingest.pdf_reader._check_deps", lambda: None)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fake pdf")

    def resolver():
        raise ValueError("private-token")

    with pytest.raises(MinerUError) as caught:
        load_document(
            str(source),
            "en",
            "zh",
            cache_dir=str(tmp_path / "cache"),
            mineru_token_resolver=resolver,
        )
    assert str(caught.value) == "MinerU credential resolution failed"
    assert caught.value.__suppress_context__
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_non_pdf_does_not_resolve_mineru(tmp_path):
    source = tmp_path / "book.txt"
    source.write_text("Sample source text.")

    def resolver():
        pytest.fail("Non-PDF input must not access credentials")

    document = load_document(str(source), "en", "zh", mineru_token_resolver=resolver)
    assert document.fmt == "text"


@pytest.mark.parametrize("explicit_token", [None, ""])
def test_missing_token_fails_safely_before_conversion(monkeypatch, tmp_path, explicit_token):
    from wenyi_core.ingest.pdf_reader import read_pdf

    if explicit_token is None:
        monkeypatch.delenv("MINERU_API_KEY", raising=False)
    else:
        monkeypatch.setenv("MINERU_API_KEY", "must-not-be-used")
    monkeypatch.setattr("wenyi_core.ingest.pdf_reader._check_deps", lambda: None)

    def convert(*args, **kwargs):
        pytest.fail("Missing credentials must not invoke conversion")

    monkeypatch.setattr("wenyi_core.ingest.pdf_to_html.convert_pdf_to_html", convert)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fake pdf")
    with pytest.raises(MinerUError, match="API token not provided and MINERU_API_KEY not set"):
        read_pdf(
            str(source), "en", "zh", cache_dir=str(tmp_path / "cache"), api_token=explicit_token
        )


@pytest.mark.parametrize("credential_source", ["resolver", "environment", "explicit"])
@pytest.mark.parametrize("error_type", [MinerUError, ValueError])
@pytest.mark.parametrize("escaped", [False, True])
def test_conversion_errors_never_expose_credentials(
    monkeypatch, tmp_path, caplog, credential_source, error_type, escaped
):
    token = "private-token\nwith-tab\tand-quote'"
    ambient = token if credential_source == "environment" else "ambient-unused"
    monkeypatch.setenv("MINERU_API_KEY", ambient)
    monkeypatch.setattr("wenyi_core.ingest.pdf_reader._check_deps", lambda: None)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fake pdf")
    cache = tmp_path / "cache"
    seen = []

    def convert(source, output, *, api_token):
        seen.append(api_token)
        effective = os.getenv("MINERU_API_KEY") if api_token is None else api_token
        reflected = repr(effective.encode()) if escaped else effective
        raise error_type(f"Illegal header value: {reflected}")

    monkeypatch.setattr("wenyi_core.ingest.pdf_to_html.convert_pdf_to_html", convert)
    from wenyi_core.ingest.pdf_reader import read_pdf

    kwargs = {}
    if credential_source == "resolver":
        kwargs["mineru_token_resolver"] = lambda: token
    elif credential_source == "explicit":
        kwargs["api_token"] = token
    with pytest.raises(MinerUError) as caught:
        read_pdf(str(source), "en", "zh", cache_dir=str(cache), **kwargs)
    assert seen == [token]
    assert str(caught.value) == (
        "PDF conversion failed. Check MinerU credentials, service availability, "
        "and the input PDF, then retry."
    )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    rendered = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert token not in rendered
    assert repr(token.encode()) not in rendered
    assert "private-token" not in caplog.text
    assert os.environ["MINERU_API_KEY"] == ambient
    assert not list(cache.rglob("*.html"))


def test_parse_artifact_skips_resolver_on_reuse(monkeypatch, tmp_path):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fake pdf")
    seen = []
    _conversion(monkeypatch, seen)
    config = Config.from_dict({"language": {"source": "en", "target": "zh"}})
    store = FileStorage(str(tmp_path / "state"))
    with store.lock():
        document = parse_document(
            store, str(source), config, mineru_token_resolver=lambda: "manual-only-token"
        )

        def unavailable():
            pytest.fail("Parsed artifacts must not resolve credentials again")

        reused = parse_document(store, str(source), config, mineru_token_resolver=unavailable)
    assert reused.chapters == document.chapters
    assert seen == ["manual-only-token"]
    assert "manual-only-token" not in str(store.read_artifact("parsed_document.json"))


def test_prepare_consumes_manual_token_without_persisting_it(monkeypatch, tmp_path):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"fake pdf")
    seen = []
    _conversion(monkeypatch, seen)
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"book_understanding": False, "review": False, "polish": False},
        }
    )
    store = FileStorage(str(tmp_path / "state"))
    orch = Orchestrator(
        config,
        client=FakeClient(handler=routing_handler),
        storage=store,
        mineru_token_resolver=lambda: "manual-only-token",
    )
    orch.prepare(str(source))
    orch.prepare(str(source))
    assert seen == ["manual-only-token"]
    assert "manual-only-token" not in str(config.model_dump())
    for path in (tmp_path / "state").rglob("*"):
        if path.is_file():
            assert b"manual-only-token" not in path.read_bytes()
