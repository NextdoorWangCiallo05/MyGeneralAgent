"""对话与会话相关请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32_000)
    session_id: Optional[str] = None
    model: Optional[str] = None
    tools: Optional[list[str]] = None
    sandbox_enabled: Optional[bool] = None
    #: 重新生成：摘掉最后一轮后重跑，`message` 需为该轮的原提问文案
    regenerate: bool = False
    #: 随本条消息发送的附件 id（先调 `POST /api/v1/attachments` 上传拿到）。
    #: 这里只做传输层的防滥用上限，业务上限见 `attachment_max_per_message`
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)


class StopRequest(BaseModel):
    session_id: str


class ConfirmRequest(BaseModel):
    """对 SSE `tool_confirm` 事件的回应。"""

    session_id: str
    tool_call_id: str = Field(min_length=1, max_length=128)
    approved: bool


class SessionCreateRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=255)
    model: Optional[str] = None
    sandbox_enabled: bool = False


class SessionUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=255)
    sandbox_enabled: Optional[bool] = None


class SessionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    model: str
    sandbox_enabled: bool
    #: 较早轮次被压缩后的摘要（为空表示未压缩过）
    summary: str = ""
    created_at: datetime
    updated_at: datetime


class MessagePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    role: str
    content: str
    seq: int
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    #: 本条消息携带的附件元数据（id / 文件名 / 类型 / 大小），正文按需另取
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    created_at: datetime
