"""LLM 抽象接口与统一数据模型。

上层（Agent）只依赖本模块的 `LLMClient` 协议与 `ChatChunk`，
后续接入其它 OpenAI 兼容模型时只需新增实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Protocol, runtime_checkable


class ChunkKind(str, Enum):
    """流式分片类型。"""

    CONTENT = "content"        # 正文增量
    REASONING = "reasoning"    # 推理增量（reasoning_content）
    TOOL_CALLS = "tool_calls"  # 完整的工具调用（聚合后一次性下发）
    USAGE = "usage"            # token 用量
    DONE = "done"              # 本轮流结束


@dataclass(slots=True)
class ChatChunk:
    kind: str
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


class LLMNotConfiguredError(RuntimeError):
    """未配置 API-Key。"""


class LLMRequestError(RuntimeError):
    """模型请求失败（网络 / 非 2xx / 协议异常）。"""


@runtime_checkable
class LLMClient(Protocol):
    """LLM 客户端协议。"""

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[ChatChunk]:
        ...
