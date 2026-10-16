"""限流（M5，《技术设计文档》9.2 资源滥用）。

设计取舍：

- **进程内滑动窗口计数，不引入 Redis**：单实例部署下足够（NFR：单实例 ≥100 并发），
  且与项目「零外部依赖即可跑起来」的基调一致；多实例部署需把计数换成 Redis，
  只要替换 `SlidingWindowLimiter`，上层 `RateLimiter` 的接口不变（见 README「已知简化」）。
- **fail-open**：限流器自身异常一律放行，限流故障不应让整个服务不可用。
- 命中维度（scope）：
  - `ip`：所有接口的全局兜底，防单 IP 刷接口；
  - `auth`：登录 / 注册 / 忘记密码 / 重置密码，防撞库、防邮件轰炸、防账号枚举；
  - `chat`：按登录用户，模型调用是主要成本项；
  - `upload`：按登录用户，文档解析与向量化有明显的 CPU 开销。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import Settings, get_settings
from app.core.errors import RateLimitedError

logger = logging.getLogger("app.ratelimit")


@dataclass(frozen=True)
class RateLimitRule:
    """一个维度的配额：`window` 秒内最多 `limit` 次。"""

    scope: str
    limit: int
    window: float = 60.0


@dataclass(frozen=True)
class Decision:
    """限流判定结果，同时用于生成响应头。"""

    scope: str
    allowed: bool
    limit: int
    remaining: int
    retry_after: int = 0
    window: int = 60

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(self.remaining, 0)),
            "X-RateLimit-Reset": str(self.retry_after or self.window),
        }
        if not self.allowed:
            headers["Retry-After"] = str(max(self.retry_after, 1))
        return headers


@dataclass
class SlidingWindowLimiter:
    """基于时间戳队列的滑动窗口限流器（单进程内有效）。"""

    max_keys: int = 20_000
    _hits: dict[str, deque[float]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_gc: float = 0.0

    async def hit(self, key: str, *, limit: int, window: float) -> tuple[int, int]:
        """记一次命中，返回 (窗口内命中数, 距可再次请求的秒数)。"""
        now = time.monotonic()
        async with self._lock:
            self._maybe_gc(now)
            bucket = self._hits.get(key)
            if bucket is None:
                bucket = deque()
                self._hits[key] = bucket

            cutoff = now - window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= limit:
                retry_after = max(bucket[0] + window - now, 0.0)
                return len(bucket), math.ceil(retry_after) or 1

            bucket.append(now)
            return len(bucket), 0

    def _maybe_gc(self, now: float) -> None:
        """偶尔清理已过期的空桶，避免字典无限增长。"""
        if len(self._hits) < self.max_keys or now - self._last_gc < 60:
            return
        self._last_gc = now
        for key in [k for k, v in self._hits.items() if not v]:
            self._hits.pop(key, None)

    async def reset(self) -> None:
        async with self._lock:
            self._hits.clear()


class RateLimiter:
    """把配置里的配额翻译成判定逻辑，并提供 fail-open 的 `enforce`。"""

    def __init__(self, settings: Settings | None = None, *, limiter: SlidingWindowLimiter | None = None) -> None:
        self._settings = settings or get_settings()
        self._limiter = limiter or SlidingWindowLimiter()

    # ---------------------------------------------------------------- 规则
    @property
    def enabled(self) -> bool:
        return bool(self._settings.rate_limit_enabled)

    def rule(self, scope: str) -> RateLimitRule | None:
        window = float(self._settings.rate_limit_window_seconds)
        limits = {
            "ip": self._settings.rate_limit_ip_per_minute,
            "auth": self._settings.rate_limit_auth_per_minute,
            "chat": self._settings.rate_limit_chat_per_minute,
            "upload": self._settings.rate_limit_upload_per_minute,
        }
        limit = limits.get(scope)
        if not limit or limit <= 0:
            return None
        return RateLimitRule(scope=scope, limit=int(limit), window=window)

    # ---------------------------------------------------------------- 判定
    async def check(self, scope: str, key: str) -> Decision | None:
        """返回判定结果；未启用或该维度未配置配额时返回 None（视为放行）。"""
        if not self.enabled or not key:
            return None
        rule = self.rule(scope)
        if rule is None:
            return None

        count, retry_after = await self._limiter.hit(
            f"{scope}:{key}", limit=rule.limit, window=rule.window
        )
        allowed = retry_after == 0
        return Decision(
            scope=scope,
            allowed=allowed,
            limit=rule.limit,
            remaining=0 if not allowed else max(rule.limit - count, 0),
            retry_after=retry_after,
            window=int(rule.window),
        )

    async def enforce(self, scope: str, key: str) -> Decision | None:
        """超限时抛 `RateLimitedError`（带 Retry-After 头），否则返回判定结果。"""
        try:
            decision = await self.check(scope, key)
        except Exception:  # noqa: BLE001 - 限流器异常一律放行，不影响业务
            logger.exception("限流判定异常，已放行: scope=%s", scope)
            return None

        if decision is None or decision.allowed:
            return decision

        logger.warning(
            "触发限流: scope=%s key=%s limit=%s",
            scope,
            _mask(key),
            decision.limit,
            extra={"scope": scope, "limit": decision.limit, "retry_after": decision.retry_after},
        )
        raise RateLimitedError(
            f"请求过于频繁，请 {decision.retry_after} 秒后再试",
            headers=decision.headers(),
            details={"scope": scope, "retry_after": decision.retry_after},
        )

    # ---------------------------------------------------------------- 工具
    def client_ip(self, request: Request) -> str:
        """取客户端 IP；部署在可信反向代理后时按 `RATE_LIMIT_TRUST_PROXY=true` 取 XFF 首段。"""
        if self._settings.rate_limit_trust_proxy:
            forwarded = request.headers.get("x-forwarded-for") or ""
            first = forwarded.split(",")[0].strip()
            if first:
                return first
        return request.client.host if request.client else "unknown"

    async def reset(self) -> None:
        await self._limiter.reset()


def _mask(key: str) -> str:
    """日志里不完整打印 key（IP / 用户 ID）。"""
    return key if len(key) <= 8 else f"{key[:4]}***"


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter


def reset_rate_limiter(limiter: RateLimiter | None = None) -> None:
    """替换（或清空）全局限流器，供测试使用。"""
    global _limiter
    _limiter = limiter


#: 免限流路径（健康检查、接口文档、静态资源）
EXEMPT_SUFFIXES = ("/health", "/openapi.json", "/docs", "/redoc")
#: 走严格配额（auth scope）的接口，其余按 IP 全局限流
AUTH_SUFFIXES = (
    "/auth/login",
    "/auth/register",
    "/auth/forgot-password",
    "/auth/reset-password",
)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """按 IP 的全局兜底限流；认证类接口单独用更严格的配额。"""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        limiter = get_rate_limiter()
        path = request.url.path
        if (
            not limiter.enabled
            or request.method == "OPTIONS"
            or any(path.endswith(suffix) for suffix in EXEMPT_SUFFIXES)
        ):
            return await call_next(request)

        scope = "auth" if any(path.endswith(s) for s in AUTH_SUFFIXES) else "ip"
        ip = limiter.client_ip(request)
        decision = None
        try:
            decision = await limiter.check(scope, ip)
        except Exception:  # noqa: BLE001 - 同上，限流异常不影响业务
            logger.exception("限流判定异常，已放行: scope=%s", scope)

        if decision is not None and not decision.allowed:
            logger.warning(
                "触发限流: scope=%s path=%s ip=%s",
                scope,
                path,
                _mask(ip),
                extra={
                    "scope": scope,
                    "path": path,
                    "ip": _mask(ip),
                    "method": request.method,
                    "retry_after": decision.retry_after,
                },
            )
            return JSONResponse(
                status_code=429,
                content={
                    "code": RateLimitedError.code,
                    "message": f"请求过于频繁，请 {decision.retry_after} 秒后再试",
                    "details": {"scope": scope, "retry_after": decision.retry_after},
                },
                headers=decision.headers(),
            )

        response = await call_next(request)
        if decision is not None:
            for name, value in decision.headers().items():
                response.headers.setdefault(name, value)
        return response
