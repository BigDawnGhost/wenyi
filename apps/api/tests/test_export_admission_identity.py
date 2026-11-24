"""In-flight export admission matches the full request identity, not just format."""

from __future__ import annotations

import json
from types import SimpleNamespace

from wenyi_api.adapters import PostgresExports


def _payload(value):
    if isinstance(value, str):
        return json.loads(value)
    return getattr(value, "obj", value)


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection

    def fetchone(self):
        return self._connection.fetchone()


class FakeConnection:
    """Evaluate the admission SELECT from the parameters the repository sends."""

    def __init__(self):
        self.exports = []
        self.jobs = []
        self.next_id = 0

    def execute(self, statement, params=None):
        self._pending = (statement, tuple(params or ()))
        return FakeCursor(self)

    def fetchone(self):
        statement, params = self._pending
        if "pg_advisory" in statement:
            return (True,)
        if "FROM projects WHERE id=%s" in statement:
            return (1,)
        if "INSERT INTO exports" in statement:
            self.next_id += 1
            self.exports.append(
                {
                    "id": self.next_id,
                    "project_id": params[0],
                    "format": params[1],
                    "options": _payload(params[2]),
                }
            )
            return (self.next_id,)
        if "INSERT INTO jobs" in statement:
            self.next_id += 1
            self.jobs.append(
                {
                    "id": self.next_id,
                    "project_id": params[0],
                    "kind": params[1],
                    "status": params[2],
                    "run_id": params[5],
                    "export_id": _payload(params[4])["export_id"],
                }
            )
            return (self.next_id,)
        if "JOIN exports e" in statement:
            # The admission query owns the dedup identity: without an options
            # parameter every in-flight export of the format is a match.
            wanted = _payload(params[2]) if len(params) > 2 else None
            for job in reversed(self.jobs):
                if job["project_id"] != params[0] or job["kind"] != "export":
                    continue
                if job["status"] not in ("queued", "running"):
                    continue
                export = next(item for item in self.exports if item["id"] == job["export_id"])
                if export["format"] != params[1]:
                    continue
                if wanted is not None and export["options"] != wanted:
                    continue
                return (job["id"], job["run_id"], export["id"])
            return None
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakePool:
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        return self._connection


def _admission():
    connection = FakeConnection()
    return PostgresExports(SimpleNamespace(pool=FakePool(connection))), connection


def test_bilingual_export_is_not_merged_into_monolingual_admission():
    exports, _ = _admission()
    plain = {
        "bilingual": False,
        "order": "target_first",
        "about_page": True,
        "preserve_source_style": False,
    }
    bilingual = dict(plain, bilingual=True)

    first = exports.admit_export("p1", "docx", plain, "run-1", {})
    second = exports.admit_export("p1", "docx", bilingual, "run-2", {})

    assert second[0] != first[0]


def test_identical_in_flight_export_stays_idempotent():
    exports, _ = _admission()
    options = {
        "bilingual": True,
        "order": "source_first",
        "about_page": True,
        "preserve_source_style": False,
    }

    first = exports.admit_export("p1", "docx", options, "run-1", {})
    again = exports.admit_export("p1", "docx", dict(options), "run-2", {})

    assert again == first
