"""FastAPI 应用入口。"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app import __version__
from app.api import (
    admin,
    attachments,
    auth,
    chat,
    knowledge,
    runtime,
    sessions,
    skills,
)
from app.config import get_settings
from app.core.errors import AppError
from app.core.logging import new_request_id, set_request_id, setup_logging
from app.core.ratelimit import RateLimitMiddleware, get_rate_limiter
from app.core.runtime_config import get_runtime_config
from app.core.security import hash_password
from app.core.tracing import flush_tracer, get_tracer
from app.db import dispose_engine, init_models
from app.llm import LLMNotConfiguredError, resolve_llm_config
from app.store import get_store

logger = logging.getLogger("app")

#: 允许前端读取的响应头（限流重试时间、请求号，便于前端提示与排障）
EXPOSED_HEADERS = [
    "X-Request-ID",
    "Retry-After",
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
]


async def _seed_admin() -> None:
    """按需创建初始管理员（仅当配置了 ADMIN_EMAIL + ADMIN_PASSWORD）。"""
    settings = get_settings()
    if not settings.admin_email or not settings.admin_password:
        return
    store = get_store()
    if await store.get_user_by_email(settings.admin_email):
        return
    user = await store.create_user(
        username=settings.admin_username or "admin",
        email=settings.admin_email,
        password_hash=hash_password(settings.admin_password),
        role="admin",
    )
    user.email_verified = True
    logger.info("已创建初始管理员: %s", user.username)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """给每个请求分配 request_id：写进日志上下文、回传 `X-Request-ID`、记录访问日志。

    同时兜住**未捕获异常**（如数据库报错），统一转成 JSON 500 信封后再往下走。
    不这么做的话，异常会冒泡到 Starlette 最外层的 ServerErrorMiddleware，
    而那个响应**不经过 CORSMiddleware**：浏览器因缺少 `Access-Control-Allow-Origin`
    读不到任何 body，前端只能把「服务端 500」显示成「无法连接后端服务，请确认后端已启动」，
    排查方向被彻底带偏。
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        incoming = (request.headers.get("x-request-id") or "").strip()
        request_id = incoming[:64] if incoming else new_request_id()
        set_request_id(request_id)
        request.state.request_id = request_id

        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "%s %s 处理失败",
                request.method,
                request.url.path,
                extra={"method": request.method, "path": request.url.path},
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "code": "INTERNAL_ERROR",
                    "message": "服务内部错误，请稍后重试（详情见后端日志）",
                },
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        response.headers.setdefault("X-Request-ID", request_id)
        logger.info(
            "%s %s -> %s (%dms)",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            extra={
                "method": request.method,
                "path": request.url.path,
                "http_status": response.status_code,
                "duration_ms": duration_ms,
                "ip": get_rate_limiter().client_ip(request),
            },
        )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    use_sql = settings.store_backend.strip().lower() == "sql"

    if use_sql and settings.auto_create_tables:
        await init_models()
        logger.info("已按 ORM 元数据自动建表（AUTO_CREATE_TABLES=true）")

    await _seed_admin()

    try:
        llm = resolve_llm_config(await get_runtime_config().effective_settings())
    except LLMNotConfiguredError as exc:
        logger.warning("%s，/chat 接口将返回 API_KEY_NOT_CONFIGURED", exc)
    else:
        if llm.requires_api_key and not llm.api_key:
            logger.warning(
                "未配置 %s 的 API-Key，/chat 接口将返回 API_KEY_NOT_CONFIGURED", llm.label
            )
        logger.info("模型接入: %s（模型 %s）", llm.label, llm.model)
    logger.info("存储后端: %s", "sql" if use_sql else "memory")

    # M5：启动时把关键运行状态打出来，便于确认生产配置是否符合预期
    limiter = get_rate_limiter()
    logger.info(
        "限流: %s（IP %s 次/%ss，认证 %s 次，对话 %s 次，上传 %s 次）",
        "开启" if limiter.enabled else "关闭",
        settings.rate_limit_ip_per_minute,
        settings.rate_limit_window_seconds,
        settings.rate_limit_auth_per_minute,
        settings.rate_limit_chat_per_minute,
        settings.rate_limit_upload_per_minute,
    )
    tracer = get_tracer()
    logger.info(
        "链路追踪: %s",
        "、".join(tracer.sink_names) if tracer.enabled else "未启用（配置 LANGFUSE_* 或 OBSERVABILITY_LOG_SPANS=true 可开启）",
    )
    yield

    flush_tracer()
    if use_sql:
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    # 中间件按「后添加的在外层」叠加，顺序：CORS → 请求上下文 → 限流。
    # 这样限流直接返回的 429 也能带上 request_id 与 CORS 头。
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=EXPOSED_HEADERS,
    )

    # ------------------------------------------------------------ 异常处理
    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=exc.to_dict(),
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        message = first.get("msg", "请求参数不合法")
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": f"参数校验失败：{location} {message}".strip(),
            },
        )

    # ------------------------------------------------------------ 路由
    prefix = settings.api_prefix
    app.include_router(auth.router, prefix=prefix)
    app.include_router(sessions.router, prefix=prefix)
    app.include_router(chat.router, prefix=prefix)
    app.include_router(runtime.router, prefix=prefix)
    app.include_router(skills.router, prefix=prefix)
    app.include_router(knowledge.router, prefix=prefix)
    app.include_router(attachments.router, prefix=prefix)
    app.include_router(admin.router, prefix=prefix)

    @app.get(f"{prefix}/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
