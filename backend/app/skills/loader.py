"""技能装配：内置 + 自定义来源，进程内单例。"""

from __future__ import annotations

import logging
from functools import lru_cache

from app.config import Settings, get_settings
from app.core.errors import SkillNotFoundError
from app.skills.base import Skill
from app.skills.builtin import BUILTIN_SKILLS
from app.skills.providers import FileSkillProvider
from app.skills.registry import SkillRegistry

logger = logging.getLogger("app.skills")


def build_skill_registry(settings: Settings | None = None) -> SkillRegistry:
    """按配置装配注册表：内置技能打底，自定义技能可覆盖同名 id。"""

    settings = settings or get_settings()
    registry = SkillRegistry()
    registry.register_many(BUILTIN_SKILLS)

    directory = (settings.skills_dir or "").strip()
    if directory:
        registry.register_many(FileSkillProvider(directory).load(), replace=True)
    return registry


@lru_cache(maxsize=1)
def get_skill_registry() -> SkillRegistry:
    """API 层与对话链路共用同一个注册表。

    技能在进程启动后按需装配一次；新增/修改技能文件后重启服务生效。
    测试里可用 `get_skill_registry.cache_clear()` 重建。
    """
    return build_skill_registry()


def resolve_skill(skill_id: str | None) -> Skill | None:
    """把请求里的 `skill_id` 解析成技能；空值 = 不使用技能。"""

    if skill_id is None:
        return None
    normalized = skill_id.strip()
    if not normalized:
        return None
    skill = get_skill_registry().get(normalized)
    if skill is None:
        raise SkillNotFoundError(f"技能不存在: {normalized}")
    return skill
