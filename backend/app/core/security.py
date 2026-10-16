"""安全工具：密码哈希、JWT 签发/校验、一次性令牌。"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.config import get_settings

# bcrypt 仅取密码前 72 字节；在入参层强制限制长度，避免静默截断带来的歧义
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 72


# ---------------------------------------------------------------- 密码哈希
def hash_password(password: str) -> str:
    """使用 bcrypt 加随机盐哈希密码。"""
    pwd = password.encode("utf-8")
    if len(pwd) > MAX_PASSWORD_BYTES:
        raise ValueError(f"密码长度不能超过 {MAX_PASSWORD_BYTES} 字节")
    return bcrypt.hashpw(pwd, bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码与哈希是否匹配（恒定不抛异常）。"""
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- JWT
def create_access_token(
    subject: str,
    *,
    role: str = "user",
    expires_minutes: int | None = None,
) -> str:
    """签发 Access Token，`sub` 为用户 ID。"""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expire_minutes = expires_minutes or settings.access_token_expire_minutes
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """解码并校验 Token，失败抛 `jwt.PyJWTError`。"""
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


# ---------------------------------------------------------------- 一次性令牌
def generate_reset_token() -> str:
    """生成不可预测的重置令牌明文（仅通过邮件下发，不落库）。"""
    return secrets.token_urlsafe(32)


def hash_reset_token(token: str) -> str:
    """重置令牌只存哈希，防止数据库泄露后被直接使用。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
