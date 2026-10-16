"""端到端：以 SQL 后端运行完整 API，验证数据跨「应用重启」持久化。

M1 的内存实现一旦进程退出数据即丢失；本测试关闭并重新创建 TestClient
（等价于重启进程、重建连接池），确认注册用户与会话依然可查。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.crypto import decrypt_value
from app.core.runtime_config import reset_runtime_config
from app.main import app
from app.store import reset_store


@pytest.fixture
def sql_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """使用临时 SQLite 文件作为持久化后端（等价 MySQL 的持久化语义）。"""
    db_file = tmp_path / "agent_app.db"
    monkeypatch.setenv("STORE_BACKEND", "sql")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("AUTO_CREATE_TABLES", "true")
    get_settings.cache_clear()
    reset_store()
    yield db_file
    reset_store()
    get_settings.cache_clear()


def test_state_survives_app_restart(sql_backend: Path) -> None:
    headers: dict[str, str] = {}
    session_id = ""

    # ---- 第一次启动：注册 + 创建会话 ----
    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "persist-user",
                "email": "persist@example.com",
                "password": "Passw0rd!123",
            },
        )
        assert registered.status_code == 201, registered.text
        token = registered.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        created = client.post(
            "/api/v1/sessions", json={"title": "持久化验证"}, headers=headers
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]

        assert client.get("/api/v1/sessions", headers=headers).json()[0]["id"] == session_id

    # ---- 第二次启动：进程级状态已清空，数据应从数据库恢复 ----
    with TestClient(app) as client:
        profile = client.get("/api/v1/auth/me", headers=headers)
        assert profile.status_code == 200, profile.text
        assert profile.json()["username"] == "persist-user"

        sessions = client.get("/api/v1/sessions", headers=headers)
        assert sessions.status_code == 200
        assert [s["id"] for s in sessions.json()] == [session_id]
        assert sessions.json()[0]["title"] == "持久化验证"


def test_duplicate_registration_is_rejected(sql_backend: Path) -> None:
    payload = {
        "username": "dup-user",
        "email": "dup@example.com",
        "password": "Passw0rd!123",
    }
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/register", json=payload).status_code == 201
        # 唯一索引 + 业务校验双重保障，重复注册返回 409
        assert client.post("/api/v1/auth/register", json=payload).status_code == 409


def test_system_config_persists_across_restart(
    sql_backend: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """管理员写入的 API-Key 以密文落库，并在应用重启后依然生效。"""
    secret = "sk-sql-1234567890abcdef"
    admin_password = "Passw0rd!123"
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", admin_password)
    get_settings.cache_clear()

    def login(client: TestClient) -> dict[str, str]:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "admin@example.com", "password": admin_password},
        )
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    # ---- 第一次启动：管理员写入 API-Key ----
    with TestClient(app) as client:
        saved = client.put(
            "/api/v1/admin/config",
            json={"llm_api_key": secret},
            headers=login(client),
        )
        assert saved.status_code == 200, saved.text
        assert secret not in saved.text
        assert saved.json()["llm_api_key_configured"] is True

    # ---- 直接读库：敏感项必须是密文 ----
    with sqlite3.connect(f"file:{sql_backend.as_posix()}?mode=ro", uri=True) as conn:
        rows = {
            key: (value, encrypted)
            for key, value, encrypted in conn.execute(
                "SELECT key, value, encrypted FROM system_config"
            )
        }
    stored_value, encrypted = rows["llm_api_key"]
    assert encrypted == 1
    assert secret not in stored_value
    assert stored_value.startswith("enc:v1:")
    assert decrypt_value(stored_value) == secret

    # ---- 第二次启动：清空进程内缓存，配置应自数据库恢复 ----
    reset_runtime_config()
    with TestClient(app) as client:
        fetched = client.get("/api/v1/admin/config", headers=login(client))
        assert fetched.status_code == 200
        assert fetched.json()["llm_api_key_configured"] is True
        assert fetched.json()["llm_api_key"] != secret
        assert secret not in fetched.text
