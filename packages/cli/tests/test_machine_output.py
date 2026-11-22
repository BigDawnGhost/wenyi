"""Machine receipts use temporary files and a fake workflow, never model services."""

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nsource\n%%EOF")
    output = tmp_path / "paper.zh.pdf"
    config = Config.from_dict({"llm": {"preset": "fake"}})
    store = SimpleNamespace(
        run_dir=str(tmp_path / "state"),
        lock=nullcontext,
        state_lock=nullcontext,
        load_manifest=lambda: {"chapters": [{"index": 0, "status": "done"}]},
        stats=lambda: {"terms": 0},
        load_usage=lambda: {},
    )

    class Workflow:
        def __init__(self, *args, **kwargs):
            self.client = FakeClient()

        def run_all(self, input_path, *, progress, **kwargs):
            progress(1, 3, "Translating")
            progress(0, 0, "Exporting")
            output.write_bytes(b"%PDF-1.7\ntranslated\n%%EOF")
            return {"store": store, "outputs": [str(output)], "output": str(output)}

    monkeypatch.setattr("wenyi_cli.commands.context.CommandContext.load_config", lambda _: config)
    monkeypatch.setattr("wenyi_core.pipeline.orchestrator.Orchestrator", Workflow)
    return source, output


def invoke(source, progress, result, *extra):
    return CliRunner().invoke(
        app,
        [
            "translate",
            str(source),
            "--progress-json",
            str(progress),
            "--result-json",
            str(result),
            *extra,
        ],
    )


def test_machine_receipt_identifies_this_run_and_actual_outputs(workflow, tmp_path):
    source, output = workflow
    progress, result = tmp_path / "progress.json", tmp_path / "result.json"
    response = invoke(source, progress, result)
    assert response.exit_code == 0, response.output
    receipt = json.loads(result.read_text())
    snapshot = json.loads(progress.read_text())
    assert receipt["protocol_version"] == 1
    assert receipt["status"] == snapshot["status"] == "success"
    assert receipt["run_id"] == snapshot["run_id"]
    assert receipt["source_sha256"]
    assert receipt["outputs"][0]["path"] == str(output)
    assert receipt["outputs"][0]["size"] == output.stat().st_size
    assert len(receipt["outputs"][0]["sha256"]) == 64
    assert receipt["state_dir"] == str(tmp_path / "state")


def test_failed_invocation_replaces_stale_success_without_leaking_errors(
    workflow, tmp_path, monkeypatch
):
    source, _ = workflow
    progress, result = tmp_path / "progress.json", tmp_path / "result.json"
    assert invoke(source, progress, result).exit_code == 0
    old_id = json.loads(result.read_text())["run_id"]

    def fail(_):
        raise ValueError("private failure detail")

    monkeypatch.setattr("wenyi_cli.commands.context.CommandContext.load_config", fail)
    response = invoke(source, progress, result)
    assert response.exit_code == 1
    receipt = json.loads(result.read_text())
    assert receipt["status"] == "failed"
    assert receipt["run_id"] != old_id
    assert receipt["outputs"] == []
    assert "private failure detail" not in result.read_text()


@pytest.mark.parametrize("protected", ["source", "config", "output", "same"])
def test_machine_paths_cannot_overwrite_workflow_files(workflow, tmp_path, protected):
    source, output = workflow
    config = tmp_path / "settings.yaml"
    config.write_text("language:\n  target: zh\n")
    progress = tmp_path / "progress.json"
    result = {"source": source, "config": config, "output": output, "same": progress}[protected]
    before = {path: path.read_bytes() for path in (source, config)}
    response = CliRunner().invoke(
        app,
        [
            "--config",
            str(config),
            "translate",
            str(source),
            "--out",
            str(output),
            "--progress-json",
            str(progress),
            "--result-json",
            str(result),
        ],
    )
    assert response.exit_code == 1
    assert "Machine output" in response.output
    for path, contents in before.items():
        assert path.read_bytes() == contents
    assert not output.exists()


def test_receipt_never_claims_missing_outputs(workflow, tmp_path, monkeypatch):
    source, _ = workflow
    original = Path.stat

    def stat(path, *args, **kwargs):
        if path.name == "paper.zh.pdf":
            raise FileNotFoundError("Output disappeared")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    result = tmp_path / "result.json"
    response = invoke(source, tmp_path / "progress.json", result)
    assert response.exit_code == 1
    assert json.loads(result.read_text())["status"] == "failed"


def _write_temp_config(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "paths:\n"
        + f"  state_dir: {(tmp_path / 'state').as_posix()}\n"
        + "llm:\n"
        + "  preset: fake\n"
    )
    return config


def _invoke_with_config(source, config, progress, result):
    return CliRunner().invoke(
        app,
        [
            "--config",
            str(config),
            "translate",
            str(source),
            "--progress-json",
            str(progress),
            "--result-json",
            str(result),
        ],
    )


def test_failed_receipt_reports_event_log_reason(tmp_path, monkeypatch):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nsource\n%%EOF")
    config = _write_temp_config(tmp_path)
    result = tmp_path / "result.json"

    class Workflow:
        def __init__(self, *args, **kwargs):
            self.client = FakeClient()

        def run_all(self, input_path, *, progress, **kwargs):
            from wenyi_core.config_rebuild import load_rebuilt_config
            from wenyi_core.storage.run_store import RunStore, translation_run_dir

            settings = load_rebuilt_config(str(config))
            store = RunStore(
                translation_run_dir(settings.state_dir, "paper", settings.target_lang),
                create=True,
            )
            store.log_event(
                "llm_retry_exhausted",
                provider="fake",
                profile="default",
                stage="translate",
                attempts=2,
                reason="timeout",
                error_type="TimeoutError",
                status_code=None,
                request_id="req-1",
            )
            raise RuntimeError("model backend unreachable")

    monkeypatch.setattr("wenyi_core.pipeline.orchestrator.Orchestrator", Workflow)
    response = _invoke_with_config(source, config, tmp_path / "progress.json", result)
    assert response.exit_code == 1
    receipt = json.loads(result.read_text())
    assert receipt["status"] == "failed"
    message = receipt["error"]["message"]
    assert "LLM request failed after 2 attempts" in message
    assert "TimeoutError" in message
    assert "reason=timeout" in message
    assert "stage=translate" in message
    assert "model backend unreachable" not in message


def test_failed_receipt_reports_unfinished_pipeline_steps(tmp_path, monkeypatch):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nsource\n%%EOF")
    config = _write_temp_config(tmp_path)
    result = tmp_path / "result.json"

    class Workflow:
        def __init__(self, *args, **kwargs):
            self.client = FakeClient()

        def run_all(self, input_path, *, progress, **kwargs):
            from wenyi_core.config_rebuild import load_rebuilt_config
            from wenyi_core.storage.run_store import RunStore, translation_run_dir

            settings = load_rebuilt_config(str(config))
            store = RunStore(
                translation_run_dir(settings.state_dir, "paper", settings.target_lang),
                create=True,
            )
            store.log_event(
                "run_steps_started", steps=["translate", "review"], input_path=str(input_path)
            )
            raise RuntimeError("cancelled by test")

    monkeypatch.setattr("wenyi_core.pipeline.orchestrator.Orchestrator", Workflow)
    response = _invoke_with_config(source, config, tmp_path / "progress.json", result)
    assert response.exit_code == 1
    message = json.loads(result.read_text())["error"]["message"]
    assert "translate, review" in message
    assert "cancelled by test" not in message


def test_failed_receipt_falls_back_without_error_events(tmp_path, monkeypatch):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nsource\n%%EOF")
    config = _write_temp_config(tmp_path)
    result = tmp_path / "result.json"

    class Workflow:
        def __init__(self, *args, **kwargs):
            self.client = FakeClient()

        def run_all(self, input_path, *, progress, **kwargs):
            raise RuntimeError("model backend unreachable")

    monkeypatch.setattr("wenyi_core.pipeline.orchestrator.Orchestrator", Workflow)
    response = _invoke_with_config(source, config, tmp_path / "progress.json", result)
    assert response.exit_code == 1
    receipt = json.loads(result.read_text())
    assert receipt["status"] == "failed"
    assert receipt["error"]["code"] == "translation_failed"
    assert "no error events" in receipt["error"]["message"]
