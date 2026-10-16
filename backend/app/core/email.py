"""邮件发送（找回密码 / 邮箱验证）。

M1 提供两种后端：
    - console：仅把邮件内容打印到后端日志，便于本地开发（默认）
    - smtp：通过标准库 smtplib 在子线程中真实发送
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from app.config import Settings, get_settings

logger = logging.getLogger("app.email")


def build_reset_link(token: str) -> str:
    """拼接前端重置密码链接。"""
    base = get_settings().app_base_url.rstrip("/")
    return f"{base}/reset-password?token={token}"


async def send_email(to: str, subject: str, body: str) -> None:
    """异步发送邮件；失败不抛异常，避免影响主流程。"""
    settings = get_settings()
    if settings.email_backend.lower() == "smtp":
        try:
            await asyncio.to_thread(_send_smtp, settings, to, subject, body)
        except Exception:  # noqa: BLE001 - 邮件失败不应阻断业务
            logger.exception("邮件发送失败: to=%s subject=%s", to, subject)
    else:
        logger.info("[DEV EMAIL] to=%s\nsubject=%s\n%s", to, subject, body)


def _send_smtp(settings: Settings, to: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
        if settings.smtp_tls:
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(message)


async def send_reset_email(to: str, token: str) -> None:
    """发送密码重置邮件。"""
    settings = get_settings()
    link = build_reset_link(token)
    minutes = settings.reset_token_expire_minutes
    body = (
        "你好，\n\n"
        "我们收到了你的密码重置请求。请在有效期内点击下面的链接设置新密码：\n\n"
        f"{link}\n\n"
        f"该链接 {minutes} 分钟内有效，且只能使用一次。\n"
        "如果不是你本人操作，请忽略本邮件，你的密码不会发生变化。\n"
    )
    await send_email(to, "重置你的密码", body)
