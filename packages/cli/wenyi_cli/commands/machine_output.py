"""Atomic, versioned CLI receipts for external process supervisors."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from wenyi_core.atomicio import publish_bytes


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class MachineOutput:
    """Publish progress and receipts without exposing configuration or model responses."""

    def __init__(
        self,
        progress_path: str | None,
        result_path: str | None,
        *,
        source: str,
        config: str,
        output: str | None,
    ) -> None:
        self.progress_path = Path(progress_path).resolve() if progress_path else None
        self.result_path = Path(result_path).resolve() if result_path else None
        self.source = Path(source).resolve()
        self.run_id = uuid4().hex
        self.completed = False
        paths = [path for path in (self.progress_path, self.result_path) if path is not None]
        protected = {self.source, Path(config).resolve()}
        if output:
            protected.add(Path(output).resolve())
        if len(set(paths)) != len(paths) or protected.intersection(paths):
            raise ValueError("Machine output paths must be distinct from source, config and output")

    @property
    def enabled(self) -> bool:
        return self.progress_path is not None or self.result_path is not None

    def _write(self, path: Path | None, **data) -> None:
        if path is None:
            return
        payload = {
            "protocol_version": 1,
            "run_id": self.run_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        publish_bytes(path, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def start(self) -> None:
        # Replace an old receipt before preflight so it cannot masquerade as this run.
        self._write(self.result_path, status="running", outputs=[])
        self(0, 0, "Starting Wenyi")

    def __call__(self, done: int, total: int, label: str) -> None:
        self._write(
            self.progress_path,
            status="running",
            done=done,
            total=total if total > 0 else None,
            label=label,
        )

    def success(self, paths: list[str], state_dir: str) -> None:
        if not self.enabled:
            self.completed = True
            return
        outputs = []
        for name in paths:
            path = Path(name).resolve()
            outputs.append(
                {"path": str(path), "size": path.stat().st_size, "sha256": file_digest(path)}
            )
        self._write(
            self.result_path,
            status="success",
            source_sha256=file_digest(self.source),
            state_dir=str(Path(state_dir).resolve()),
            outputs=outputs,
        )
        self._write(self.progress_path, status="success", done=1, total=1, label="Completed")
        self.completed = True

    def failed(self) -> None:
        self._write(
            self.result_path,
            status="failed",
            outputs=[],
            error={"code": "translation_failed", "message": "See the Wenyi log for details."},
        )
        self._write(self.progress_path, status="failed", done=0, total=None, label="Stopped")
