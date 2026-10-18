"""思考模式：一个布尔开关切换「推理模型 / 默认模型」。

前端只需要知道「能不能开」和「叫什么名字」，不需要硬编码厂商的模型名；
因此可用性与模型名都由 `/api/v1/config` 下发，切换动作走 `POST /chat` 的
`thinking` 字段，并**按会话记忆**（写回 session.model）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_api
import app.api.runtime as runtime_api
from app.config import get_settings
from app.core.runtime_config import reset_runtime_config
from app.llm.client import ChatChunk, ChunkKind
from app.main import app
from app.store import reset_store

PASSWORD = "secret123"


class ScriptedLLM:
    """只吐一句正文的模型替身，够用来观察会话模型的选择。"""

    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[ChatChunk]:
        yield ChatChunk(kind=ChunkKind.CONTENT.value, text="好的。")
        yield ChatChunk(kind=ChunkKind.DONE.value)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_store()
    reset_runtime_config()
    monkeypatch.setattr(get_settings(), "llm_api_key", "test-key")
    monkeypatch.setattr(get_settings(), "thinking_model", "")
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: ScriptedLLM())
    yield
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
        json={"username": "thinker", "email": "thinker@example.com", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _chat(client: TestClient, headers: dict[str, str], **extra: Any) -> str:
    payload = {"message": "你好", **extra}
    response = client.post("/api/v1/chat", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.content.decode("utf-8")


def _start_model(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("data: ") and '"session_id"' in line:
            return json.loads(line[len("data: ") :])["model"]
    raise AssertionError("没有找到 start 事件")


def test_runtime_config_exposes_thinking_capability(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """DeepSeek 预设带推理模型：开关可用，且把模型名告诉前端。"""
    body = client.get("/api/v1/config", headers=auth_headers).json()

    assert body["thinking_available"] is True
    assert body["thinking_model"] == "deepseek-reasoner"


def test_thinking_mode_switches_model_and_remembers_it(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """开启 → 会话模型换成推理模型；关闭 → 回落到默认模型；两者都持久化。"""
    body = _chat(client, auth_headers, thinking=True)
    assert _start_model(body) == "deepseek-reasoner"

    session = client.get("/api/v1/sessions", headers=auth_headers).json()[0]
    assert session["model"] == "deepseek-reasoner"

    # 不传 thinking 时保持会话当前设置不变（不会莫名其妙掉回默认模型）
    body = _chat(client, auth_headers, session_id=session["id"])
    assert _start_model(body) == "deepseek-reasoner"

    # 关闭思考模式：回到厂商默认模型（deepseek-chat）
    body = _chat(client, auth_headers, session_id=session["id"], thinking=False)
    assert _start_model(body) == "deepseek-chat"
    session = client.get("/api/v1/sessions", headers=auth_headers).json()[0]
    assert session["model"] == "deepseek-chat"


def test_thinking_mode_is_off_by_default(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """默认仍是普通模型：不传 thinking 不该悄悄换成推理模型（更慢更贵）。"""
    body = _chat(client, auth_headers)

    assert _start_model(body) == "deepseek-chat"
    assert client.get("/api/v1/sessions", headers=auth_headers).json()[0]["model"] == "deepseek-chat"


def test_explicit_model_wins_when_thinking_enabled(
    client: TestClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调用方点名了模型就按它来：私有网关没声明预设也能用推理模型。"""
    monkeypatch.setattr(chat_api, "resolve_thinking_model", lambda _: "")

    body = _chat(client, auth_headers, thinking=True, model="custom-reasoner")
    assert _start_model(body) == "custom-reasoner"


def test_thinking_mode_unavailable_returns_400(
    client: TestClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """厂商没有推理模型时：配置接口报不可用，强行开启给出明确 400 而不是静默降级。"""
    monkeypatch.setattr(chat_api, "resolve_thinking_model", lambda _: "")
    monkeypatch.setattr(runtime_api, "resolve_thinking_model", lambda _: "")

    config = client.get("/api/v1/config", headers=auth_headers).json()
    assert config["thinking_available"] is False
    assert config["thinking_model"] == ""

    response = client.post(
        "/api/v1/chat", json={"message": "你好", "thinking": True}, headers=auth_headers
    )
    assert response.status_code == 400
    assert response.json()["code"] == "THINKING_MODE_UNAVAILABLE"
    # 请求被拒时不留下半个会话
    assert client.get("/api/v1/sessions", headers=auth_headers).json() == []
