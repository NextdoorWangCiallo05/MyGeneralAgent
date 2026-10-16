"""M3 人工确认链路测试。

核心不变式：**副作用工具在用户明确授权之前绝不产生任何副作用**。
覆盖授权执行、拒绝执行、超时按拒绝、沙箱关闭时兜底拦截，
以及 `/chat/confirm` 接口对失效请求的处理。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.agent import Agent
from app.core.confirmations import get_confirmations, reset_confirmations
from app.core.events import EventType
from app.core.runtime_config import reset_runtime_config
from app.llm.client import ChatChunk, ChunkKind
from app.main import app
from app.store import get_store, reset_store
from app.tools.registry import build_default_registry
from app.tools.sandbox import resolve_path

PASSWORD = "secret123"

WRITE_CALL = {
    "id": "call_write_1",
    "type": "function",
    "function": {"name": "write_file", "arguments": '{"path": "out.txt", "content": "hello"}'},
}

SCRIPTS: list[list[ChatChunk]] = [
    [
        ChatChunk(kind=ChunkKind.TOOL_CALLS.value, tool_calls=[WRITE_CALL]),
        ChatChunk(kind=ChunkKind.DONE.value),
    ],
    [
        ChatChunk(kind=ChunkKind.CONTENT.value, text="已完成。"),
        ChatChunk(kind=ChunkKind.DONE.value),
    ],
]


class ScriptedLLM:
    """按脚本逐轮流式返回的 LLM 替身。"""

    def __init__(self, *_: Any, **__: Any) -> None:
        self._turn = -1

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[ChatChunk]:
        self._turn += 1
        for chunk in SCRIPTS[min(self._turn, len(SCRIPTS) - 1)]:
            yield chunk


@pytest.fixture(autouse=True)
def isolated() -> Iterator[None]:
    """隔离存储、确认表与运行时配置，避免用例之间互相污染。"""
    reset_store()
    reset_confirmations()
    reset_runtime_config()
    get_settings().sandbox_global_enabled = False
    yield
    reset_store()
    reset_confirmations()
    reset_runtime_config()
    get_settings().sandbox_global_enabled = False


async def _drive(
    *,
    approve: bool | None,
    sandbox_enabled: bool = True,
    confirm_timeout: float = 5.0,
) -> tuple[list[Any], str]:
    """跑完一轮 Agent，收到确认请求时按 `approve` 作答（None 表示不响应）。"""
    settings = get_settings().model_copy(
        update={"sandbox_global_enabled": True, "tool_confirm_timeout": confirm_timeout}
    )
    store = get_store()
    user = await store.create_user(
        username="writer", email="writer@example.com", password_hash="x"
    )
    session = await store.create_session(
        user_id=user.id, model="test-model", sandbox_enabled=sandbox_enabled
    )
    agent = Agent(ScriptedLLM(), build_default_registry(), store, settings=settings)

    events: list[Any] = []
    async for event in agent.run(user=user, session=session, user_input="把结果写入 out.txt"):
        events.append(event)
        if event.type == EventType.TOOL_CONFIRM.value and approve is not None:
            assert get_confirmations().resolve(event.data["id"], approve) is True
    return events, user.id


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def _statuses(events: list[Any]) -> list[str]:
    return [event.data["status"] for event in events if event.type == EventType.TOOL_RESULT.value]


def _types(events: list[Any]) -> list[str]:
    return [event.type for event in events]


# ---------------------------------------------------------------- Agent 层
def test_approved_write_file_is_executed() -> None:
    events, user_id = _run(_drive(approve=True))

    assert EventType.TOOL_CONFIRM.value in _types(events)
    assert _statuses(events) == ["success"]
    assert resolve_path(user_id, "out.txt").read_text(encoding="utf-8") == "hello"


def test_rejected_write_file_is_not_executed() -> None:
    events, user_id = _run(_drive(approve=False))

    assert _statuses(events) == ["rejected"]
    assert not resolve_path(user_id, "out.txt").exists()


def test_confirmation_timeout_is_treated_as_rejection() -> None:
    events, user_id = _run(_drive(approve=None, confirm_timeout=0.2))

    assert _statuses(events) == ["rejected"]
    assert not resolve_path(user_id, "out.txt").exists()


def test_sandbox_tools_blocked_when_session_disabled() -> None:
    events, user_id = _run(_drive(approve=True, sandbox_enabled=False))

    assert EventType.TOOL_CONFIRM.value not in _types(events)
    assert _statuses(events) == ["error"]
    assert not resolve_path(user_id, "out.txt").exists()


# ---------------------------------------------------------------- API 层
@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"username": "confirmer", "email": "confirmer@example.com", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_confirm_stale_request_returns_conflict(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    session = client.post(
        "/api/v1/sessions", json={"title": "确认测试"}, headers=auth_headers
    ).json()

    response = client.post(
        "/api/v1/chat/confirm",
        json={"session_id": session["id"], "tool_call_id": "ghost", "approved": True},
        headers=auth_headers,
    )

    assert response.status_code == 409
    assert "CONFIRMATION_NOT_FOUND" in response.text


def test_confirm_rejects_foreign_session(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/chat/confirm",
        json={"session_id": "not-mine", "tool_call_id": "call_1", "approved": True},
        headers=auth_headers,
    )
    assert response.status_code == 404


def test_confirm_requires_authentication(client: TestClient) -> None:
    response = client.post(
        "/api/v1/chat/confirm",
        json={"session_id": "s", "tool_call_id": "c", "approved": True},
    )
    assert response.status_code == 401


def test_runtime_config_exposes_sandbox_switches(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    body = client.get("/api/v1/config", headers=auth_headers).json()

    assert "sandbox_global_enabled" in body
    assert set(body["sandbox_tools"]) == {"read_file", "write_file", "python_exec"}
