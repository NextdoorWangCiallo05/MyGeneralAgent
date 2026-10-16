"""工具执行上下文：用 ContextVar 传递调用者身份。

文件类与代码类工具需要知道「现在是谁在调用」才能定位其沙箱目录。
身份**不能**放进工具参数由模型生成（那等于把沙箱越权交给模型），
因此改由 Agent 在执行工具前通过 ContextVar 注入，模型无从伪造。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ToolContext:
    """一次工具调用所处的上下文。"""

    user_id: str
    session_id: str
    sandbox_enabled: bool = False


_context: ContextVar[ToolContext | None] = ContextVar("tool_context", default=None)


def set_tool_context(context: ToolContext) -> Token[ToolContext | None]:
    """绑定上下文，返回可用于还原的 Token。"""
    return _context.set(context)


def reset_tool_context(token: Token[ToolContext | None]) -> None:
    _context.reset(token)


def require_tool_context() -> ToolContext:
    """取当前上下文；缺失说明工具被绕过 Agent 直接调用，属编码错误。"""
    context = _context.get()
    if context is None:
        raise RuntimeError("工具执行上下文缺失，无法确定沙箱归属")
    return context


__all__ = [
    "ToolContext",
    "require_tool_context",
    "reset_tool_context",
    "set_tool_context",
]
