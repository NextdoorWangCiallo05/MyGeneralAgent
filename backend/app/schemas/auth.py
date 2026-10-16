"""认证相关请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.security import MAX_PASSWORD_BYTES, MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH


def _check_password(value: str) -> str:
    if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"密码长度不能超过 {MAX_PASSWORD_BYTES} 字节")
    return value


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        return _check_password(value)


class LoginRequest(BaseModel):
    # 支持用户名或邮箱登录
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserPublic"


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    email: str
    role: str
    sandbox_enabled: bool
    email_verified: bool
    status: str
    created_at: datetime


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=8, max_length=512)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)

    @field_validator("new_password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        return _check_password(value)


class MessageResponse(BaseModel):
    message: str


TokenResponse.model_rebuild()
