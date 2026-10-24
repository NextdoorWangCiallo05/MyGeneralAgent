"""技能模块：把「角色指令 + 工具范围」打包成可切换的专用助手。"""

from app.skills.base import Skill
from app.skills.builtin import BUILTIN_SKILLS
from app.skills.loader import build_skill_registry, get_skill_registry, resolve_skill
from app.skills.providers import FileSkillProvider, SkillProvider
from app.skills.registry import SkillRegistry

__all__ = [
    "BUILTIN_SKILLS",
    "FileSkillProvider",
    "Skill",
    "SkillProvider",
    "SkillRegistry",
    "build_skill_registry",
    "get_skill_registry",
    "resolve_skill",
]
