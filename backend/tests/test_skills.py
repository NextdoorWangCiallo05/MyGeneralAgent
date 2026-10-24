"""技能（Skill）：内置技能、目录定制、对话注入与工具收敛。

技能只做两件事——往 system 里加角色指令、收窄工具范围，
所以测试也从这两点验证；另外覆盖「自定义目录」这个定制化入口。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_api
from app.config import get_settings
from app.core.runtime_config import reset_runtime_config
from app.llm.client import ChatChunk, ChunkKind
from app.main import app
from app.skills import build_skill_registry, get_skill_registry
from app.skills.providers import FileSkillProvider
from app.store import reset_store

PASSWORD = "secret123"

CALC_CALL = {
    "id": "call_calc",
    "type": "function",
    "function": {"name": "calculator", "arguments": '{"expression": "1+1"}'},
}


class CapturingLLM:
    """记录本轮实际下发的 messages / tools，并按脚本吐分片。"""

    scripts: list[list[ChatChunk]] = []
    messages: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []

    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[ChatChunk]:
        type(self).messages = messages
        type(self).tools = tools or []
        script = type(self).scripts.pop(0) if type(self).scripts else [
            ChatChunk(kind=ChunkKind.CONTENT.value, text="好的。"),
            ChatChunk(kind=ChunkKind.DONE.value),
        ]
        for chunk in script:
            yield chunk


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_store()
    reset_runtime_config()
    monkeypatch.setattr(get_settings(), "llm_api_key", "test-key")
    monkeypatch.setattr(get_settings(), "skills_dir", "")
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: CapturingLLM())
    get_skill_registry.cache_clear()
    CapturingLLM.scripts = []
    CapturingLLM.messages = []
    CapturingLLM.tools = []
    yield
    get_skill_registry.cache_clear()
    reset_store()
    reset_runtime_config()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"username": "skiller", "email": "skiller@example.com", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _tool_names(tools: list[dict[str, Any]]) -> set[str]:
    return {item["function"]["name"] for item in tools}


# ---------------------------------------------------------------- 接口 / 注入


def test_list_skills_exposes_builtin_capabilities(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    data = client.get("/api/v1/skills", headers=auth_headers).json()

    ids = {item["id"] for item in data["items"]}
    assert {"research", "coding", "writing"} <= ids
    assert data["total"] == len(data["items"])

    research = next(item for item in data["items"] if item["id"] == "research")
    assert research["name"] == "深度调研"
    assert "web_search" in research["tools"]
    # 完整指令属于服务端资产，不随列表下发
    assert "instructions" not in research


def test_skills_require_auth(client: TestClient) -> None:
    assert client.get("/api/v1/skills").status_code == 401


def test_chat_injects_skill_instructions_and_limits_tools(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/chat",
        json={"message": "帮我查一下", "skill_id": "research"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text

    system = CapturingLLM.messages[0]
    assert system["role"] == "system"
    assert "【当前技能：深度调研】" in system["content"]
    assert "调研分析师" in system["content"]

    # 只下发技能声明的工具；沙箱关闭时读文件类本来就不在（双重收敛）
    names = _tool_names(CapturingLLM.tools)
    assert names <= {"calculator", "web_search", "fetch_url", "knowledge_search"}
    assert {"calculator", "web_search", "fetch_url"} <= names


def test_chat_without_skill_keeps_default_toolset(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/chat", json={"message": "你好"}, headers=auth_headers
    )
    assert response.status_code == 200, response.text

    assert "【当前技能" not in CapturingLLM.messages[0]["content"]
    # 不选技能时 calculator / web_search 等照旧可用
    assert {"calculator", "web_search", "fetch_url"} <= _tool_names(CapturingLLM.tools)


def test_unknown_skill_returns_400_without_creating_session(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/chat",
        json={"message": "你好", "skill_id": "no-such-skill"},
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "SKILL_NOT_FOUND"
    assert client.get("/api/v1/sessions", headers=auth_headers).json() == []


def test_skill_blocks_tools_outside_scope(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """模型硬要调用技能外的工具时，被拦下并告知原因（不给执行机会）。"""
    CapturingLLM.scripts = [
        [
            ChatChunk(kind=ChunkKind.TOOL_CALLS.value, tool_calls=[CALC_CALL]),
            ChatChunk(kind=ChunkKind.DONE.value),
        ],
        [ChatChunk(kind=ChunkKind.CONTENT.value, text="好的。"), ChatChunk(kind=ChunkKind.DONE.value)],
    ]

    response = client.post(
        "/api/v1/chat",
        json={"message": "算一下", "skill_id": "writing"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.content.decode("utf-8")

    results: list[dict[str, Any]] = []
    current = ""
    for line in body.splitlines():
        if line.startswith("event: "):
            current = line[len("event: ") :]
        elif line.startswith("data: ") and current == "tool_result":
            results.append(json.loads(line[len("data: ") :]))
    assert results and results[0]["status"] == "error"
    assert "不在当前技能范围内" in results[0]["summary"]
    # 技能外的工具不会下发给模型
    assert "calculator" not in _tool_names(CapturingLLM.tools)


# ---------------------------------------------------------------- 定制化入口


def test_file_provider_loads_markdown_skill(tmp_path: Path) -> None:
    (tmp_path / "contract-review.md").write_text(
        """---
name: 合同审阅
description: 逐条核对合同风险
tools: read_file, write_file
---

你是资深合同律师，审阅时先列出风险点，再给修改建议。
""",
        encoding="utf-8",
    )

    skills = FileSkillProvider(tmp_path).load()

    assert len(skills) == 1
    skill = skills[0]
    assert skill.id == "contract-review"
    assert skill.name == "合同审阅"
    assert skill.description == "逐条核对合同风险"
    assert skill.tools == ("read_file", "write_file")
    assert skill.source == "file"
    assert "资深合同律师" in skill.instructions
    assert "---" not in skill.instructions


def test_file_provider_skips_bad_files_and_missing_dir(tmp_path: Path) -> None:
    (tmp_path / "Bad Name.md").write_text("非法 id 的技能", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("不是 Markdown", encoding="utf-8")

    provider = FileSkillProvider(tmp_path)
    assert provider.load() == []
    assert FileSkillProvider(tmp_path / "nope").load() == []


def test_custom_skill_overrides_builtin(tmp_path: Path) -> None:
    (tmp_path / "writing.md").write_text(
        """---
name: 品牌文案
description: 按品牌手册写文案
---

按《品牌手册》的口吻写作。
""",
        encoding="utf-8",
    )
    settings = get_settings().model_copy(update={"skills_dir": str(tmp_path)})

    registry = build_skill_registry(settings)

    skill = registry.get("writing")
    assert skill is not None
    assert skill.source == "file"
    assert skill.name == "品牌文案"
    # 未覆盖的内置技能仍在
    assert registry.get("research") is not None


def test_api_lists_skill_from_configured_dir(
    client: TestClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "weekly-report.md").write_text(
        "---\nname: 周报助手\ndescription: 把流水账整理成周报\n---\n\n你负责整理周报。\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(get_settings(), "skills_dir", str(tmp_path))
    get_skill_registry.cache_clear()

    data = client.get("/api/v1/skills", headers=auth_headers).json()

    custom = next(item for item in data["items"] if item["id"] == "weekly-report")
    assert custom["name"] == "周报助手"
    assert custom["source"] == "file"
