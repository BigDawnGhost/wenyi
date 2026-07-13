"""FastAPI 装配：挂载路由、初始化 DB 池、可选静态 Token、CORS。

启动：``uvicorn wenyi_api.main:app --reload``
OpenAPI：``/docs`` 或 ``/openapi.json``（前端类型同步的单一事实来源）。
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from .config import settings
from .db import init_pool
from .routers import (
    chapters,
    events,
    export,
    glossary,
    health,
    projects,
    review,
    strategies,
    style,
    ws,
)


def create_app() -> FastAPI:
    app = FastAPI(
        title="文译 (Wenyi) API",
        version="0.2.0",
        description="基于 AI 的长篇小说翻译平台 — Web API（FastAPI + Postgres + Arq）",
    )

    @app.on_event("startup")
    def _startup() -> None:
        init_pool(settings.psycopg_dsn)

    # CORS：开发期前端独立端口直连；生产可收紧。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.environ.get("WENYI_CORS_ORIGINS", "*").split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 可选静态 Token 鉴权（v1 无用户系统）：仅校验 HTTP 请求，放行健康检查与 WebSocket。
    if settings.api_token:
        from fastapi import Request
        from fastapi.responses import JSONResponse
        from starlette.middleware.base import BaseHTTPMiddleware

        token = settings.api_token

        class _TokenMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                path = request.url.path
                if path.startswith("/health") or path.startswith("/ws/"):
                    return await call_next(request)
                auth = request.headers.get("authorization", "")
                provided = auth.removeprefix("Bearer ").strip()
                if provided != token:
                    return JSONResponse({"detail": "invalid api token"}, status_code=401)
                return await call_next(request)

        app.add_middleware(_TokenMiddleware)

    app.include_router(health.router)
    app.include_router(strategies.router)
    app.include_router(projects.router)
    app.include_router(chapters.router)
    app.include_router(glossary.router)
    app.include_router(review.router)
    app.include_router(style.router)
    app.include_router(export.router)
    app.include_router(events.router)
    app.include_router(ws.router)

    return app


app = create_app()


def _custom_openapi():  # 让 OpenAPI 标题更友好（可选）
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title="文译 (Wenyi) API",
        version="0.2.0",
        description=app.description,
        routes=app.routes,
    )
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi  # type: ignore[assignment]
