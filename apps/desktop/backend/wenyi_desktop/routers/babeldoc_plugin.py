"""Private Desktop plugin controls, separate from portable workflow configuration."""

from fastapi import APIRouter, Depends, HTTPException, Request

from .credentials import desktop_only

router = APIRouter(
    prefix="/desktop/plugins/babeldoc",
    dependencies=[Depends(desktop_only)],
    include_in_schema=False,
)


@router.get("")
async def status(request: Request) -> dict:
    return request.app.state.babeldoc_plugin.status()


@router.post("/enable", status_code=202)
async def enable(request: Request) -> dict:
    plugin = request.app.state.babeldoc_plugin
    if not plugin.target:
        raise HTTPException(409, "BabelDOC is not available for this platform")
    await plugin.enable()
    return plugin.status()


@router.post("/disable")
async def disable(request: Request) -> dict:
    guard = request.app.state.update_guard
    # The same admission boundary protects disabling and native application updates.
    # It prevents new jobs from slipping in while we check and stop the plugin.
    await guard.prepare(allow_plugin_activity=True)
    try:
        await request.app.state.babeldoc_plugin.disable()
        return request.app.state.babeldoc_plugin.status()
    finally:
        await guard.cancel(allow_plugin_activity=True)
