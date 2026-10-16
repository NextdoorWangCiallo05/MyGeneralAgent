"""多厂商接入的专项测试。

分三层覆盖：
- 解析层：`resolve_llm_config` 的厂商预设、显式覆盖、旧键兼容、非法厂商；
- 客户端层：`OpenAICompatClient` 的请求组装与 SSE 解析（假 httpx，不发真实请求）；
- 接口层：`/admin/llm/providers` 与切换厂商后的配置回显、`/chat` 的密钥校验。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_api
from app.config import Settings, get_settings
from app.core.runtime_config import reset_runtime_config
from app.llm.client import ChatChunk, ChunkKind, LLMNotConfiguredError, LLMRequestError
from app.llm.openai_compat import OpenAICompatClient
from app.llm.providers import PRESETS, ProviderPreset, build_llm_client, resolve_llm_config
from app.main import app
from app.store import reset_store

SECRET = "sk-live-1234567890abcdef"
PASSWORD = "secret123"
ADMIN_EMAIL = "root@example.com"


def _settings(**overrides: Any) -> Settings:
    """构造一份只用于解析的 Settings：不读 .env，避免受开发者本机配置影响。"""
    base: dict[str, Any] = {
        "llm_provider": "deepseek",
        "llm_api_key": "",
        "llm_base_url": "",
        "default_model": "",
        "deepseek_api_key": "",
        "deepseek_base_url": "https://api.deepseek.com",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


# ---------------------------------------------------------------- 解析层
@pytest.mark.parametrize("preset", [p for p in PRESETS if p.id != "custom"])
def test_preset_alone_is_enough_to_resolve(preset: ProviderPreset) -> None:
    """只选厂商（不填地址/模型/密钥）就能得到一份确定的接入参数。"""
    config = resolve_llm_config(_settings(llm_provider=preset.id))

    assert config.provider == preset.id
    assert config.label == preset.label
    assert config.base_url == preset.base_url.rstrip("/")
    assert config.model == preset.default_model
    assert config.requires_api_key is preset.requires_api_key
    assert config.stream_usage is preset.stream_usage


def test_unknown_provider_is_rejected_with_options() -> None:
    with pytest.raises(LLMNotConfiguredError) as excinfo:
        resolve_llm_config(_settings(llm_provider="anthropic"))

    message = str(excinfo.value)
    assert "anthropic" in message
    assert "deepseek" in message and "openai" in message


def test_explicit_values_override_preset() -> None:
    config = resolve_llm_config(
        _settings(
            llm_provider="openai",
            llm_api_key="sk-proxy",
            llm_base_url="https://proxy.example.com/v1/",
            default_model="gpt-4o",
        )
    )

    assert config.base_url == "https://proxy.example.com/v1"
    assert config.model == "gpt-4o"
    assert config.api_key == "sk-proxy"


def test_custom_provider_needs_address_and_model() -> None:
    """自定义厂商没有预设，缺地址/模型时要给出可读的错误。"""
    with pytest.raises(LLMNotConfiguredError, match="服务地址"):
        resolve_llm_config(_settings(llm_provider="custom"))

    with pytest.raises(LLMNotConfiguredError, match="模型"):
        resolve_llm_config(
            _settings(llm_provider="custom", llm_base_url="https://gateway.internal/v1")
        )

    config = resolve_llm_config(
        _settings(
            llm_provider="custom",
            llm_base_url="https://gateway.internal/v1",
            default_model="internal-model",
        )
    )
    assert config.base_url == "https://gateway.internal/v1"
    assert config.model == "internal-model"


def test_legacy_deepseek_keys_still_work() -> None:
    """升级前的 .env / 后台配置无需改动：旧键在 deepseek 厂商下依旧生效。"""
    config = resolve_llm_config(
        _settings(
            llm_provider="deepseek",
            deepseek_api_key="sk-legacy",
            deepseek_base_url="https://legacy.deepseek.com/",
            llm_base_url="",
        )
    )

    assert config.api_key == "sk-legacy"
    assert config.base_url == "https://legacy.deepseek.com"


def test_legacy_keys_are_ignored_by_other_providers() -> None:
    config = resolve_llm_config(
        _settings(
            llm_provider="openai",
            deepseek_api_key="sk-legacy",
            deepseek_base_url="https://legacy.deepseek.com",
        )
    )

    assert config.api_key == ""
    assert config.base_url == "https://api.openai.com/v1"


def test_new_key_wins_over_legacy_key() -> None:
    config = resolve_llm_config(
        _settings(llm_provider="deepseek", llm_api_key="sk-new", deepseek_api_key="sk-old")
    )

    assert config.api_key == "sk-new"


def test_build_client_maps_every_field() -> None:
    config = resolve_llm_config(_settings(llm_provider="ollama"))
    client = build_llm_client(config)

    assert isinstance(client, OpenAICompatClient)
    assert client.default_model == "qwen2.5:7b"


# ---------------------------------------------------------------- 客户端层
class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class _FakeResponse:
    def __init__(self, lines: list[str], status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self._lines = lines
        self._body = body

    async def aread(self) -> bytes:
        return self._body

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line


def _install_fake_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    lines: list[str] | None = None,
    status_code: int = 200,
    body: bytes = b"",
) -> dict[str, Any]:
    """把 httpx.AsyncClient 换成不发网络的替身，返回记录到的请求参数。"""
    recorded: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            recorded["client_kwargs"] = kwargs

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *exc_info: Any) -> bool:
            return False

        def stream(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            json: dict[str, Any] | None = None,
        ) -> _FakeStream:
            recorded["method"] = method
            recorded["url"] = url
            recorded["headers"] = headers or {}
            recorded["json"] = json or {}
            return _FakeStream(_FakeResponse(lines or [], status_code, body))

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return recorded


async def _collect(client: Any, messages: list[dict[str, Any]] | None = None) -> list[ChatChunk]:
    return [
        chunk
        async for chunk in client.chat_stream(messages or [{"role": "user", "content": "hi"}])
    ]


def _frame(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False)


def test_request_is_openai_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _install_fake_httpx(monkeypatch, lines=["data: [DONE]"])
    client = build_llm_client(
        resolve_llm_config(_settings(llm_provider="deepseek", llm_api_key="sk-x"))
    )

    chunks = asyncio.run(_collect(client))

    assert [c.kind for c in chunks] == [ChunkKind.DONE.value]
    assert recorded["method"] == "POST"
    assert recorded["url"] == "https://api.deepseek.com/chat/completions"
    assert recorded["headers"]["Authorization"] == "Bearer sk-x"
    assert recorded["json"]["stream"] is True
    assert recorded["json"]["stream_options"] == {"include_usage": True}
    assert recorded["json"]["model"] == "deepseek-chat"


def test_local_provider_omits_authorization_and_stream_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ollama 之类的本地端点不校验密钥，也不接受 stream_options。"""
    recorded = _install_fake_httpx(monkeypatch, lines=["data: [DONE]"])
    client = build_llm_client(resolve_llm_config(_settings(llm_provider="ollama")))

    asyncio.run(_collect(client))

    assert "Authorization" not in recorded["headers"]
    assert "stream_options" not in recorded["json"]
    assert recorded["url"] == "http://localhost:11434/v1/chat/completions"


def test_sse_frames_are_parsed_and_tool_calls_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        _frame({"choices": [{"delta": {"reasoning_content": "让我想想"}}]}),
        _frame({"choices": [{"delta": {"content": "你好"}}]}),
        _frame(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "get_weather", "arguments": '{"city"'},
                                }
                            ]
                        }
                    }
                ]
            }
        ),
        _frame(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": ': "北京"}'}}]
                        }
                    }
                ]
            }
        ),
        _frame({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 5}}),
        "",  # 心跳空行应被忽略
        "data: [DONE]",
    ]
    _install_fake_httpx(monkeypatch, lines=lines)
    client = build_llm_client(resolve_llm_config(_settings(llm_api_key="sk-x")))

    chunks = asyncio.run(_collect(client))

    assert [c.kind for c in chunks] == [
        ChunkKind.REASONING.value,
        ChunkKind.CONTENT.value,
        ChunkKind.USAGE.value,
        ChunkKind.TOOL_CALLS.value,
        ChunkKind.DONE.value,
    ]
    assert chunks[0].text == "让我想想"
    assert chunks[1].text == "你好"
    assert chunks[2].usage["completion_tokens"] == 5
    assert chunks[3].tool_calls == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "北京"}'},
        }
    ]


def test_http_error_becomes_llm_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_httpx(
        monkeypatch, status_code=503, body=b'{"error": "upstream overloaded"}'
    )
    client = build_llm_client(resolve_llm_config(_settings(llm_api_key="sk-x")))

    with pytest.raises(LLMRequestError, match="503"):
        asyncio.run(_collect(client))


def test_client_rejects_missing_key() -> None:
    client = OpenAICompatClient(
        api_key="", base_url="https://api.deepseek.com", model="deepseek-chat", label="DeepSeek"
    )

    with pytest.raises(LLMNotConfiguredError, match="API-Key"):
        asyncio.run(_collect(client))


# ---------------------------------------------------------------- 接口层
class EchoLLM:
    """最小 LLM 替身：只回一句文本。"""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None: ...

    async def chat_stream(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[ChatChunk]:
        yield ChatChunk(kind=ChunkKind.CONTENT.value, text="收到")
        yield ChatChunk(kind=ChunkKind.DONE.value)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_store()
    reset_runtime_config()
    monkeypatch.setattr(get_settings(), "llm_api_key", "")
    monkeypatch.setattr(get_settings(), "deepseek_api_key", "")
    yield
    reset_store()
    reset_runtime_config()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("ADMIN_USERNAME", "root")
    get_settings.cache_clear()
    # cache_clear 会重建 Settings，需对新对象再次压掉真实凭据
    monkeypatch.setattr(get_settings(), "llm_api_key", "")
    monkeypatch.setattr(get_settings(), "deepseek_api_key", "")
    reset_runtime_config()

    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()
    reset_runtime_config()


@pytest.fixture
def admin_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"username": ADMIN_EMAIL, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def user_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"username": "bob", "email": "bob@example.com", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_provider_list_is_admin_only(
    client: TestClient, admin_headers: dict[str, str], user_headers: dict[str, str]
) -> None:
    assert client.get("/api/v1/admin/llm/providers").status_code == 401
    assert client.get("/api/v1/admin/llm/providers", headers=user_headers).status_code == 403

    body = client.get("/api/v1/admin/llm/providers", headers=admin_headers).json()

    assert [item["id"] for item in body["items"]] == [preset.id for preset in PRESETS]
    assert body["active"] == "deepseek"
    ollama = next(item for item in body["items"] if item["id"] == "ollama")
    assert ollama["requires_api_key"] is False
    assert ollama["models"]


def test_switching_provider_updates_effective_values(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.put(
        "/api/v1/admin/config",
        json={"llm_provider": "openai", "llm_api_key": SECRET, "default_model": "gpt-4o"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert SECRET not in response.text

    body = response.json()
    assert body["llm_provider"] == "openai"
    assert body["effective_provider"] == "openai"
    assert body["effective_provider_label"] == "OpenAI"
    assert body["effective_base_url"] == "https://api.openai.com/v1"
    assert body["effective_model"] == "gpt-4o"
    assert body["llm_api_key_configured"] is True
    assert body["llm_api_key_from_legacy"] is False

    assert (
        client.get("/api/v1/admin/llm/providers", headers=admin_headers).json()["active"]
        == "openai"
    )

    # 清掉模型覆盖，回到厂商预设的默认模型
    cleared = client.put(
        "/api/v1/admin/config", json={"default_model": ""}, headers=admin_headers
    ).json()
    assert cleared["default_model"] == ""
    assert cleared["effective_model"] == "gpt-4o-mini"


def test_custom_provider_requires_address_and_model(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    body = client.put(
        "/api/v1/admin/config", json={"llm_provider": "custom"}, headers=admin_headers
    ).json()
    # 地址与模型都还没填：表单照常回显，生效值为空
    assert body["llm_provider"] == "custom"
    assert body["effective_base_url"] == ""
    assert body["effective_model"] == ""

    body = client.put(
        "/api/v1/admin/config",
        json={"llm_base_url": "https://gateway.example.com/v1/", "default_model": "internal"},
        headers=admin_headers,
    ).json()
    assert body["llm_base_url"] == "https://gateway.example.com/v1"
    assert body["effective_base_url"] == "https://gateway.example.com/v1"
    assert body["effective_model"] == "internal"


def test_invalid_provider_and_base_url_are_rejected(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    bad_provider = client.put(
        "/api/v1/admin/config", json={"llm_provider": "anthropic"}, headers=admin_headers
    )
    assert bad_provider.status_code == 422
    assert "未知的模型厂商" in bad_provider.text

    bad_url = client.put(
        "/api/v1/admin/config", json={"llm_base_url": "api.example.com"}, headers=admin_headers
    )
    assert bad_url.status_code == 422


def test_legacy_key_reports_configured_only_for_deepseek(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    body = client.put(
        "/api/v1/admin/config", json={"deepseek_api_key": SECRET}, headers=admin_headers
    ).json()
    assert body["llm_api_key_configured"] is True
    assert body["llm_api_key_from_legacy"] is True

    # 切到别家后旧键不再计入，避免界面显示「已配置」而 /chat 报未配置
    body = client.put(
        "/api/v1/admin/config", json={"llm_provider": "openai"}, headers=admin_headers
    ).json()
    assert body["llm_api_key_configured"] is False
    assert body["llm_api_key_from_legacy"] is False


def test_chat_skips_key_check_for_local_provider(
    client: TestClient,
    admin_headers: dict[str, str],
    user_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """切到本地 Ollama 后，没配密钥也能对话，且请求带着解析后的参数。"""
    captured: dict[str, Any] = {}

    def fake_build(config: Any) -> EchoLLM:
        captured["config"] = config
        return EchoLLM()

    monkeypatch.setattr(chat_api, "build_llm_client", fake_build)

    blocked = client.post("/api/v1/chat", json={"message": "hi"}, headers=user_headers)
    assert blocked.status_code == 503
    assert blocked.json()["code"] == "API_KEY_NOT_CONFIGURED"

    client.put("/api/v1/admin/config", json={"llm_provider": "ollama"}, headers=admin_headers)

    accepted = client.post("/api/v1/chat", json={"message": "hi"}, headers=user_headers)
    assert accepted.status_code == 200, accepted.text
    assert "event: text" in accepted.content.decode("utf-8")

    config = captured["config"]
    assert config.provider == "ollama"
    assert config.requires_api_key is False
    assert config.base_url == "http://localhost:11434/v1"
    assert config.model == "qwen2.5:7b"

    sessions = client.get("/api/v1/sessions", headers=user_headers).json()
    assert sessions[0]["model"] == "qwen2.5:7b"
