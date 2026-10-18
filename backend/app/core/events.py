"""SSE 事件定义（与《技术设计文档》6.2 节事件表一致）。

事件类型：
    start / thinking / thinking_end / tool_call / tool_confirm / tool_result / text / usage /
    done / error

其中 `tool_confirm` 表示「该工具需要人工确认」，前端应提示用户后调用
`POST /chat/confirm` 唤醒；Agent 会保持本次流连接挂起等待（见 core.confirmations）。

`thinking_end` 表示「该轮推理内容输出完毕」（第一个非 reasoning 分片到达时补发），
前端据此把「思考过程」自动收起；只吐正文的普通回答不会出现该事件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    START = "start"
    THINKING = "thinking"
    THINKING_END = "thinking_end"
    TOOL_CALL = "tool_call"
    TOOL_CONFIRM = "tool_confirm"
    TOOL_RESULT = "tool_result"
    TEXT = "text"
    USAGE = "usage"
    DONE = "done"
    ERROR = "error"


@dataclass(slots=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        """序列化为 SSE 帧。"""
        payload = json.dumps(self.data, ensure_ascii=False, default=str)
        return f"event: {self.type}\ndata: {payload}\n\n"

    # -------------------------------------------------- 工厂方法
    @classmethod
    def start(cls, *, message_id: str, session_id: str, model: str = "") -> "Event":
        # model 是本轮实际使用的模型名（空 = 服务端默认），前端据此同步「思考模式」开关状态
        return cls(
            EventType.START.value,
            {"message_id": message_id, "session_id": session_id, "model": model},
        )

    @classmethod
    def thinking(cls, delta: str) -> "Event":
        return cls(EventType.THINKING.value, {"delta": delta})

    @classmethod
    def thinking_end(cls) -> "Event":
        """本轮推理内容输出完毕（无 data，仅作阶段分隔）。"""
        return cls(EventType.THINKING_END.value)

    @classmethod
    def text(cls, delta: str) -> "Event":
        return cls(EventType.TEXT.value, {"delta": delta})

    @classmethod
    def tool_call(cls, call_id: str, name: str, args: dict[str, Any]) -> "Event":
        return cls(EventType.TOOL_CALL.value, {"id": call_id, "name": name, "args": args})

    @classmethod
    def tool_confirm(
        cls, call_id: str, name: str, args: dict[str, Any], expires_in: float
    ) -> "Event":
        """请求用户确认执行某个副作用工具；`expires_in` 为等待秒数。"""
        return cls(
            EventType.TOOL_CONFIRM.value,
            {"id": call_id, "name": name, "args": args, "expires_in": expires_in},
        )

    @classmethod
    def tool_result(cls, call_id: str, status: str, summary: str) -> "Event":
        return cls(EventType.TOOL_RESULT.value, {"id": call_id, "status": status, "summary": summary})

    @classmethod
    def usage(cls, prompt_tokens: int, completion_tokens: int) -> "Event":
        return cls(
            EventType.USAGE.value,
            {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        )

    @classmethod
    def done(cls, finish_reason: str = "stop") -> "Event":
        return cls(EventType.DONE.value, {"finish_reason": finish_reason})

    @classmethod
    def error(cls, code: str, message: str) -> "Event":
        return cls(EventType.ERROR.value, {"code": code, "message": message})
