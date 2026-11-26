"""Web worker registration preserves the public Arq deployment contract."""

from wenyi_api.workers import ExportWorkerSettings, WorkerSettings
from wenyi_backend.workers import tasks


def test_exports_have_independent_queue():
    assert WorkerSettings.queue_name != ExportWorkerSettings.queue_name
    assert tasks.run_export not in WorkerSettings.functions
    assert tasks.run_export in ExportWorkerSettings.functions
    assert all("qa" not in fn.__name__ for fn in WorkerSettings.functions)
    assert all(fn.__name__ != "run_model_compare" for fn in WorkerSettings.functions)
