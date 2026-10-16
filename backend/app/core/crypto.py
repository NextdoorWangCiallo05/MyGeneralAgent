"""敏感配置的对称加解密。

`system_config` 表中的敏感项（如 DeepSeek API-Key）以**密文**入库，
密钥来自基础设施配置 `ENCRYPTION_KEY`（见《技术设计文档》11.3 节）。

设计要点：
- 采用 Fernet（AES-128-CBC + HMAC-SHA256），同时保证机密性与完整性；
- Fernet 要求 32 字节 urlsafe base64 密钥，而运维侧更习惯配置任意长度的随机串，
  因此这里统一用 SHA-256 派生，任意 `ENCRYPTION_KEY` 都可以直接使用；
- 解密失败（密钥变更 / 密文被篡改）返回 `None` 而非抛异常，
  由调用方按「未配置」处理，避免单个坏值导致整个服务不可用。
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

logger = logging.getLogger("app.crypto")

# 密文前缀：便于人工识别，也为后续算法平滑升级预留版本位
_PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    """由 `ENCRYPTION_KEY` 派生 Fernet 实例。"""
    secret = get_settings().encryption_key or ""
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_value(plain: str) -> str:
    """明文 → 带版本前缀的密文。"""
    token = _fernet().encrypt(plain.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def decrypt_value(cipher: str | None) -> str | None:
    """密文 → 明文；无法解密时返回 `None`。"""
    if not cipher:
        return None
    if not cipher.startswith(_PREFIX):
        # 只可能来自手工改库：原样返回更利于排障，同时留下告警
        logger.warning("检测到未加密的敏感配置值，建议通过管理接口重新写入")
        return cipher
    try:
        raw = _fernet().decrypt(cipher[len(_PREFIX) :].encode("ascii"))
    except (InvalidToken, ValueError):
        logger.error("敏感配置解密失败：ENCRYPTION_KEY 可能已变更，请在管理后台重新配置")
        return None
    return raw.decode("utf-8")


def is_encrypted(value: str | None) -> bool:
    """判断值是否为本模块产出的密文。"""
    return bool(value) and value.startswith(_PREFIX)


def mask_secret(value: str | None, *, keep: int = 4) -> str:
    """脱敏展示：保留首尾少量字符，中间以 `*` 代替。

    >>> mask_secret("sk-1234567890abcdef")
    'sk-1****cdef'
    """
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * 6}{value[-keep:]}"
