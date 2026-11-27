"""Drive the source Desktop child through normal parsing, pause, resume and exit."""

import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from desktop_test_support import initialize, local_backend, wait_job
from fastapi.testclient import TestClient
from wenyi_backend.context import use_context
from wenyi_backend.execution import process
from wenyi_desktop.main import create_app, create_context


@pytest.mark.parametrize("kind", ["prepare", "review"])
def test_pause_and_close_kill_the_model_writer_without_waiting(tmp_path, monkeypatch, kind):
    # Desktop must wake the owning supervisor, not wait for its next catalog poll.
    monkeypatch.setattr(process, "_POLL_SECONDS", 60)
    attempts = queue.Queue()

    class SilentModel(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            disconnected = threading.Event()
            attempts.put(disconnected)
            try:
                self.rfile.read()  # Never send an HTTP response.
            finally:
                disconnected.set()

    # Override the model endpoint before creating a project. No external endpoint
    # or OS credential vault is needed for this test.
    with ThreadingHTTPServer(("127.0.0.1", 0), SilentModel) as model:
        serving = threading.Thread(target=model.serve_forever, daemon=True)
        serving.start()
        config = tmp_path / "config.yaml"
        config.write_text(
            f"llm:\n  preset: deepseek\n  providers:\n    deepseek:\n"
            f"      kind: deepseek\n      base_url: http://127.0.0.1:{model.server_port}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test")
        context = create_context(tmp_path / "data", config_path=str(config))
        app = create_app(context=context, isolate_workflows=True)
        try:
            with use_context(context), TestClient(app) as client:
                created = client.post(
                    "/projects",
                    data={
                        "project": json.dumps(
                            {
                                "name": "Offline",
                                "source_lang": "en",
                                "target_lang": "zh",
                            }
                        )
                    },
                    files={"file": ("book.txt", b"A short source paragraph.\n")},
                )
                assert created.status_code == 201, created.text
                pid = created.json()["id"]
                root = f"/projects/{pid}"
                parse = context.repository.list_jobs(pid)[0]
                assert wait_job(parse["run_id"], timeout=15)["status"] == "done"
                assert client.get(root + "/preview").status_code == 200

                backend = local_backend(context)
                if kind == "review":
                    pid, store = initialize(backend)
                    store.close()
                    root = f"/projects/{pid}"
                preparing = client.post(root + ("/review/run" if kind == "review" else "/prepare"))
                assert preparing.status_code == 200, preparing.text
                first = attempts.get(timeout=15)
                directory = backend.project_dir(pid)
                unplugged = tmp_path / "disconnected-library"
                # On Windows the open project cannot be renamed; the catalog-only
                # failure injection in test_local_workflows also covers that platform.
                disconnect = kind == "prepare" and os.name != "nt"
                if disconnect:
                    directory.rename(unplugged)
                try:
                    assert client.post(root + "/pause").status_code == 200
                    assert first.wait(5)
                    assert wait_job(preparing.json()["job_id"], timeout=5)["status"] == "paused"
                finally:
                    if disconnect:
                        unplugged.rename(directory)
                assert client.get(root).json()["status"] == "paused"
                if kind == "review":
                    runs = client.get(root + "/review/runs").json()
                    assert len(runs) == 1 and runs[0]["status"] == "paused"
                    detail = client.get(root + "/review/runs/" + runs[0]["id"]).json()
                    assert detail["result"]["status"] == detail["status"] == "paused"
                    assert detail["result"]["workflow_run_id"] == preparing.json()["job_id"]
                    events = client.get(root + "/events", params={"type": "task_paused"}).json()
                    assert [e["payload"]["run_id"] for e in events] == [preparing.json()["job_id"]]

                resumed = client.post(root + "/resume")
                assert resumed.status_code == 200, resumed.text
                second = attempts.get(timeout=15)
                if kind == "review":
                    live = client.get(root + "/review/runs/" + runs[0]["id"]).json()
                    assert live["status"] == "running"
                    assert live["result"]["workflow_run_id"] == resumed.json()["job_id"]

            assert second.wait(5)
            interrupted = context.repository.latest_resumable_job(pid)
            assert interrupted is not None and interrupted["status"] == "interrupted"
            with TestClient(app) as restarted:
                assert restarted.get(root).json()["status"] == "paused"
                if kind == "review":
                    assert restarted.get(root + "/review/runs").json()[0]["status"] == "interrupted"
        finally:
            model.shutdown()
            serving.join(timeout=5)
