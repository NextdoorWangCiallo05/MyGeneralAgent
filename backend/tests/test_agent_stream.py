"""Agent 流式链路测试：用脚本化 LLM 替身验证 SSE 事件序列。

覆盖：文本流、工具调用-结果回填、多步收敛、token 用量、错误与最大轮次保护。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_api
from app.config import get_settings
from app.llm.client import ChatChunk, ChunkKind, LLMRequestError
from app.main import app
from app.store import reset_store

PASSWORD = "secret123"
TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "calculator", "arguments": '{"expression": "6*7"}'},
}


class ScriptedLLM:
    """按脚本逐轮流式返回的 LLM 替身。"""

    scripts: list[list[ChatChunk]] = []
    seen_tools: list[dict[str, Any]] | None = None
    seen_messages: list[dict[str, Any]] | None = None

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
        ScriptedLLM.seen_messages = messages
        ScriptedLLM.seen_tools = tools
        self._turn += 1
        script = ScriptedLLM.scripts[min(self._turn, len(ScriptedLLM.scripts) - 1)]
        for chunk in script:
            yield chunk


class ExplodingLLM:
    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def chat_stream(self, *_: Any, **__: Any) -> AsyncIterator[ChatChunk]:
        raise LLMRequestError("上游模型返回 500")
        yield  # pragma: no cover - 使函数成为生成器


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_store()
    ScriptedLLM.scripts = []
    ScriptedLLM.seen_tools = None
    ScriptedLLM.seen_messages = None
    monkeypatch.setattr(get_settings(), "llm_api_key", "test-key")
    yield
    reset_store()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"username": "streamer", "email": "streamer@example.com", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _post_chat(client: TestClient, headers: dict[str, str], message: str = "6*7 等于多少") -> str:
    response = client.post("/api/v1/chat", json={"message": message}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return response.content.decode("utf-8")


def test_system_prompt_guides_mermaid_diagrams(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """系统提示要让模型在结构化场景主动画图，并给出防解析失败的语法约束。"""
    ScriptedLLM.scripts = [
        [
            ChatChunk(kind=ChunkKind.CONTENT.value, text="这是流程图："),
            ChatChunk(kind=ChunkKind.DONE.value),
        ]
    ]
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: ScriptedLLM())

    _post_chat(client, auth_headers, "讲讲下单流程")

    assert ScriptedLLM.seen_messages is not None
    head = ScriptedLLM.seen_messages[0]
    assert head["role"] == "system"
    system = str(head["content"])
    assert "```mermaid" in system
    for token in ("flowchart", "sequenceDiagram", "stateDiagram-v2", "erDiagram"):
        assert token in system
    assert "不要为配图而配图" in system


def test_tool_call_roundtrip(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ScriptedLLM.scripts = [
        [
            ChatChunk(kind=ChunkKind.CONTENT.value, text="我先算一下。"),
            ChatChunk(kind=ChunkKind.TOOL_CALLS.value, tool_calls=[TOOL_CALL]),
            ChatChunk(kind=ChunkKind.DONE.value),
        ],
        [
            ChatChunk(kind=ChunkKind.CONTENT.value, text="结果是 42。"),
            ChatChunk(kind=ChunkKind.USAGE.value, usage={"prompt_tokens": 12, "completion_tokens": 6}),
            ChatChunk(kind=ChunkKind.DONE.value),
        ],
    ]
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: ScriptedLLM())

    body = _post_chat(client, auth_headers)

    assert "event: start" in body
    assert "event: text" in body
    assert "event: tool_call" in body
    assert "event: tool_result" in body
    assert "event: usage" in body
    assert "event: done" in body
    assert '"name": "calculator"' in body
    assert "6*7 = 42" in body
    assert "结果是 42。" in body

    # 工具 schema 已下发给模型
    assert ScriptedLLM.seen_tools is not None
    assert ScriptedLLM.seen_tools[0]["function"]["name"] == "calculator"

    # 历史已落库：user -> assistant(tool_calls) -> tool -> assistant
    sessions = client.get("/api/v1/sessions", headers=auth_headers).json()
    assert len(sessions) == 1
    session_id = sessions[0]["id"]
    assert sessions[0]["title"].startswith("6*7")

    history = client.get(f"/api/v1/sessions/{session_id}/messages", headers=auth_headers).json()
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    assert history[1]["tool_calls"][0]["function"]["name"] == "calculator"
    assert history[2]["content"] == "42"


def test_regenerate_replaces_last_turn(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """重新生成应摘掉最后一轮再重跑：历史被替换而非追加，模型看不到上一版回答。"""
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: ScriptedLLM())

    ScriptedLLM.scripts = [
        [
            ChatChunk(kind=ChunkKind.CONTENT.value, text="第一版回答"),
            ChatChunk(kind=ChunkKind.DONE.value),
        ]
    ]
    assert "第一版回答" in _post_chat(client, auth_headers, "介绍一下你自己")

    sessions = client.get("/api/v1/sessions", headers=auth_headers).json()
    assert len(sessions) == 1
    session_id = sessions[0]["id"]
    title = sessions[0]["title"]

    ScriptedLLM.scripts = [
        [
            ChatChunk(kind=ChunkKind.CONTENT.value, text="第二版回答"),
            ChatChunk(kind=ChunkKind.DONE.value),
        ]
    ]
    response = client.post(
        "/api/v1/chat",
        json={"message": "介绍一下你自己", "session_id": session_id, "regenerate": True},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert "第二版回答" in response.content.decode("utf-8")

    # 仍是一轮对话：旧回答被替换掉
    history = client.get(f"/api/v1/sessions/{session_id}/messages", headers=auth_headers).json()
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["content"] == "介绍一下你自己"
    assert history[1]["content"] == "第二版回答"

    # 送模型的上下文与原轮一致：只有一份提问，且不含上一版回答
    assert ScriptedLLM.seen_messages is not None
    assert [m["role"] for m in ScriptedLLM.seen_messages] == ["system", "user"]
    assert all("第一版回答" not in str(m.get("content", "")) for m in ScriptedLLM.seen_messages)

    # 标题沿用首次生成的，不被重跑改写
    assert client.get("/api/v1/sessions", headers=auth_headers).json()[0]["title"] == title


def test_regenerate_without_history_degrades_to_normal_send(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """空会话请求重新生成时不应报错，按普通发送处理。"""
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: ScriptedLLM())
    ScriptedLLM.scripts = [
        [
            ChatChunk(kind=ChunkKind.CONTENT.value, text="正常回答"),
            ChatChunk(kind=ChunkKind.DONE.value),
        ]
    ]

    response = client.post(
        "/api/v1/chat",
        json={"message": "你好", "regenerate": True},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert "正常回答" in response.content.decode("utf-8")


def test_llm_error_is_streamed_not_raised(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_api, "build_llm_client", lambda _config: ExplodingLLM())

    body = _post_chat(client, auth_headers)

    assert "event: error" in body
    assert '"code": "LLM_ERROR"' in body
    assert "event: done" in body


def test_max_steps_guard(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "max_steps", 2)
    ScriptedLLM.scripts = [
        [
            ChatChunk(kind=ChunkKind.TOOL_CALLS.value, tool_calls=[TOOL_CALL]),
            ChatChunk(kind=ChunkKind.DONE.value),
        ]
    ]
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: ScriptedLLM())

    body = _post_chat(client, auth_headers)

    assert '"code": "MAX_STEPS_EXCEEDED"' in body
    assert body.count("event: tool_call") == 2


def test_calculator_error_is_fed_back_to_model(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_call = {
        "id": "call_bad",
        "type": "function",
        "function": {"name": "calculator", "arguments": '{"expression": "1/0"}'},
    }
    ScriptedLLM.scripts = [
        [ChatChunk(kind=ChunkKind.TOOL_CALLS.value, tool_calls=[bad_call]), ChatChunk(kind=ChunkKind.DONE.value)],
        [ChatChunk(kind=ChunkKind.CONTENT.value, text="除数为零，无法计算。"), ChatChunk(kind=ChunkKind.DONE.value)],
    ]
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: ScriptedLLM())

    body = _post_chat(client, auth_headers, "1/0 是多少")

    assert '"status": "error"' in body
    assert "除数为零" in body


def test_stop_endpoint_requires_ownership(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.post("/api/v1/chat/stop", json={"session_id": "missing"}, headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"
