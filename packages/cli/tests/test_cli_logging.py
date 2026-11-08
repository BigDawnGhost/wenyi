"""CLI diagnostics belong in invocation-local files, not in live progress output."""

import json
import logging

import typer
from typer.testing import CliRunner
from wenyi_cli.cli import create_app
from wenyi_cli.commands.context import current_context
from wenyi_core.observability import bind_request_id, reset_request_id


def test_cli_saves_application_and_transport_logs_without_console_noise(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "settings" / "custom.yaml"
    app = create_app()

    @app.command()
    def probe():
        current_context().console.print("Progress is still visible")
        logging.getLogger("httpx").info("HTTP Request: GET /health 200 OK")
        logging.getLogger("httpcore").info("connection opened")
        logging.getLogger("wenyi_core.storage.locks").warning("using Python locks")
        logging.getLogger("wenyi.probe").error("diagnostic error")
        current_context().console.print("Finished")

    result = CliRunner().invoke(app, ["--config", str(config), "probe"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["Progress is still visible", "Finished"]
    [path] = (config.parent / "logs").glob("wenyi-cli-*.log")
    text = path.read_text(encoding="utf-8")
    for message in ("HTTP Request:", "connection opened", "using Python locks", "diagnostic error"):
        assert message in text
        assert message not in result.output
    assert not (tmp_path / "logs").exists()


def test_app_creation_does_not_reconfigure_process_logging(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    create_app()
    assert root.handlers == before
    assert root.level == level
    assert list(tmp_path.iterdir()) == []


def test_repeated_invocations_close_files_and_restore_existing_handlers(tmp_path):
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    app = create_app()
    installed = []

    @app.command()
    def probe(label: str):
        installed.extend(h for h in root.handlers if getattr(h, "_wenyi_handler", False))
        logging.getLogger("wenyi.probe").info(label)

    runner = CliRunner()
    for label in ("first invocation", "second invocation"):
        result = runner.invoke(app, ["-c", str(tmp_path / "config.yaml"), "probe", label])
        assert result.exit_code == 0, result.output
        assert root.handlers == before
        assert root.level == level
    files = list((tmp_path / "logs").glob("wenyi-cli-*.log"))
    assert len(files) == 2
    texts = [path.read_text(encoding="utf-8") for path in files]
    assert sum("first invocation" in text for text in texts) == 1
    assert sum("second invocation" in text for text in texts) == 1
    assert len(installed) == 2
    assert all(handler.stream is None for handler in installed)


def test_json_log_files_keep_correlation_and_exception_details(tmp_path, monkeypatch):
    monkeypatch.setenv("WENYI_LOG_FORMAT", "json")
    app = create_app()

    @app.command()
    def probe():
        token = bind_request_id("test-correlation")
        try:
            try:
                raise ValueError("diagnostic detail")
            except ValueError:
                logging.getLogger("wenyi.probe").exception("operation failed")
        finally:
            reset_request_id(token)

    result = CliRunner().invoke(app, ["-c", str(tmp_path / "config.yaml"), "probe"])
    assert result.exit_code == 0, result.output
    assert result.output == ""
    [path] = (tmp_path / "logs").glob("wenyi-cli-*.log")
    [record] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert record["service"] == "wenyi-cli"
    assert record["request_id"] == "test-correlation"
    assert "ValueError: diagnostic detail" in record["exception"]


def test_failed_commands_keep_visible_error_and_point_to_saved_log(tmp_path):
    app = create_app()

    @app.command()
    def probe():
        logging.getLogger("wenyi.probe").warning("saved diagnostic")
        current_context().console.print("Error: invalid input")
        raise typer.Exit(1)

    result = CliRunner().invoke(app, ["-c", str(tmp_path / "config.yaml"), "probe"])
    assert result.exit_code == 1
    [path] = (tmp_path / "logs").glob("wenyi-cli-*.log")
    assert "Error: invalid input" in result.output
    assert str(path) in result.output
    assert "saved diagnostic" not in result.output
    assert "saved diagnostic" in path.read_text(encoding="utf-8")


def test_unwritable_log_destination_fails_clearly_without_console_fallback(tmp_path):
    (tmp_path / "logs").write_text("not a directory", encoding="utf-8")
    app = create_app()
    invoked = []

    @app.command()
    def probe():
        invoked.append(True)

    result = CliRunner().invoke(app, ["-c", str(tmp_path / "config.yaml"), "probe"])
    assert result.exit_code == 1
    assert "Cannot initialize diagnostic log" in result.output
    assert "Traceback" not in result.output
    assert invoked == []


def test_unexpected_failure_keeps_traceback_in_diagnostic_log(tmp_path):
    app = create_app()

    @app.command()
    def probe():
        raise RuntimeError("unexpected diagnostic detail")

    result = CliRunner().invoke(app, ["-c", str(tmp_path / "config.yaml"), "probe"])
    assert result.exit_code == 1
    [path] = (tmp_path / "logs").glob("wenyi-cli-*.log")
    assert str(path) in result.output
    assert "RuntimeError: unexpected diagnostic detail" in path.read_text(encoding="utf-8")


def test_config_creation_failure_is_concise_and_logging_is_restored(tmp_path, monkeypatch):
    app = create_app()
    root = logging.getLogger()
    before, level = list(root.handlers), root.level

    def fail(*_):
        raise PermissionError("config directory is read-only")

    monkeypatch.setattr("wenyi_cli.commands.bootstrap.Config.create_default_file", fail)
    result = CliRunner().invoke(app, ["-c", str(tmp_path / "config.yaml"), "--help"])
    assert result.exit_code == 1
    assert "Configuration error: config directory is read-only" in result.output
    assert "Traceback" not in result.output
    assert root.handlers == before
    assert root.level == level
