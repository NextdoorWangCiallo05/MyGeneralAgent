"""技能的公开视图（不含完整指令文本）。"""

from __future__ import annotations

from pydantic import BaseModel


class SkillPublic(BaseModel):
    """前端技能选择器用的最小信息。"""

    id: str
    name: str
    description: str = ""
    #: 该技能限定的工具名；空列表 = 不限制
    tools: list[str] = []
    #: 来源：builtin / file / 自定义 Provider 的名字
    source: str = "builtin"


class SkillListResponse(BaseModel):
    items: list[SkillPublic] = []
    total: int = 0
