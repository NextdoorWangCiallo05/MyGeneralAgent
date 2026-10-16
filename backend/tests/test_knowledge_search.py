"""知识库检索（M4 后半程）测试：向量化、重建索引、召回、工具门控与 Agent 下发。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.chat as chat_api
from app.config import get_settings
from app.core.embedding import HashEmbedder, cosine_similarity, reset_embedder
from app.llm.client import ChatChunk, ChunkKind
from app.main import app
from app.store import reset_store
from app.tools.context import ToolContext, reset_tool_context, set_tool_context
from app.tools.knowledge import _knowledge_search
from app.tools.registry import build_default_registry

PASSWORD = "secret123"

GUIDE = """# 部署手册

本项目使用 docker compose 部署，默认监听 8000 端口，日志目录为 /var/log/agent。

## 回滚

出现异常时执行 make rollback 回滚到上一个镜像版本。
"""

EXPENSE = """# 报销制度

差旅费报销需要提供发票与行程单，报销周期为十五个工作日。
"""


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_store()
    reset_embedder()
    # 默认用零依赖的 hash 后端，避免测试依赖 torch / 模型下载
    monkeypatch.setattr(get_settings(), "embedding_backend", "hash")
    monkeypatch.setattr(get_settings(), "embedding_dim", 256)
    monkeypatch.setattr(get_settings(), "llm_api_key", "test-key")
    yield
    reset_store()
    reset_embedder()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


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


def _upload(
    client: TestClient,
    headers: dict[str, str],
    *,
    filename: str = "guide.md",
    text: str = GUIDE,
):
    return client.post(
        "/api/v1/knowledge/documents",
        headers=headers,
        files={"file": (filename, text.encode("utf-8"), "text/markdown")},
    )


def _user_id(client: TestClient, headers: dict[str, str]) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["id"]


def _run_tool(user_id: str, query: str) -> Any:
    token = set_tool_context(ToolContext(user_id=user_id, session_id="session-1"))
    try:
        return asyncio.run(_knowledge_search(query))
    finally:
        reset_tool_context(token)


class CapturingLLM:
    """记录下发给模型的 tools，并直接返回一段文本。"""

    tools_seen: list[list[dict[str, Any]]] = []

    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[ChatChunk]:
        CapturingLLM.tools_seen.append(list(tools or []))
        yield ChatChunk(kind=ChunkKind.CONTENT.value, text="好的")
        yield ChatChunk(kind=ChunkKind.DONE.value)


# ---------------------------------------------------------------- 向量化
def test_hash_embedder_is_normalized_and_ranks_related_text_higher() -> None:
    embedder = HashEmbedder(dim=256)

    vectors = asyncio.run(
        embedder.encode(["部署后默认监听 8000 端口", "默认端口是 8000", "今天天气不错"])
    )

    assert all(abs(sum(value * value for value in vector) - 1) < 1e-6 for vector in vectors)
    assert cosine_similarity(vectors[0], vectors[1]) > cosine_similarity(vectors[0], vectors[2])
    assert len(vectors[0]) == 256


def test_upload_stores_vectors_and_marks_document(client: TestClient) -> None:
    headers = _register(client)

    created = _upload(client, headers)

    assert created.status_code == 201, created.text
    assert created.json()["embedded"] is True

    documents = client.get("/api/v1/knowledge/documents", headers=headers).json()["items"]
    assert documents[0]["embedded"] is True

    chunks = client.get(
        f"/api/v1/knowledge/documents/{documents[0]['id']}/chunks", headers=headers
    ).json()["items"]
    assert chunks and all(chunk["has_embedding"] for chunk in chunks)


# ---------------------------------------------------------------- 检索
def test_search_returns_most_relevant_document(client: TestClient) -> None:
    headers = _register(client)
    _upload(client, headers)
    _upload(client, headers, filename="expense.md", text=EXPENSE)

    response = client.get(
        "/api/v1/knowledge/search",
        params={"q": "部署之后默认监听哪个端口"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["backend"] == "hash"
    assert payload["items"]
    assert payload["items"][0]["filename"] == "guide.md"
    assert "8000" in payload["items"][0]["content"]


def test_search_is_scoped_to_document_owner(client: TestClient) -> None:
    owner = _register(client, "owner")
    _upload(client, owner)
    stranger = _register(client, "stranger")

    response = client.get(
        "/api/v1/knowledge/search", params={"q": "docker compose 部署端口"}, headers=stranger
    )

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_search_requires_enabled_embedding_backend(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "embedding_backend", "none")
    reset_embedder()
    headers = _register(client)

    response = client.get(
        "/api/v1/knowledge/search", params={"q": "任意查询"}, headers=headers
    )

    assert response.status_code == 400
    assert response.json()["code"] == "EMBEDDING_UNAVAILABLE"


def test_reindex_backfills_missing_vectors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    # 先关闭向量化上传，模拟「入库时没有嵌入后端」的历史数据
    monkeypatch.setattr(settings, "embedding_backend", "none")
    reset_embedder()
    headers = _register(client, "reindexer")
    created = _upload(client, headers)
    assert created.status_code == 201, created.text
    assert created.json()["embedded"] is False

    monkeypatch.setattr(settings, "embedding_backend", "hash")
    reset_embedder()
    result = client.post("/api/v1/knowledge/reindex", headers=headers)

    assert result.status_code == 200, result.text
    assert result.json()["embedded"] >= 1
    assert result.json()["backend"] == "hash"

    found = client.get(
        "/api/v1/knowledge/search",
        params={"q": "docker compose 部署默认监听端口"},
        headers=headers,
    )
    assert found.status_code == 200
    assert found.json()["items"]


# ---------------------------------------------------------------- 工具
def test_knowledge_tool_returns_snippets_for_owner(client: TestClient) -> None:
    headers = _register(client, "tooluser")
    _upload(client, headers)

    result = _run_tool(_user_id(client, headers), "部署端口是多少")

    assert result.status == "success"
    assert result.data["hits"] >= 1
    assert "guide.md" in result.content
    assert "8000" in result.content


def test_knowledge_tool_does_not_leak_other_users(client: TestClient) -> None:
    owner = _register(client, "owner")
    _upload(client, owner)
    stranger = _register(client, "stranger")

    result = _run_tool(_user_id(client, stranger), "部署端口是多少")

    assert result.status == "success"
    assert result.data["hits"] == 0
    assert "8000" not in result.content


def test_registry_gates_knowledge_tool() -> None:
    registry = build_default_registry()

    assert "knowledge_search" in registry.available_names(
        sandbox_enabled=False, knowledge_enabled=True
    )
    assert "knowledge_search" not in registry.available_names(
        sandbox_enabled=False, knowledge_enabled=False
    )


def test_runtime_config_reports_embedding_backend(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _register(client)

    payload = client.get("/api/v1/config", headers=headers).json()
    assert payload["knowledge_enabled"] is True
    assert payload["knowledge_backend"] == "hash"
    assert payload["knowledge_tool"] == "knowledge_search"

    monkeypatch.setattr(get_settings(), "embedding_backend", "none")
    reset_embedder()
    payload = client.get("/api/v1/config", headers=headers).json()
    assert payload["knowledge_enabled"] is False
    assert payload["knowledge_backend"] == "none"
    assert payload["knowledge_tool"] is None


# ---------------------------------------------------------------- Agent 集成
def test_chat_offers_knowledge_tool_after_indexing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _register(client, "chatuser")
    _upload(client, headers)
    CapturingLLM.tools_seen = []
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: CapturingLLM())

    response = client.post(
        "/api/v1/chat", json={"message": "部署端口是多少"}, headers=headers
    )

    assert response.status_code == 200, response.text
    names = {item["function"]["name"] for item in CapturingLLM.tools_seen[0]}
    assert "knowledge_search" in names


def test_chat_hides_knowledge_tool_without_indexed_documents(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _register(client, "emptyuser")
    CapturingLLM.tools_seen = []
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: CapturingLLM())

    response = client.post("/api/v1/chat", json={"message": "你好"}, headers=headers)

    assert response.status_code == 200, response.text
    names = {item["function"]["name"] for item in CapturingLLM.tools_seen[0]}
    assert "knowledge_search" not in names
