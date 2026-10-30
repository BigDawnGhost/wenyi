"""In-memory artifact storage for offline domain tests."""

from contextlib import nullcontext
from copy import deepcopy


class MemoryArtifacts:
    """An independent artifact backend with no filesystem delegation."""

    def __init__(self, run_dir):
        self.run_dir = str(run_dir)
        self.data = {}
        self.events = []
        self.manifest = {}
        self.usage = None

    def read_artifact(self, key):
        return deepcopy(self.data.get(key))

    def write_artifact(self, key, value):
        self.data[key] = deepcopy(value)

    def delete_artifact(self, key):
        self.data.pop(key, None)

    def list_artifacts(self, prefix=""):
        return sorted(key for key in self.data if key.startswith(prefix))

    def append_artifact_record(self, key, record):
        self.data.setdefault(key, []).append(deepcopy(record))

    def read_artifact_records(self, key):
        return deepcopy(self.data.get(key, []))

    def state_lock(self):
        return nullcontext()

    def save_manifest(self, data):
        self.manifest = deepcopy(data)

    def save_usage(self, data):
        self.usage = deepcopy(data)

    def load_usage(self):
        return deepcopy(self.usage)

    def record_timing(self, record):
        self.data.setdefault("timing", {})[record["id"]] = record
        return self.data["timing"]

    def log_event(self, event, **data):
        self.events.append({"event": event, **data})
