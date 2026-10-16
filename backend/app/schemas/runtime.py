"""运行时开关的公开视图（面向已登录用户，不含任何敏感项）。"""

from __future__ import annotations

from pydantic import BaseModel


class RuntimeConfigPublic(BaseModel):
    """前端据此决定沙箱开关是否可用。

    沙箱需要「管理员全局总闸」与「会话级开关」同时打开才生效，
    因此前端必须知道全局状态，否则用户开了开关却看不到任何效果。
    """

    sandbox_global_enabled: bool
    #: 全局开启时，沙箱内可用的工具名（便于前端提示能力范围）
    sandbox_tools: list[str] = []

    #: 是否启用了向量化（false 时知识库检索不可用）
    knowledge_enabled: bool = False
    #: 当前嵌入后端名，例如 hash / local:BAAI/bge-small-zh-v1.5 / none
    knowledge_backend: str = "none"
    #: 知识库检索工具名（启用时下发，便于前端提示）
    knowledge_tool: str | None = None
