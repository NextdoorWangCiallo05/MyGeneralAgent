"""认证接口：注册 / 登录 / 登出 / 当前用户 / 找回密码 / 重置密码。"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.api.deps import CurrentUser, Store
from app.config import Settings, get_settings
from app.core.email import send_reset_email
from app.core.errors import (
    InvalidCredentialsError,
    InvalidResetTokenError,
    UserExistsError,
)
from app.core.security import (
    create_access_token,
    generate_reset_token,
    hash_password,
    hash_reset_token,
    verify_password,
)
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserPublic,
)

logger = logging.getLogger("app.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

# 防用户枚举：无论邮箱是否存在，都返回同一句文案
_FORGOT_MESSAGE = "若该邮箱已注册，我们已发送重置邮件，请查收"


def _token_response(user, settings: Settings) -> TokenResponse:
    token = create_access_token(user.id, role=user.role)
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserPublic.model_validate(user),
    )


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(payload: RegisterRequest, store: Store) -> TokenResponse:
    """注册并直接返回登录态。"""
    settings = get_settings()
    if await store.get_user_by_username(payload.username):
        raise UserExistsError("用户名已被注册")
    if await store.get_user_by_email(payload.email):
        raise UserExistsError("邮箱已被注册")

    user = await store.create_user(
        username=payload.username,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role="user",
    )
    logger.info("新用户注册: %s", user.username)
    return _token_response(user, settings)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, store: Store) -> TokenResponse:
    """用户名或邮箱 + 密码登录。"""
    settings = get_settings()
    user = await store.get_user_by_login(payload.username)
    if user is None or not verify_password(payload.password, user.password_hash):
        raise InvalidCredentialsError()
    if not user.is_active:
        raise InvalidCredentialsError("账号已被禁用，请联系管理员")
    return _token_response(user, settings)


@router.post("/logout", response_model=MessageResponse)
async def logout(user: CurrentUser) -> MessageResponse:
    """JWT 无状态，登出由前端清除令牌即可。"""
    return MessageResponse(message="已登出")


@router.get("/me", response_model=UserPublic)
async def me(user: CurrentUser) -> UserPublic:
    return UserPublic.model_validate(user)


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(payload: ForgotPasswordRequest, store: Store) -> MessageResponse:
    """发送重置邮件；响应与邮箱是否存在无关。"""
    settings = get_settings()
    user = await store.get_user_by_email(str(payload.email))
    if user is not None and user.is_active:
        token = generate_reset_token()
        await store.create_reset_token(
            user_id=user.id,
            token_hash=hash_reset_token(token),
            ttl_minutes=settings.reset_token_expire_minutes,
        )
        await send_reset_email(user.email, token)
    else:
        logger.info("找回密码：邮箱未注册或已禁用 (%s)", payload.email)
    return MessageResponse(message=_FORGOT_MESSAGE)


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(payload: ResetPasswordRequest, store: Store) -> MessageResponse:
    """凭一次性令牌重置密码。"""
    record = await store.consume_reset_token(hash_reset_token(payload.token))
    if record is None:
        raise InvalidResetTokenError()

    await store.update_password(record.user_id, hash_password(payload.new_password))
    await store.invalidate_reset_tokens(record.user_id)
    logger.info("用户 %s 重置了密码", record.user_id)
    return MessageResponse(message="密码已重置，请使用新密码登录")
