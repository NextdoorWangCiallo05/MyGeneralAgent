"""技能接口：列出当前可用的技能（内置 + 自定义）。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser  # noqa: F401 - 统一鉴权依赖
from app.schemas.skill import SkillListResponse, SkillPublic
from app.skills import get_skill_registry

router = APIRouter(tags=["skills"])


@router.get("/skills", response_model=SkillListResponse, summary="列出可用技能")
async def list_skills(_: CurrentUser) -> SkillListResponse:
    """返回技能 id / 展示名 / 说明 / 工具范围，供前端渲染选择器。

    技能在进程启动时装配一次，修改 `SKILLS_DIR` 里的文件后需重启服务。
    """
    skills = get_skill_registry().public_list()
    return SkillListResponse(
        items=[SkillPublic(**item) for item in skills],
        total=len(skills),
    )
