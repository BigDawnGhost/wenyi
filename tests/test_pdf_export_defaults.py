"""Default PDF export follows persisted BabelDOC metadata and snapshot boundaries."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fake_llm import routing_handler
from trans_novel.assemble.writer import assemble
from trans_novel.cli import app
from trans_novel.config import Config
from trans_novel.ingest.models import Chapter, Document, Segment
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.runstore import RunStore


@pytest.fixture
def pdf_project(tmp_path, monkeypatch):
    source = tmp_path / "book.PDF"
    source.write_bytes(b"%PDF-1.4 offline fixture")
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {
                "pdf_backend": "babeldoc",
                "book_understanding": False,
                "polish": False,
                "review": False,
                "annotation_alignment": False,
            },
            "paths": {"state_dir": str(tmp_path / "state")},
        }
    )
    document = Document(
        title="book",
        source_lang="en",
        target_lang="zh",
        source_path=str(source),
        fmt="pdf",
        meta={
            "babeldoc": True,
            "pdf_export": "babeldoc",
            "babeldoc_session_id": "offline-session",
            "babeldoc_bridge_url": "http://bridge.invalid",
        },
        chapters=[
            Chapter(
                index=0,
                title="Chapter one",
                # Supply a template so explicit EPUB overrides can use the existing renderer.
                template='<html><body><p data-tn-id="p0">A short source.</p></body></html>',
                segments=[
                    Segment(
                        index=0,
                        source="A short source.",
                        anchor="p0",
                        meta={"babeldoc_id": "0:0"},
                    )
                ],
            )
        ],
    )
    monkeypatch.setattr(
        "trans_novel.pipeline.preparation.load_document",
        lambda *a, **kw: document.model_copy(deep=True),
    )
    monkeypatch.setattr("trans_novel.cli._load_config", lambda: config)
    monkeypatch.setattr(
        "trans_novel.pipeline.runtime.build_client",
        lambda cfg: FakeClient(handler=routing_handler, config=cfg.llm),
    )
    requests = []

    def fillback(self, session_id, translations, *, out_path):
        requests.append((session_id, dict(translations), out_path))
        Path(out_path).write_bytes(b"%PDF-1.4 offline fillback")
        return str(out_path)

    monkeypatch.setattr("trans_novel.pdf_bridge.BabeldocBridgeClient.fillback", fillback)
    return config, source, requests


@pytest.mark.parametrize("command", ["translate", "assemble"])
@pytest.mark.parametrize(
    "mono,bilingual,explicit_out",
    [(True, False, False), (False, True, False), (True, True, False), (True, True, True)],
)
def test_cli_defaults_to_pdf_for_mono_and_bilingual_outputs(
    pdf_project, tmp_path, command, mono, bilingual, explicit_out
):
    config, source, requests = pdf_project
    config.output.mono = mono
    config.output.bilingual = bilingual
    if command == "assemble":
        Orchestrator(config).run(str(source))
    arguments = [command, str(source)]
    if explicit_out:
        arguments += ["--out", str(tmp_path / "custom" / "result.pdf")]

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 0, result.output
    if explicit_out:
        expected = [tmp_path / "custom" / "result.pdf", tmp_path / "custom" / "result-bi.pdf"]
    else:
        expected = []
        if mono:
            expected.append(tmp_path / "output" / "book.zh.pdf")
        if bilingual:
            expected.append(tmp_path / "output" / "book.zh-bi.pdf")
    assert [Path(request[2]) for request in requests] == expected
    assert all(path.read_bytes().startswith(b"%PDF-") for path in expected)
    assert all(request[:2] == ("offline-session", {"0:0": "译0"}) for request in requests)


@pytest.mark.parametrize("command", ["translate", "assemble"])
@pytest.mark.parametrize("fmt", ["epub", "txt"])
def test_explicit_format_overrides_babeldoc_default(pdf_project, command, fmt):
    config, source, requests = pdf_project
    if command == "assemble":
        Orchestrator(config).run(str(source))

    result = CliRunner().invoke(app, [command, str(source), "--format", fmt])

    assert result.exit_code == 0, result.output
    assert (source.parent / "output" / f"book.zh.{fmt}").is_file()
    assert requests == []


def test_chapter_only_translation_does_not_treat_pdf_default_as_explicit_format(pdf_project):
    _, source, requests = pdf_project
    result = CliRunner().invoke(app, ["translate", str(source), "--chapter", "0"])
    assert result.exit_code == 0, result.output
    assert requests == []
    assert not (source.parent / "output").exists()


def test_saved_babeldoc_state_defaults_to_pdf_after_config_switches_to_mineru(pdf_project):
    config, source, requests = pdf_project
    Orchestrator(config).run(str(source))
    config.pipeline.pdf_backend = "mineru"

    result = CliRunner().invoke(app, ["assemble", str(source)])

    assert result.exit_code == 0, result.output
    assert len(requests) == 1
    assert Path(requests[0][2]).suffix == ".pdf"


def test_saved_non_babeldoc_pdf_still_defaults_to_epub(pdf_project):
    config, source, requests = pdf_project
    store = Orchestrator(config).run(str(source))
    manifest = store.load_manifest()
    manifest["meta"] = {"mineru": True}
    store.save_manifest(manifest)

    result = CliRunner().invoke(app, ["assemble", str(source)])

    assert result.exit_code == 0, result.output
    assert (source.parent / "output" / "book.zh.epub").is_file()
    assert requests == []


def test_direct_writer_and_whole_workflow_share_babeldoc_default(pdf_project):
    config, source, requests = pdf_project
    result = Orchestrator(config).run_all(str(source))
    assert result["outputs"] == [str(source.parent / "output" / "book.zh.pdf")]
    output = assemble(result["store"], str(source))
    assert output == result["outputs"][0]
    assert len(requests) == 2


def test_default_format_and_targets_come_from_the_same_export_snapshot(pdf_project, monkeypatch):
    config, source, requests = pdf_project
    store = Orchestrator(config).run(str(source))
    create_snapshot = RunStore.create_export_snapshot

    def change_live_state_after_snapshot(self, **kwargs):
        snapshot = create_snapshot(self, **kwargs)
        with store.lock():
            manifest = store.load_manifest()
            manifest["meta"] = {"mineru": True}
            chapter = store.load_chapter(0)
            chapter.segments[0].target = "Later live translation"
            store.save_chapter(chapter)
            store.save_manifest(manifest)
        return snapshot

    monkeypatch.setattr(RunStore, "create_export_snapshot", change_live_state_after_snapshot)
    result = Orchestrator(config).run_assemble(str(source))

    assert result["outputs"] == [str(source.parent / "output" / "book.zh.pdf")]
    assert requests[0][1] == {"0:0": "译0"}
    assert store.load_chapter(0).segments[0].target == "Later live translation"
