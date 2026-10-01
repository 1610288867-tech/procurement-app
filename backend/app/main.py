"""FastAPI 应用入口。

M0 阶段这里只有健康检查；M1 起开始挂业务路由。

三个健康检查端点，用途不同：

| 端点 | 作用 | 依赖数据库？ |
| --- | --- | --- |
| `/health/live` | 进程活着（总是 200） | 否 |
| `/health/ready` | 依赖就绪（数据库能连） | 是 |
| `/health/db` | 打印数据库版本，排查用 | 是 |

统一错误体与 request_id 也在这里定死（见 errors.py 的说明）。
"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.database import SessionFactory
from app.errors import BusinessError

settings = get_settings()

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
)
logger = logging.getLogger("procurement")

app = FastAPI(
    title="企业采购审批系统",
    description="个人学习项目：动态多级审批 / 预算并发预留 / 三单匹配",
    version="0.1.0",
)


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """给每个请求打上 request_id，并记录一条结构化访问日志。

    request_id 同时进响应头和错误体：出错时你在日志里搜这 12 个字符
    就能还原整条请求链路。
    """
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id

    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000

    response.headers["X-Request-Id"] = request_id
    logger.info(
        "request method=%s path=%s status=%s request_id=%s duration_ms=%.1f",
        request.method,
        request.url.path,
        response.status_code,
        request_id,
        duration_ms,
    )
    return response


@app.exception_handler(BusinessError)
async def business_error_handler(request: Request, exc: BusinessError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": getattr(request.state, "request_id", None),
            }
        },
    )


@app.get("/health/live", tags=["健康检查"])
async def live() -> dict[str, str]:
    """存活检查：只要进程在就返回 ok，不碰数据库。"""
    return {"status": "ok"}


@app.get("/health/ready", tags=["健康检查"])
async def ready() -> dict[str, object]:
    """就绪检查：数据库连不上就返回 503。"""
    try:
        async with SessionFactory() as session:
            await session.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 - 就绪检查要吞掉所有异常并如实上报
        logger.warning("ready check failed: %s", exc)
        return JSONResponse(  # type: ignore[return-value]
            status_code=503,
            content={"status": "unavailable", "database": "down"},
        )
    return {"status": "ok", "database": "up"}


@app.get("/health/db", tags=["健康检查"])
async def db_version() -> dict[str, str]:
    """打印数据库与 pgvector 版本，排查环境问题用。"""
    async with SessionFactory() as session:
        version = (await session.execute(text("select version()"))).scalar_one()
        vector = (
            await session.execute(
                text(
                    "select default_version from pg_available_extensions "
                    "where name = 'vector'"
                )
            )
        ).scalar_one_or_none()
    return {"postgres": str(version), "pgvector": str(vector)}
