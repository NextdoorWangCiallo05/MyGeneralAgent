"""M1 骨架端到端冒烟测试（pytest）。

覆盖：健康检查、注册/登录/鉴权、找回密码全流程、会话接口、SSE 前置校验、
工具安全求值、密码哈希。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import app.api.auth as auth_api
from app.core.security import hash_password, verify_password
from app.main import app
from app.store import reset_store
from app.tools.calculator import evaluate

PASSWORD = "secret123"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """每个用例重置内存存储，并拦截邮件发送以捕获重置令牌。"""
    reset_store()
    captured: dict[str, str] = {}

    async def fake_send_reset_email(to: str, token: str) -> None:
        captured["to"] = to
        captured["token"] = token

    monkeypatch.setattr(auth_api, "send_reset_email", fake_send_reset_email)
    yield captured
    reset_store()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _register(client: TestClient, username: str = "alice", email: str = "alice@example.com") -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={"username": username, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_register_login_me(client: TestClient) -> None:
    data = _register(client)
    headers = _auth(data["access_token"])

    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["username"] == "alice"
    assert me.json()["role"] == "user"

    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers=_auth("bogus")).status_code == 401

    login = client.post("/api/v1/auth/login", json={"username": "alice@example.com", "password": PASSWORD})
    assert login.status_code == 200


def test_register_rejects_duplicates_and_bad_input(client: TestClient) -> None:
    _register(client)

    dup = client.post(
        "/api/v1/auth/register",
        json={"username": "alice", "email": "other@example.com", "password": PASSWORD},
    )
    assert dup.status_code == 409
    assert dup.json()["code"] == "USER_EXISTS"

    bad = client.post(
        "/api/v1/auth/register",
        json={"username": "bo", "email": "not-an-email", "password": "x"},
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "VALIDATION_ERROR"


def test_login_rejects_wrong_password(client: TestClient) -> None:
    _register(client)
    response = client.post("/api/v1/auth/login", json={"username": "alice", "password": "wrong-password"})
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_CREDENTIALS"


def test_sessions_are_user_scoped(client: TestClient) -> None:
    headers = _auth(_register(client)["access_token"])

    created = client.post("/api/v1/sessions", json={}, headers=headers)
    assert created.status_code == 201
    session_id = created.json()["id"]

    assert len(client.get("/api/v1/sessions", headers=headers).json()) == 1
    assert client.get(f"/api/v1/sessions/{session_id}", headers=headers).status_code == 200

    other = _auth(_register(client, "bob", "bob@example.com")["access_token"])
    assert client.get("/api/v1/sessions", headers=other).json() == []
    assert client.get(f"/api/v1/sessions/{session_id}", headers=other).status_code == 404
    assert client.get(f"/api/v1/sessions/{session_id}/messages", headers=other).status_code == 404

    patched = client.patch(f"/api/v1/sessions/{session_id}", json={"title": "改个名"}, headers=headers)
    assert patched.status_code == 200
    assert patched.json()["title"] == "改个名"

    assert client.delete(f"/api/v1/sessions/{session_id}", headers=headers).status_code == 200
    assert client.get("/api/v1/sessions", headers=headers).json() == []


def test_chat_requires_api_key(client: TestClient) -> None:
    headers = _auth(_register(client)["access_token"])
    response = client.post("/api/v1/chat", json={"message": "1+1"}, headers=headers)
    assert response.status_code == 503
    assert response.json()["code"] == "API_KEY_NOT_CONFIGURED"


def test_password_reset_flow(client: TestClient, isolated_env: dict[str, str]) -> None:
    _register(client)

    generic = "若该邮箱已注册，我们已发送重置邮件，请查收"
    message = client.post("/api/v1/auth/forgot-password", json={"email": "alice@example.com"})
    assert message.status_code == 200
    assert message.json()["message"] == generic
    token = isolated_env["token"]

    # 未注册邮箱返回完全一致的响应，防枚举
    unknown = client.post("/api/v1/auth/forgot-password", json={"email": "nobody@example.com"})
    assert unknown.status_code == 200
    assert unknown.json()["message"] == generic

    invalid = client.post("/api/v1/auth/reset-password", json={"token": "invalid-token-value", "new_password": "newsecret123"})
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "INVALID_RESET_TOKEN"

    ok = client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": "newsecret123"})
    assert ok.status_code == 200

    reused = client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": "newsecret123"})
    assert reused.status_code == 400

    assert client.post("/api/v1/auth/login", json={"username": "alice", "password": "newsecret123"}).status_code == 200
    assert client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 401


def test_calculator_tool_is_safe() -> None:
    assert evaluate("12*(3+4)/2") == 42
    assert abs(evaluate("sqrt(2)") - 2**0.5) < 1e-12
    assert evaluate("round(2.567, 2)") == 2.57

    for expression in ["__import__('os').system('dir')", "9**9**9", "1/0", "open('x')", "a + 1"]:
        with pytest.raises(Exception):
            evaluate(expression)


def test_password_hashing() -> None:
    hashed = hash_password(PASSWORD)
    assert hashed != PASSWORD
    assert verify_password(PASSWORD, hashed)
    assert not verify_password("nope", hashed)
    assert hash_password(PASSWORD) != hashed  # 随机盐
    assert not verify_password(PASSWORD, "not-a-hash")
