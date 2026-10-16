"""M2：管理员后台配置 API-Key 的端到端测试。

覆盖：
- 权限：未登录 401、普通用户 403、管理员放行；
- 安全：API-Key 以密文写入 `system_config`，接口只回显脱敏值；
- 生效：后台写入密钥后 `/chat` 立即可用，清除后回落到环境变量；
- 其它：沙箱全局开关、用户列表、字段级局部更新与参数校验。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_api
from app.config import get_settings
from app.core.crypto import decrypt_value, is_encrypted
from app.core.runtime_config import RuntimeConfigService, reset_runtime_config
from app.llm.client import ChatChunk, ChunkKind
from app.main import app
from app.store import MemoryStore, reset_store

PASSWORD = "secret123"
SECRET = "sk-live-1234567890abcdef"
ADMIN_EMAIL = "root@example.com"


class EchoLLM:
    """最小 LLM 替身：只回一句文本，用于验证密钥已就绪。"""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None: ...

    async def chat_stream(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[ChatChunk]:
        yield ChatChunk(kind=ChunkKind.CONTENT.value, text="收到")
        yield ChatChunk(kind=ChunkKind.DONE.value)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """每个用例使用全新的内存存储与配置缓存，环境变量侧密钥为空。"""
    reset_store()
    reset_runtime_config()
    monkeypatch.setattr(get_settings(), "llm_api_key", "")
    yield
    reset_store()
    reset_runtime_config()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """启动应用并借助 `ADMIN_EMAIL/ADMIN_PASSWORD` 播种初始管理员。"""
    monkeypatch.setenv("ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("ADMIN_USERNAME", "root")
    get_settings.cache_clear()
    # cache_clear 会重建 Settings，需对新对象再次压掉真实密钥
    monkeypatch.setattr(get_settings(), "llm_api_key", "")
    reset_runtime_config()

    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()
    reset_runtime_config()


@pytest.fixture
def admin_headers(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": ADMIN_EMAIL, "password": PASSWORD})
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


# ---------------------------------------------------------------- 权限
def test_admin_endpoints_reject_anonymous_and_regular_users(
    client: TestClient, admin_headers: dict[str, str], user_headers: dict[str, str]
) -> None:
    assert client.get("/api/v1/admin/config").status_code == 401
    assert client.get("/api/v1/admin/config", headers=user_headers).status_code == 403
    assert client.get("/api/v1/admin/users", headers=user_headers).status_code == 403
    assert client.put(
        "/api/v1/admin/sandbox", json={"enabled": True}, headers=user_headers
    ).status_code == 403

    assert client.get("/api/v1/admin/config", headers=admin_headers).status_code == 200


# ---------------------------------------------------------------- 加密与脱敏
def test_secret_is_encrypted_at_rest() -> None:
    """API-Key 以密文入库，脱敏视图不含明文，清除后配置行被删除。"""

    async def scenario() -> None:
        store = MemoryStore()
        service = RuntimeConfigService(store=store, settings=get_settings())

        await service.update({"llm_api_key": SECRET}, updated_by="admin-1")

        item = await store.get_config("llm_api_key")
        assert item is not None
        assert item.encrypted is True
        assert is_encrypted(item.value)
        assert SECRET not in item.value
        assert decrypt_value(item.value) == SECRET
        assert item.updated_by == "admin-1"

        assert (await service.load(force=True)).llm_api_key == SECRET

        view = await service.public_view()
        assert SECRET not in json.dumps(view, default=str)
        assert "*" in view["llm_api_key"]
        assert view["llm_api_key_configured"] is True

        # 清除：删除配置行，回落环境变量
        await service.update({"llm_api_key": ""}, updated_by="admin-1")
        assert await store.get_config("llm_api_key") is None
        assert (await service.load(force=True)).llm_api_key == ""

    asyncio.run(scenario())


def test_non_secret_config_is_stored_in_plain_text() -> None:
    """非敏感项（布尔 / 数值）明文入库，读取时正确还原类型。"""

    async def scenario() -> None:
        store = MemoryStore()
        service = RuntimeConfigService(store=store, settings=get_settings())

        await service.update(
            {"sandbox_global_enabled": True, "max_steps": 7}, updated_by="admin-1"
        )

        flag = await store.get_config("sandbox_global_enabled")
        assert flag is not None and flag.value == "true" and flag.encrypted is False

        snapshot = await service.load(force=True)
        assert snapshot.sandbox_global_enabled is True
        assert snapshot.max_steps == 7

    asyncio.run(scenario())


def test_config_response_never_leaks_plain_key(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.put(
        "/api/v1/admin/config", json={"llm_api_key": SECRET}, headers=admin_headers
    )
    assert response.status_code == 200, response.text
    assert SECRET not in response.text

    body = response.json()
    assert body["llm_api_key_configured"] is True
    assert "*" in body["llm_api_key"]

    fetched = client.get("/api/v1/admin/config", headers=admin_headers)
    assert SECRET not in fetched.text
    assert fetched.json()["llm_api_key_configured"] is True


def test_clearing_api_key_falls_back_to_environment(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    client.put("/api/v1/admin/config", json={"llm_api_key": SECRET}, headers=admin_headers)
    assert client.get("/api/v1/admin/config", headers=admin_headers).json()[
        "llm_api_key_configured"
    ]

    cleared = client.put(
        "/api/v1/admin/config", json={"llm_api_key": ""}, headers=admin_headers
    )
    assert cleared.status_code == 200
    assert cleared.json()["llm_api_key_configured"] is False
    assert cleared.json()["llm_api_key"] == ""


# ---------------------------------------------------------------- 对对话生效
def test_chat_uses_key_configured_from_admin_backend(
    client: TestClient,
    admin_headers: dict[str, str],
    user_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 未配置密钥：维持 M1 行为
    blocked = client.post("/api/v1/chat", json={"message": "hi"}, headers=user_headers)
    assert blocked.status_code == 503
    assert blocked.json()["code"] == "API_KEY_NOT_CONFIGURED"

    client.put("/api/v1/admin/config", json={"llm_api_key": SECRET}, headers=admin_headers)

    monkeypatch.setattr(chat_api, "build_llm_client", lambda _config: EchoLLM())
    body = client.post("/api/v1/chat", json={"message": "hi"}, headers=user_headers)
    assert body.status_code == 200, body.text
    assert "event: text" in body.content.decode("utf-8")


def test_chat_follows_default_model_from_admin_backend(
    client: TestClient,
    admin_headers: dict[str, str],
    user_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后台改默认模型后，新建会话应带上该模型。"""
    client.put("/api/v1/admin/config", json={"llm_api_key": SECRET}, headers=admin_headers)
    client.put(
        "/api/v1/admin/config",
        json={"default_model": "deepseek-reasoner"},
        headers=admin_headers,
    )

    captured: dict[str, Any] = {}

    def fake_build(config: Any) -> EchoLLM:
        captured["config"] = config
        return EchoLLM()

    monkeypatch.setattr(chat_api, "build_llm_client", fake_build)
    assert client.post("/api/v1/chat", json={"message": "hi"}, headers=user_headers).status_code == 200

    assert captured["config"].api_key == SECRET
    assert captured["config"].model == "deepseek-reasoner"
    assert captured["config"].provider == "deepseek"

    sessions = client.get("/api/v1/sessions", headers=user_headers).json()
    assert sessions[0]["model"] == "deepseek-reasoner"


# ---------------------------------------------------------------- 局部更新与校验
def test_partial_update_keeps_other_fields(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    baseline = client.get("/api/v1/admin/config", headers=admin_headers).json()

    updated = client.put(
        "/api/v1/admin/config",
        json={"max_steps": 7},
        headers=admin_headers,
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["max_steps"] == 7
    assert body["default_model"] == baseline["default_model"]
    assert body["llm_base_url"] == baseline["llm_base_url"]
    assert body["updated_at"] is not None


def test_invalid_values_are_rejected(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    bad_url = client.put(
        "/api/v1/admin/config", json={"llm_base_url": "ftp://example.com"}, headers=admin_headers
    )
    assert bad_url.status_code == 422
    assert bad_url.json()["code"] == "VALIDATION_ERROR"

    bad_steps = client.put("/api/v1/admin/config", json={"max_steps": 0}, headers=admin_headers)
    assert bad_steps.status_code == 422


# ---------------------------------------------------------------- 沙箱与用户
def test_sandbox_global_toggle(client: TestClient, admin_headers: dict[str, str]) -> None:
    assert (
        client.get("/api/v1/admin/config", headers=admin_headers).json()[
            "sandbox_global_enabled"
        ]
        is False
    )

    enabled = client.put("/api/v1/admin/sandbox", json={"enabled": True}, headers=admin_headers)
    assert enabled.status_code == 200
    assert enabled.json()["sandbox_global_enabled"] is True
    assert (
        client.get("/api/v1/admin/config", headers=admin_headers).json()[
            "sandbox_global_enabled"
        ]
        is True
    )

    disabled = client.put("/api/v1/admin/sandbox", json={"enabled": False}, headers=admin_headers)
    assert disabled.json()["sandbox_global_enabled"] is False


def test_admin_can_list_users(
    client: TestClient, admin_headers: dict[str, str], user_headers: dict[str, str]
) -> None:
    response = client.get("/api/v1/admin/users", headers=admin_headers)
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["total"] == 2
    roles = {item["role"] for item in body["items"]}
    assert roles == {"admin", "user"}
    # 响应中不得出现密码哈希等敏感字段
    assert "password_hash" not in json.dumps(body)
