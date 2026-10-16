"""工具调用审计（M5，《技术设计文档》5.2 `tool_audit_logs`）。

审计诉求：谁、在哪个会话、让 Agent 调用了什么工具、参数是什么、结果如何、耗时多久、
是否经过用户确认。异常情况（工具报错、超时、被用户拒绝）同样要留痕，便于事后追责与排障。

约定：

- 写入失败只告警，绝不影响对话主流程（fail-open）；
- 参数与结果做**截断 + 敏感字段脱敏**后再落库，避免超长内容与密钥进库；
- `result` 只存摘要（工具返回给模型的是完整内容，审计只需要「做了什么、结果概况」）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.store.base import AbstractStore
from app.store.entities import AuditLog, new_id, utcnow

logger = logging.getLogger("app.audit")

#: 落库字段长度上限
MAX_ARGS_CHARS = 2000
MAX_SUMMARY_CHARS = 500
MAX_ERROR_CHARS = 500

#: 命中即脱敏的参数键（不区分大小写，按子串匹配）
SENSITIVE_KEYWORDS = ("password", "passwd", "secret", "token", "api_key", "apikey", "authorization", "cookie")

MASK = "***"


def sanitize(value: Any, *, depth: int = 0) -> Any:
    """脱敏 + 限制嵌套深度，避免把异常结构或密钥写进审计表。"""
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        return {
            str(key): (MASK if _is_sensitive(str(key)) else sanitize(item, depth=depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return value if len(value) <= 500 else f"{value[:500]}…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:200]


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(word in lowered for word in SENSITIVE_KEYWORDS)


def dump_args(args: Any) -> str:
    """把工具参数序列化成审计用文本。"""
    try:
        text = json.dumps(sanitize(args), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(args)
    return text[:MAX_ARGS_CHARS]


def dump_result(summary: str, error: str = "", extra: dict[str, Any] | None = None) -> str:
    """结果摘要序列化成 JSON 文本（status/duration_ms 另有独立列）。"""
    payload: dict[str, Any] = {"summary": (summary or "")[:MAX_SUMMARY_CHARS]}
    if error:
        payload["error"] = error[:MAX_ERROR_CHARS]
    if extra:
        payload["extra"] = sanitize(extra)
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps({"summary": str(summary)[:MAX_SUMMARY_CHARS]}, ensure_ascii=False)


def build_audit_log(
    *,
    user_id: str,
    session_id: str,
    tool_name: str,
    args: Any,
    status: str,
    summary: str = "",
    error: str = "",
    duration_ms: int = 0,
    approved: bool = False,
) -> AuditLog:
    return AuditLog(
        id=new_id(),
        user_id=user_id,
        session_id=session_id,
        tool_name=tool_name,
        args=dump_args(args),
        result=dump_result(summary, error),
        status=status,
        duration_ms=max(int(duration_ms), 0),
        approved=approved,
        created_at=utcnow(),
    )


async def record_tool_call(
    store: AbstractStore,
    *,
    user_id: str,
    session_id: str,
    tool_name: str,
    args: Any,
    status: str,
    summary: str = "",
    error: str = "",
    duration_ms: int = 0,
    approved: bool = False,
) -> AuditLog | None:
    """写一条审计记录；失败只告警（审计不可用不应中断对话）。"""
    entry = build_audit_log(
        user_id=user_id,
        session_id=session_id,
        tool_name=tool_name,
        args=args,
        status=status,
        summary=summary,
        error=error,
        duration_ms=duration_ms,
        approved=approved,
    )
    try:
        return await store.add_audit_log(entry)
    except Exception:  # noqa: BLE001
        logger.exception(
            "工具审计写入失败: session=%s tool=%s",
            session_id,
            tool_name,
            extra={"session_id": session_id, "tool": tool_name, "status": status},
        )
        return None
