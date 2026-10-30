"""Subtitle cue inspection and durable manual revisions."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from wenyi_core.srt.store import SrtRunStore

from ..project_service import project_write, require_project, storage_for
from ..schemas import SubtitleResult, TargetEdit

router = APIRouter(prefix="/projects/{pid}/subtitles", tags=["subtitles"])


def _require_subtitles(project: dict) -> None:
    if project.get("fmt") != "srt":
        raise HTTPException(422, "This operation is only available for subtitle projects")


@router.get("", response_model=SubtitleResult)
def get_subtitles(pid: str) -> dict:
    _require_subtitles(require_project(pid))
    storage = storage_for(pid)
    store = SrtRunStore(storage.run_dir, storage=storage)
    rows = store.load_cues()
    if not rows:
        preview = storage.read_artifact("subtitle_preview.json") or []
        rows = {str(row["index"]): row for row in preview}
    cues = []
    for key, row in sorted(rows.items(), key=lambda item: int(item[0])):
        timestamp = row.get("timestamp", "")
        start, separator, end = timestamp.partition("-->")
        cues.append(
            {
                "id": str(key),
                "index": int(row.get("index", key)),
                "timestamp": timestamp,
                "start": start.strip(),
                "end": end.strip() if separator else "",
                "source": row.get("source", ""),
                "target": row.get("target"),
                "status": row.get("status", "pending"),
            }
        )
    return {
        "cues": cues,
        "completed": sum(cue["status"] == "done" for cue in cues),
        "total": len(cues),
    }


@router.put("/{cueid}")
def edit_subtitle(pid: str, cueid: str, body: TargetEdit) -> dict:
    with project_write(pid) as (project, storage):
        _require_subtitles(project)
        store = SrtRunStore(storage.run_dir, storage=storage)
        with storage.state_lock():
            cues = store.load_cues()
            if cueid not in cues:
                raise HTTPException(404, "subtitle cue not found")
            before = cues[cueid].get("target")
            cues[cueid]["target"] = body.target
            cues[cueid]["status"] = "done" if body.target.strip() else "pending"
            store.save_cues(cues)
            # A cue may appear in multiple overlapping windows. Update every
            # cached response containing it so a subsequent resume cannot undo it.
            for key in storage.list_artifacts("srt/batches/"):
                batch = storage.read_artifact(key)
                translations = batch.get("translations") if isinstance(batch, dict) else None
                if isinstance(translations, dict) and cueid in translations:
                    if body.target.strip():
                        translations[cueid] = body.target
                    else:
                        translations.pop(cueid)
                    storage.write_artifact(key, batch)
            done = sum(row.get("status") == "done" for row in cues.values())
            store.update_manifest(
                done_count=done,
                cue_count=len(cues),
                status="done" if done == len(cues) else "interrupted",
            )
            storage.log_event(
                "manual_subtitle_edited", index=cueid, before=before, after=body.target
            )
    return {"ok": True, "id": cueid}
