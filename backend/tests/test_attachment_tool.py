"""会话附件读取工具（read_attachment）测试：分段读取、关键词定位、用户与会话隔离、门控。

附件是会话级资源：`Agent` 会把它们注入 system 的「本会话文件」区，放不进预算的部分
由本工具按需读取。这里直接调工具函数（不经过模型），验证读得准、不会越权。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_api
from app.config import get_settings
from app.llm.client import ChatChunk, ChunkKind
from app.main import app
from app.store import reset_store
from app.tools.attachments import _read_attachment
from app.tools.context import ToolContext, reset_tool_context, set_tool_context
from app.tools.registry import build_default_registry

PASSWORD = "secret123"

MANUAL = """# 发布手册

## 回滚
出现异常时执行 scripts/rollback.sh 回滚到上一个镜像版本。

## 灰度
新版本先放量 5%，观察 30 分钟后再全量。
"""


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_store()
    monkeypatch.setattr(get_settings(), "llm_api_key", "test-key")
    # 只为了通过发消息把附件绑定到会话，这里不需要真模型
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_, **__: CapturingLLM())
    yield
    reset_store()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


class CapturingLLM:
    """记录下发的 tools，并直接返回一段文本（这里只借它拿到 session_id）。"""

    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[ChatChunk]:
        yield ChatChunk(kind=ChunkKind.CONTENT.value, text="好的")
        yield ChatChunk(kind=ChunkKind.DONE.value)


def _register(client: TestClient, username: str = "reader") -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _user_id(client: TestClient, headers: dict[str, str]) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["id"]


def _upload(
    client: TestClient,
    headers: dict[str, str],
    *,
    filename: str = "manual.md",
    text: str = MANUAL,
) -> str:
    response = client.post(
        "/api/v1/attachments",
        headers=headers,
        files={"file": (filename, text.encode("utf-8"), "text/markdown")},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _bind_to_session(
    client: TestClient, headers: dict[str, str], attachment_id: str
) -> str:
    """通过发一条消息把附件绑定到新会话，返回 session_id。"""
    response = client.post(
        "/api/v1/chat",
        json={"message": "看看这份手册", "attachment_ids": [attachment_id]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    matched = re.search(r'"session_id":\s*"([^"]+)"', response.content.decode())
    assert matched is not None
    return matched.group(1)


def _run_tool(user_id: str, attachment_id: str, *, session_id: str, **kwargs: Any) -> Any:
    token = set_tool_context(ToolContext(user_id=user_id, session_id=session_id))
    try:
        return asyncio.run(_read_attachment(attachment_id, **kwargs))
    finally:
        reset_tool_context(token)


def test_read_attachment_slices_by_offset_and_limit(client: TestClient) -> None:
    headers = _register(client)
    attachment_id = _upload(client, headers)
    user_id = _user_id(client, headers)

    # 未绑定会话的附件（刚上传、还没随消息发出）也允许当前用户读
    whole = _run_tool(user_id, attachment_id, session_id="session-1")
    assert whole.status == "success"
    assert "【附件】manual.md" in whole.content
    assert "scripts/rollback.sh" in whole.content
    assert whole.data["total_chars"] == len(MANUAL.strip())

    head = _run_tool(user_id, attachment_id, session_id="session-1", limit=12)
    assert "本次返回第 1~12 字" in head.content
    assert "可继续用 offset=12 读取" in head.content
    assert "scripts/rollback.sh" not in head.content

    # offset 越界时夹到文件末尾，不能抛 IndexError
    tail = _run_tool(user_id, attachment_id, session_id="session-1", offset=99_999)
    assert tail.status == "success"
    assert tail.data["offset"] == whole.data["total_chars"]

    # 模型偶尔会把 offset 传成字符串，这里要能兜住而不是报参数错误
    loose = _run_tool(user_id, attachment_id, session_id="session-1", offset="0", limit="20")
    assert loose.status == "success"


def test_read_attachment_locates_keyword(client: TestClient) -> None:
    headers = _register(client)
    attachment_id = _upload(client, headers)
    user_id = _user_id(client, headers)

    hit = _run_tool(user_id, attachment_id, session_id="session-1", keyword="回滚")
    assert hit.status == "success"
    assert "命中 2 处" in hit.content
    assert "scripts/rollback.sh" in hit.content
    assert hit.data["hits"] == 2

    miss = _run_tool(user_id, attachment_id, session_id="session-1", keyword="量子计算")
    assert miss.status == "success"
    assert "没有出现" in miss.content
    assert miss.data["hits"] == 0


def test_read_attachment_rejects_other_users_and_sessions(client: TestClient) -> None:
    owner = _register(client, "file-owner")
    other = _register(client, "file-other")
    owner_id = _user_id(client, owner)
    other_id = _user_id(client, other)
    attachment_id = _upload(client, owner)

    # 别人的附件读不到（不区分"不存在"与"不是你的"，避免探测）
    stolen = _run_tool(other_id, attachment_id, session_id="session-1")
    assert stolen.status == "error"
    assert "不属于当前用户" in stolen.content

    # 绑定到别的会话的附件，本会话读不到（否则猜到 id 就能跨会话翻文件）
    session_id = _bind_to_session(client, owner, attachment_id)
    cross = _run_tool(owner_id, attachment_id, session_id="another-session")
    assert cross.status == "error"
    assert "其他会话" in cross.content

    # 本会话读得到
    ok = _run_tool(owner_id, attachment_id, session_id=session_id)
    assert ok.status == "success"

    # 空 id / 不存在的 id 都给可读错误，让模型能自我纠正
    empty = _run_tool(owner_id, "", session_id=session_id)
    assert empty.status == "error"
    assert "不能为空" in empty.content
    unknown = _run_tool(owner_id, "not-a-real-id", session_id=session_id)
    assert unknown.status == "error"
    assert "不存在" in unknown.content


def test_registry_gates_attachment_tool() -> None:
    """会话里没有附件时不下发 read_attachment（与知识库工具同源的门控）。"""
    registry = build_default_registry()

    assert "read_attachment" in registry.available_names(
        sandbox_enabled=False, attachments_enabled=True
    )
    assert "read_attachment" not in registry.available_names(
        sandbox_enabled=False, attachments_enabled=False
    )
    assert registry.is_attachment_required("read_attachment") is True
    assert registry.is_attachment_required("calculator") is False


def test_attachment_tool_is_readonly_and_not_sandboxed() -> None:
    """附件读取只读、不需要沙箱，也不该要求人工确认（否则会话里问文件要弹确认框）。"""
    registry = build_default_registry()
    tool = registry.get("read_attachment")

    assert tool is not None
    assert tool.readonly is True
    assert tool.requires_confirmation is False
    assert tool.sandbox_required is False
