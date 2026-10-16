"""对话附件相关响应模型。

`AttachmentPublic` 用于上传响应与消息里的附件元数据；
`AttachmentDetail` 额外带上解析后的正文，供前端「查看原文」。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AttachmentPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    #: 归一化后的扩展名，如 txt / md / pdf
    file_type: str
    #: 浏览器上报的 MIME 类型，仅作展示
    content_type: str = ""
    size_bytes: int = 0
    #: 解析出的正文字符数（送模型时会按上限截断）
    char_count: int = 0
    created_at: datetime
    #: 是否已随某条消息发出（未发出的会在 TTL 后被清理）
    bound: bool = False


class AttachmentDetail(AttachmentPublic):
    """附件详情：比列表多一段解析后的纯文本。"""

    text: str = ""
    #: 正文是否因超出 `attachment_max_chars` 被截断
    truncated: bool = False


__all__ = ["AttachmentDetail", "AttachmentPublic"]
