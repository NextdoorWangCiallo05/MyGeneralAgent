"""技能的定制化入口。

技能从哪里来由 `SkillProvider` 决定：

- 内置：`app/skills/builtin.py`（随代码发布）；
- 文件：`FileSkillProvider` 扫描 `SKILLS_DIR` 下的 Markdown（运维/团队自助扩展）；
- 其他：数据库、远程配置中心、插件包……实现同样的 `load()` 即可，
  在 `build_skill_registry()` 里挂上就能被 `/api/v1/skills` 与对话链路看到。

自定义 Provider 只需满足协议（鸭子类型即可，无需继承）：

```python
class MyProvider:
    name = "db"

    def load(self) -> Iterable[Skill]:
        for row in fetch_rows():
            yield Skill(id=row.id, name=row.name, instructions=row.prompt,
                        tools=tuple(row.tools), source=self.name)
```

约定：
- `load()` 只做读取，不修改外部状态；同一 id 的后加载者覆盖先加载者；
- 单个条目解析失败应当跳过并记日志，**不要**让整个注册表装配失败。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

from app.skills.base import SKILL_ID_PATTERN, Skill

logger = logging.getLogger("app.skills")


@runtime_checkable
class SkillProvider(Protocol):
    """技能来源协议：实现 `load()` 即可接入。"""

    #: 来源标识，会写进 `Skill.source`，方便前端区分内置 / 自定义
    name: str

    def load(self) -> Iterable[Skill]:
        """返回该来源提供的全部技能；允许分多次 yield。"""
        ...


class FileSkillProvider:
    """把目录里的 Markdown 当成技能包。

    每个 `*.md` 一个技能：文件名（去扩展名）就是技能 id，正文是指令，
    头部可选的 frontmatter 声明元数据：

    ```markdown
    ---
    name: 合同审阅
    description: 逐条核对合同条款风险
    tools: read_file, write_file
    ---

    你是资深合同律师，审阅时……
    ```

    - `tools` 逗号分隔（中英文逗号都行），留空 = 不限制工具；
    - `name` 缺省用 id，`description` 缺省为空；
    - 文件解析失败、id 非法都会跳过并打 warning，不影响其他技能。
    """

    name = "file"

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser()

    def load(self) -> list[Skill]:
        if not self.directory.is_dir():
            logger.warning("SKILLS_DIR 不存在，跳过自定义技能: %s", self.directory)
            return []

        skills: list[Skill] = []
        for path in sorted(self.directory.glob("*.md")):
            skill = self._load_one(path)
            if skill is not None:
                skills.append(skill)
        if skills:
            logger.info("从 %s 加载了 %d 个自定义技能", self.directory, len(skills))
        return skills

    def _load_one(self, path: Path) -> Skill | None:
        skill_id = path.stem.strip().lower()
        if not SKILL_ID_PATTERN.match(skill_id):
            logger.warning("跳过技能文件（id 不合法）: %s", path)
            return None
        try:
            # utf-8-sig：Windows 记事本存的文件常带 BOM
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            logger.warning("跳过技能文件（读取失败 %s）: %s", exc, path)
            return None

        meta, body = _split_frontmatter(text)
        tools = tuple(
            item.strip()
            for item in meta.get("tools", "").replace("，", ",").split(",")
            if item.strip()
        )
        return Skill(
            id=skill_id,
            name=meta.get("name", "").strip() or skill_id,
            description=meta.get("description", "").strip(),
            instructions=body.strip(),
            tools=tools,
            source=self.name,
        )


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析 `---` 包裹的简单 key/value 头，返回 (元数据, 正文)。

    不引入 YAML 依赖：只支持 `key: value` 单行，值可用成对引号包裹。
    头部格式不对时按「整篇都是正文」处理，宁可少解析也不要丢掉指令。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end = -1
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            end = index
            break
    if end == -1:
        return {}, text

    meta: dict[str, str] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        meta[key.strip().lower()] = value

    return meta, "\n".join(lines[end + 1 :])
