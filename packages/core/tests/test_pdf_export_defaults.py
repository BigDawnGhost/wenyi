"""Default PDF export follows persisted BabelDOC metadata and snapshot boundaries."""

import hashlib
import json
from pathlib import Path

import pytest
from pypdf import PdfReader
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.assemble.writer import assemble
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.run_store import RunStore

from tests.fake_llm import routing_handler
from tests.pdf_fixtures import pdf_bytes


@pytest.fixture
def pdf_project(tmp_path, monkeypatch):
    source = tmp_path / "book.PDF"
    source.write_bytes(pdf_bytes(["Original page"]))
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
        "wenyi_core.pipeline.prepare.input_preparation.load_document",
        lambda *a, **kw: document.model_copy(deep=True),
    )
    monkeypatch.setattr(
        "wenyi_cli.commands.context.CommandContext.load_config", lambda self: config
    )
    monkeypatch.setattr(
        "wenyi_core.pipeline.run.runtime.build_client",
        lambda cfg: FakeClient(handler=routing_handler, config=cfg.llm),
    )
    requests = []

    def fillback(self, session_id, translations, *, out_path):
        requests.append((session_id, dict(translations), out_path))
        Path(out_path).write_bytes(pdf_bytes(["Translated page"]))
        return str(out_path)

    monkeypatch.setattr("wenyi_core.pdf_bridge.BabeldocBridgeClient.fillback", fillback)
    return config, source, requests


def test_machine_receipts_cover_real_pdf_workflow_and_resume(pdf_project, tmp_path):
    _, source, requests = pdf_project
    progress, receipt = tmp_path / "progress.json", tmp_path / "result.json"
    previous_id = None
    for _ in range(2):
        response = CliRunner().invoke(
            app,
            [
                "translate",
                str(source),
                "--progress-json",
                str(progress),
                "--result-json",
                str(receipt),
            ],
        )
        assert response.exit_code == 0, response.output
        result = json.loads(receipt.read_text())
        snapshot = json.loads(progress.read_text())
        assert result["status"] == snapshot["status"] == "success"
        assert result["run_id"] == snapshot["run_id"] != previous_id
        assert result["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        output = Path(result["outputs"][0]["path"])
        assert output.read_bytes() == pdf_bytes(["Translated page"])
        assert result["outputs"][0]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
        assert Path(result["state_dir"]).is_dir()
        previous_id = result["run_id"]
    assert len(requests) == 2
    assert all(request[:2] == ("offline-session", {"0:0": "译0"}) for request in requests)


@pytest.mark.parametrize("command", ["translate", "assemble"])
@pytest.mark.parametrize("order", ["target_first", "source_first"])
@pytest.mark.parametrize(
    "mono,bilingual,explicit_out",
    [
        (True, False, False),
        (False, True, False),
        (True, True, False),
        (True, True, True),
        (False, True, True),
    ],
)
def test_cli_defaults_to_pdf_for_mono_and_bilingual_outputs(
    pdf_project, tmp_path, command, order, mono, bilingual, explicit_out
):
    config, source, requests = pdf_project
    config.output.mono = mono
    config.output.bilingual = bilingual
    config.output.bilingual_order = order
    if command == "assemble":
        Orchestrator(config).run(str(source))
    arguments = [command, str(source)]
    if explicit_out:
        arguments += ["--out", str(tmp_path / "custom" / "result.pdf")]

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 0, result.output
    expected = []
    if explicit_out:
        if mono:
            expected.append(tmp_path / "custom" / "result.pdf")
        if bilingual:
            expected.append(tmp_path / "custom" / "result-bi.pdf")
    else:
        if mono:
            expected.append(tmp_path / "output" / "book.zh.pdf")
        if bilingual:
            expected.append(tmp_path / "output" / "book.zh-bi.pdf")
    assert len(requests) == len(expected)
    assert set(expected[0].parent.iterdir()) == set(expected)
    for path in expected:
        page = PdfReader(path).pages[0]
        paired = path.stem.endswith("-bi")
        assert page.mediabox.width == (400 if paired else 200)
        text = page.extract_text()
        assert ("Original page" in text) is paired
        assert "Translated page" in text
        if paired:
            source_first = text.index("Original page") < text.index("Translated page")
            assert source_first is (order == "source_first")
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


@pytest.mark.parametrize("meta", [None, {}, {"mineru": True}, {"babeldoc": False}])
def test_non_babeldoc_pdf_export_fails_before_creating_output(pdf_project, tmp_path, meta):
    config, source, requests = pdf_project
    store = Orchestrator(config).run(str(source))
    manifest = store.load_manifest()
    manifest["meta"] = meta
    store.save_manifest(manifest)
    output = tmp_path / "rejected" / "book.pdf"

    with pytest.raises(ValueError, match="PDF export is only available for BabelDOC"):
        assemble(store, str(source), out_path=str(output), out_format="pdf")

    assert not output.parent.exists()
    assert requests == []


def test_cli_rejects_non_babeldoc_pdf_export_without_traceback(pdf_project):
    config, source, requests = pdf_project
    store = Orchestrator(config).run(str(source))
    manifest = store.load_manifest()
    manifest["meta"] = {"mineru": True}
    store.save_manifest(manifest)

    result = CliRunner().invoke(app, ["assemble", str(source), "--format", "pdf"])

    assert result.exit_code == 1, result.output
    assert "PDF export is only available for BabelDOC" in result.output
    assert "Traceback" not in result.output
    assert not (source.parent / "output").exists()
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


@pytest.mark.parametrize("bilingual", [False, True])
def test_pdf_export_retains_blank_polished_paragraph(pdf_project, tmp_path, bilingual):
    config, source, requests = pdf_project
    store = Orchestrator(config).run(str(source))
    chapter = store.load_chapter(0)
    chapter.segments.append(
        Segment(
            index=35,
            source=".",
            target="",
            target_before_polish=".",
            meta={"babeldoc_id": "2:4", "layout_label": "fallback_line"},
        )
    )
    store.save_chapter(chapter)
    before = store.load_chapter(0).model_dump()
    output = tmp_path / "export.pdf"

    assemble(store, str(source), out_path=str(output), out_format="pdf", bilingual=bilingual)

    assert output.read_bytes().startswith(b"%PDF-")
    assert requests[-1][1] == {"0:0": "译0", "2:4": "."}
    assert store.load_chapter(0).model_dump() == before
