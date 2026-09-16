"""Interactive paragraph retranslation while the main book workflow keeps running."""

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from ..project_service import require_book, require_project
from ..retranslation_service import create_request, requests_for, set_result
from ..schemas import RetranslationInput, RetranslationOut
from ..workers import enqueue

router = APIRouter(tags=["retranslation"])


@router.get("/projects/{pid}/retranslations", response_model=list[RetranslationOut])
def list_retranslations(pid: str):
    require_book(require_project(pid))
    return requests_for(pid)


@router.post(
    "/projects/{pid}/chapters/{ci}/retranslate", response_model=RetranslationOut, status_code=202
)
async def retranslate(pid: str, ci: int, body: RetranslationInput):
    request = await run_in_threadpool(create_request, pid, ci, body.segment_indices)
    rid = request["id"]
    try:
        job = await enqueue("run_retranslation", project_id=pid, request_id=rid, _job_id=rid)
        if job is None:
            raise RuntimeError("queue did not accept task")
    except Exception as error:
        await run_in_threadpool(set_result, pid, rid, "error", error="任务队列不可用，请重新提交")
        raise HTTPException(503, "任务队列不可用，请重新提交") from error
    return next(r for r in await run_in_threadpool(requests_for, pid) if r["id"] == rid)
