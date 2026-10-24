"""技能注册表：注册、查找、对外视图。"""

from __future__ import annotations

import logging
from typing import Iterable

from app.skills.base import SKILL_ID_PATTERN, Skill

logger = logging.getLogger("app.skills")


class SkillRegistry:
    """进程内技能表。

    与 `ToolRegistry` 刻意保持同构（register / register_many / get / list），
    但技能是**纯数据**，没有执行逻辑，因此不需要异步接口。
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    # ---------------------------------------------------------------- 注册
    def register(self, skill: Skill, *, replace: bool = False) -> None:
        if not SKILL_ID_PATTERN.match(skill.id):
            raise ValueError(
                f"技能 id 不合法: {skill.id!r}（只允许小写字母、数字、- 和 _，且以字母或数字开头）"
            )
        if skill.id in self._skills and not replace:
            raise ValueError(f"技能已存在: {skill.id}")
        if replace and skill.id in self._skills:
            logger.info("技能 %s 被同名自定义技能覆盖", skill.id)
        self._skills[skill.id] = skill

    def register_many(self, skills: Iterable[Skill], *, replace: bool = False) -> None:
        for skill in skills:
            self.register(skill, replace=replace)

    def unregister(self, skill_id: str) -> None:
        self._skills.pop(skill_id, None)

    # ---------------------------------------------------------------- 查询
    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)

    def list(self) -> list[Skill]:
        return list(self._skills.values())

    def public_list(self) -> list[dict[str, object]]:
        return [skill.to_public() for skill in self._skills.values()]

    def __len__(self) -> int:
        return len(self._skills)
