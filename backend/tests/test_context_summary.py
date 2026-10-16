"""上下文压缩（FR-6）测试。

覆盖：token 估算、轮次切分（不拆散工具调用序列）、摘要注入、摘要落库、
以及摘要服务不可用时的降级行为。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_api
from app.config import get_settings
from app.core.summary import estimate_tokens, split_turns
from app.llm.client import ChatChunk, ChunkKind, LLMRequestError
from app.main import app
from app.store import get_store, reset_store
from app.store.entities import Message

PASSWORD = "secret123"
SUMMARY_TEXT = "用户叫小王，正在整理 2026 年 Q3 的销售报表。"
TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "calculator", "arguments": '{"expression": "1+1"}'},
}


class ScriptedLLM:
    """按顺序返回脚本的 LLM 替身，并记录每次收到的消息。"""

    scripts: list[list[ChatChunk]] = []
    calls: list[list[dict[str, Any]]] = []

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
        ScriptedLLM.calls.append(messages)
        self._turn += 1
        script = ScriptedLLM.scripts[min(self._turn, len(ScriptedLLM.scripts) - 1)]
        for chunk in script:
            yield chunk


class FailingSummaryLLM(ScriptedLLM):
    """摘要调用（不携带 tools）失败，正式对话正常返回。"""

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[ChatChunk]:
        if tools is None:
            raise LLMRequestError("摘要服务不可用")
        async for chunk in super().chat_stream(
            messages, tools=tools, model=model, temperature=temperature
        ):
            yield chunk


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_store()
    ScriptedLLM.scripts = []
    ScriptedLLM.calls = []
    monkeypatch.setattr(get_settings(), "llm_api_key", "test-key")
    # 阈值调小，避免测试里真的塞几千 token
    monkeypatch.setattr(get_settings(), "summary_trigger_tokens", 200)
    monkeypatch.setattr(get_settings(), "summary_keep_turns", 2)
    monkeypatch.setattr(get_settings(), "summary_min_new_tokens", 200)
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
        json={"username": "summarizer", "email": "summarizer@example.com", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _answer(text: str) -> list[ChatChunk]:
    return [
        ChatChunk(kind=ChunkKind.CONTENT.value, text=text),
        ChatChunk(kind=ChunkKind.DONE.value),
    ]


def _text(chunk_count: int = 30) -> str:
    return "细节" * chunk_count


def _chat(
    client: TestClient,
    headers: dict[str, str],
    message: str,
    session_id: str | None = None,
) -> Any:
    payload: dict[str, Any] = {"message": message}
    if session_id:
        payload["session_id"] = session_id
    return client.post("/api/v1/chat", json=payload, headers=headers)


def _seed_history(client: TestClient, headers: dict[str, str], turns: int) -> str:
    """建立会话并写入若干轮历史消息，返回 session_id。"""
    first = client.post("/api/v1/chat", json={"message": "你好"}, headers=headers)
    assert first.status_code == 200, first.text
    session_id = client.get("/api/v1/sessions", headers=headers).json()[0]["id"]

    store = get_store()

    async def seed() -> None:
        for index in range(turns):
            await store.add_message(
                session_id=session_id, role="user", content=f"第 {index} 轮问题{_text()}"
            )
            await store.add_message(
                session_id=session_id, role="assistant", content=f"第 {index} 轮回答{_text()}"
            )

    asyncio.run(seed())
    return session_id


# ---------------------------------------------------------------- 纯函数
def test_estimate_tokens_counts_cjk_heavier() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("中文") == 2
    assert estimate_tokens("abc中文") == 2


def test_split_turns_keeps_tool_messages_with_their_turn() -> None:
    history = [
        Message(id="1", session_id="s", role="user", content="查一下", seq=0),
        Message(id="2", session_id="s", role="assistant", content="", seq=1, tool_calls=[TOOL_CALL]),
        Message(id="3", session_id="s", role="tool", content="2", seq=2, tool_call_id="call_1"),
        Message(id="4", session_id="s", role="assistant", content="结果是 2", seq=3),
        Message(id="5", session_id="s", role="user", content="谢谢", seq=4),
        Message(id="6", session_id="s", role="assistant", content="不客气", seq=5),
    ]

    turns = split_turns(history)

    assert [len(turn) for turn in turns] == [4, 2]
    # 工具调用与它的 tool 结果始终落在同一轮里
    assert [m.role for m in turns[0]] == ["user", "assistant", "tool", "assistant"]


# ---------------------------------------------------------------- 端到端
def test_summary_injected_and_persisted(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: ScriptedLLM())
    ScriptedLLM.scripts = [_answer("你好")]
    session_id = _seed_history(client, auth_headers, turns=6)

    ScriptedLLM.scripts = [_answer(SUMMARY_TEXT), _answer("好的")]
    ScriptedLLM.calls = []
    response = _chat(client, auth_headers, "继续", session_id)

    assert response.status_code == 200, response.text
    assert "好的" in response.content.decode("utf-8")

    # 1) 摘要先注入 system 消息
    sent = ScriptedLLM.calls[-1]
    assert "【历史对话摘要（较早轮次已压缩）】" in sent[0]["content"]
    assert SUMMARY_TEXT in sent[0]["content"]
    # 2) 只保留最近 2 轮（4 条）+ 本轮输入
    assert len(sent) == 1 + 4 + 1
    assert sent[1]["content"].startswith("第 4 轮问题")

    # 3) 摘要与进度已落库
    listed = client.get("/api/v1/sessions", headers=auth_headers).json()
    assert listed[0]["id"] == session_id
    assert listed[0]["summary"] == SUMMARY_TEXT

    stored = asyncio.run(get_store().get_session(session_id))
    assert stored is not None
    assert stored.summary == SUMMARY_TEXT
    assert stored.summary_upto_seq >= 0


def test_second_round_reuses_summary_without_recalling_model(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """增量摘要：已压缩过的前缀不再重复调用模型。"""
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: ScriptedLLM())
    ScriptedLLM.scripts = [_answer("你好")]
    session_id = _seed_history(client, auth_headers, turns=6)

    ScriptedLLM.scripts = [_answer(SUMMARY_TEXT), _answer("好的")]
    _chat(client, auth_headers, "继续", session_id)

    ScriptedLLM.scripts = [_answer("第二轮回答")]
    ScriptedLLM.calls = []
    _chat(client, auth_headers, "再继续", session_id)

    # 本轮只发生一次模型调用（即正式回答），没有额外的摘要调用
    assert len(ScriptedLLM.calls) == 1
    sent = ScriptedLLM.calls[0]
    assert SUMMARY_TEXT in sent[0]["content"]
    # 已摘要的前缀（10 条）不再送入模型，只保留剩余 6 条 + 本轮输入
    assert len(sent) == 1 + 6 + 1

    stored = asyncio.run(get_store().get_session(session_id))
    assert stored is not None and stored.summary == SUMMARY_TEXT


def test_summary_failure_falls_back_to_full_history(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_api, "build_llm_client", lambda _config: FailingSummaryLLM())
    ScriptedLLM.scripts = [_answer("你好")]
    session_id = _seed_history(client, auth_headers, turns=6)

    ScriptedLLM.scripts = [_answer("好的")]
    ScriptedLLM.calls = []
    response = _chat(client, auth_headers, "继续", session_id)

    body = response.content.decode("utf-8")
    assert "好的" in body
    assert "event: error" not in body

    # 摘要失败 -> 不加摘要、历史完整保留
    sent = ScriptedLLM.calls[-1]
    assert "历史对话摘要" not in sent[0]["content"]
    assert len(sent) == 1 + 14 + 1

    stored = asyncio.run(get_store().get_session(session_id))
    assert stored is not None
    assert stored.summary == ""
