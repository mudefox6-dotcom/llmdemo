"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.core.auth import require_auth, require_auth_or_query_token

from app.api.routes_auth import router as auth_router
from app.api.routes_task import router as task_router
from app.api.routes_stream import router as stream_router
from app.core.task_queue import start_worker, stop_worker
from app.storage.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await start_worker()
    try:
        yield
    finally:
        await stop_worker()


app = FastAPI(
    title="Multi-Agent Requirement Delivery System",
    description="LangChain + LangGraph multi-agent delivery workflow.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(auth_router)
# 任务与流式接口都要求登录：部署到公网后，未认证的人不能创建任务消耗 LLM 额度。
# 认证可用 AUTH_ENABLED=false 关掉（仅限本地调试）。
app.include_router(task_router, dependencies=[Depends(require_auth)])
# /stream 用 EventSource 访问，浏览器不允许它带自定义头，所以额外接受 ?token=
app.include_router(stream_router, dependencies=[Depends(require_auth_or_query_token)])


@app.get("/health")
async def health():
    return {"status": "ok"}
