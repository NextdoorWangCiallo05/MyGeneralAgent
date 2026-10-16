"""知识库（M4）测试：文本分块、文档解析、上传 / 预览 / 删除与权限隔离。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.chunking import split_text
from app.core.document_parser import parse_document
from app.core.errors import UnsupportedDocumentError
from app.main import app
from app.store import reset_store

PASSWORD = "secret123"
MARKDOWN = """# 产品说明

本项目是一个多轮对话 Agent，支持联网搜索与代码沙箱工具。

## 部署

使用 docker compose 启动，默认监听 8000 端口。
"""


@pytest.fixture(autouse=True)
def isolated() -> Iterator[None]:
    reset_store()
    yield
    reset_store()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _register(client: TestClient, username: str) -> dict[str, str]:
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
    data: bytes = MARKDOWN.encode("utf-8"),
    content_type: str = "text/markdown",
):
    return client.post(
        "/api/v1/knowledge/documents",
        headers=headers,
        files={"file": (filename, data, content_type)},
    )


# ---------------------------------------------------------------- 分块
def test_split_text_produces_overlapping_chunks() -> None:
    text = "".join(f"第{index}句：这是一段用于测试分块的中文内容。" for index in range(1, 61))

    chunks = split_text(text, chunk_size=200, overlap=20)

    assert len(chunks) > 1
    assert all(len(chunk) <= 200 for chunk in chunks)
    # 相邻片段保留重叠窗口，避免答案被切在边界上
    # （片段较长时重叠会被压缩，因此只校验「确实存在重叠且来自上一片段尾部」）
    carried = chunks[1].split("\n", 1)[0]
    assert carried
    assert len(carried) <= 20
    assert chunks[0].endswith(carried)


def test_split_text_handles_empty_and_short_text() -> None:
    assert split_text("   \n\n ") == []
    assert split_text("短文本") == ["短文本"]


# ---------------------------------------------------------------- 解析
def test_parse_document_decodes_utf8_and_gb18030() -> None:
    assert parse_document("a.txt", "中文内容".encode("utf-8")).strip() == "中文内容"
    assert parse_document("a.txt", "中文内容".encode("gb18030")).strip() == "中文内容"


def test_parse_document_reads_structured_text_and_drops_bom() -> None:
    """json / csv 按纯文本读取（白名单里有它们，解析器就不能漏），且不把 BOM 带进正文。"""
    assert parse_document("config.json", '{"name": "demo"}'.encode()).strip() == '{"name": "demo"}'
    # Windows 记事本 / Excel 导出的文件常带 UTF-8 BOM
    assert parse_document("bom.txt", "\ufeff第一行".encode()) == "第一行"
    assert parse_document("bom.json", "\ufeff{}".encode()).startswith("{")


def test_parse_document_rejects_unsupported_type() -> None:
    with pytest.raises(UnsupportedDocumentError):
        parse_document("archive.zip", b"PK\x03\x04")


# ---------------------------------------------------------------- 接口
def test_upload_list_preview_and_delete(client: TestClient) -> None:
    headers = _register(client, "reader")

    created = _upload(client, headers)
    assert created.status_code == 201, created.text
    document = created.json()
    assert document["filename"] == "guide.md"
    assert document["file_type"] == "md"
    assert document["status"] == "ready"
    assert document["chunk_count"] >= 1
    assert document["char_count"] == len(MARKDOWN)
    # 默认 EMBEDDING_BACKEND=hash：上传即完成向量化（详见 test_knowledge_search.py）
    assert document["embedded"] is True

    listed = client.get("/api/v1/knowledge/documents", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["embedded"] is True

    preview = client.get(
        f"/api/v1/knowledge/documents/{document['id']}/chunks", headers=headers
    )
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["total"] == document["chunk_count"]
    assert payload["items"][0]["seq"] == 0
    assert "产品说明" in payload["items"][0]["content"]
    assert payload["items"][0]["has_embedding"] is True

    removed = client.delete(
        f"/api/v1/knowledge/documents/{document['id']}", headers=headers
    )
    assert removed.status_code == 200
    assert client.get("/api/v1/knowledge/documents", headers=headers).json()["total"] == 0
    assert (
        client.get(
            f"/api/v1/knowledge/documents/{document['id']}/chunks", headers=headers
        ).status_code
        == 404
    )


def test_upload_rejects_unsupported_type_and_empty_file(client: TestClient) -> None:
    headers = _register(client, "writer")

    bad_type = _upload(client, headers, filename="payload.exe", data=b"MZ\x90\x00")
    assert bad_type.status_code == 400
    assert "UNSUPPORTED_DOCUMENT" in bad_type.text

    empty = _upload(client, headers, filename="empty.txt", data=b"")
    assert empty.status_code == 400
    assert "DOCUMENT_PARSE_ERROR" in empty.text


def test_upload_rejects_oversize_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "knowledge_max_file_bytes", 16)
    headers = _register(client, "bigfile")

    response = _upload(client, headers)

    assert response.status_code == 413
    assert "DOCUMENT_TOO_LARGE" in response.text


def test_documents_are_isolated_between_users(client: TestClient) -> None:
    owner = _register(client, "owner")
    other = _register(client, "intruder")
    document_id = _upload(client, owner).json()["id"]

    assert (
        client.get(
            f"/api/v1/knowledge/documents/{document_id}/chunks", headers=other
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/api/v1/knowledge/documents/{document_id}", headers=other).status_code
        == 404
    )
    assert client.get("/api/v1/knowledge/documents", headers=other).json()["total"] == 0
    # 越权删除不影响属主文档
    assert client.get("/api/v1/knowledge/documents", headers=owner).json()["total"] == 1


def test_upload_requires_authentication(client: TestClient) -> None:
    response = client.post(
        "/api/v1/knowledge/documents",
        files={"file": ("a.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 401
