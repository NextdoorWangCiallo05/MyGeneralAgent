"""依赖注入：当前用户、管理员校验、限流、仓储实例。"""

from __future__ import annotations

from typing import Annotated, Any

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.ratelimit import get_rate_limiter
from app.core.security import decode_access_token
from app.store import AbstractStore, User, get_store

_bearer_scheme = HTTPBearer(auto_error=False)


def store_dep() -> AbstractStore:
    return get_store()


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    store: Annotated[AbstractStore, Depends(store_dep)],
) -> User:
    """解析 Bearer Token 并返回当前用户。"""
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("缺少访问令牌")

    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("登录已过期，请重新登录") from exc
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("令牌无效") from exc

    user_id = payload.get("sub")
    user = await store.get_user(user_id) if user_id else None
    if user is None:
        raise UnauthorizedError("用户不存在")
    if not user.is_active:
        raise ForbiddenError("账号已被禁用")
    return user


async def require_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    if not user.is_admin:
        raise ForbiddenError("需要管理员权限")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
Store = Annotated[AbstractStore, Depends(store_dep)]


def rate_limited(scope: str) -> Any:
    """按登录用户限流的依赖工厂，用法：`Depends(rate_limited("chat"))`。

    中间件已经在按 IP 做全局兜底，这里针对「单个账号」再加一层，
    保护模型调用（chat）与文档解析（upload）这类高成本接口。
    """

    async def _dependency(user: CurrentUser) -> None:
        await get_rate_limiter().enforce(scope, user.id)

    return _dependency
