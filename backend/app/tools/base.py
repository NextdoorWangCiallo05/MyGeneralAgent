"""工具基类与执行结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(slots=True)
class ToolResult:
    """统一工具执行结果。"""

    status: str  # success | error | timeout | needs_confirmation | rejected
    summary: str
    content: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "success"


@dataclass(slots=True)
class Tool:
    """工具定义。

    - `description` 直接决定 LLM 能否选对工具，需写得具体；
    - `parameters` 为 JSON Schema；
    - `requires_confirmation` 为 True 时需人工确认后才执行（副作用类）；
    - `sandbox_required` 为 True 时仅在沙箱开启时可用，未开启则不下发给模型；
    - `knowledge_required` 为 True 时仅在知识库可检索（已启用向量化且存在已向量化片段）时下发；
    - `attachment_required` 为 True 时仅在本会话存在附件时下发（避免模型调用注定失败的工具）。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[ToolResult]]
    requires_confirmation: bool = False
    readonly: bool = True
    sandbox_required: bool = False
    knowledge_required: bool = False
    attachment_required: bool = False
    timeout: float = 30.0

    def to_openai_schema(self) -> dict[str, Any]:
        """导出为 OpenAI 兼容的 tools 参数元素。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
