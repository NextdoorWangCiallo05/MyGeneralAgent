"""技能（Skill）数据模型。

一个「技能」= 一段角色指令 + 一个工具范围，用来把通用助手切换成某个专用助手
（调研员 / 代码助手 / 文案）。它**不是**工具本身，也不执行代码，只影响两件事：

1. system 提示里追加「当前技能」段落（`instructions`）；
2. 下发给模型的工具清单被裁剪到 `tools`（留空表示不限制）。

技能的来源由 `SkillProvider` 决定（见 `app/skills/providers.py`），
所以同一份运行时既能用内置技能，也能用用户目录里的自定义技能。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: 技能 id 只允许这些字符：url 路径 / 请求体 / 文件名都要能安全承载
SKILL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Skill:
    """一个可选的技能包（不可变，注册后进程内共享）。"""

    #: 唯一标识，小写字母 / 数字 / `-` / `_`，如 `research`
    id: str
    #: 展示名，如「深度调研」
    name: str
    #: 一句话说明，前端下拉里显示
    description: str = ""
    #: 追加到 system 提示的角色指令（可以有 Markdown 结构）
    instructions: str = ""
    #: 允许使用的工具名；空元组 = 不限制（沿用会话当前可用工具）
    tools: tuple[str, ...] = ()
    #: 来源：builtin（内置）/ file（自定义目录）/ 自定义 Provider 自报的名字
    source: str = "builtin"

    def to_public(self) -> dict[str, Any]:
        """对外视图：不含完整指令文本（前端不需要，也避免上下文体积泄漏）。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tools": list(self.tools),
            "source": self.source,
        }

    @property
    def restricted_tools(self) -> frozenset[str] | None:
        """工具白名单；None 表示不限制。"""
        return frozenset(self.tools) if self.tools else None
