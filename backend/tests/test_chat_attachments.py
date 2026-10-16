"""对话附件测试：上传解析、权限隔离、随消息注入上下文与重新生成。

分三层覆盖：
- 接口层：`POST/GET/DELETE /api/v1/attachments` 的解析、校验与越权防护；
- 链路层：`POST /api/v1/chat` 带 `attachment_ids` 时附件正文注入模型上下文、
  元数据快照落库、附件被绑定；
- 边界：数量上限、超长截断、重新生成时附件不丢、超时未发送的附件被清理。
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.attachments as attachments_api
import app.api.chat as chat_api
from app.config import get_settings
from app.llm.client import ChatChunk, ChunkKind
from app.main import app
from app.store import reset_store

PASSWORD = "secret123"
MANUAL = """# 部署手册

服务通过 docker compose 启动，默认监听 8000 端口。

## 回滚

执行 scripts/rollback.sh 即可回滚到上一个镜像版本。
"""


class ScriptedLLM:
    """按脚本返回固定文本的 LLM 替身（只关心送进去的 messages 与 tools）。"""

    seen_messages: list[dict[str, Any]] | None = None
    seen_tools: list[dict[str, Any]] | None = None

    def __init__(self, *_: Any, **__: Any) -> None: ...

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
        yield ChatChunk(kind=ChunkKind.CONTENT.value, text="收到，我来看看附件。")
        yield ChatChunk(kind=ChunkKind.DONE.value)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_store()
    ScriptedLLM.seen_messages = None
    ScriptedLLM.seen_tools = None
    # 清理节流是进程内状态，逐用例复位，保证清理逻辑总能被触发
    monkeypatch.setattr(attachments_api, "_last_purge_at", 0.0)
    monkeypatch.setattr(get_settings(), "llm_api_key", "test-key")
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: ScriptedLLM())
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
    filename: str = "manual.md",
    data: bytes = MANUAL.encode("utf-8"),
    content_type: str = "text/markdown",
):
    return client.post(
        "/api/v1/attachments",
        headers=headers,
        files={"file": (filename, data, content_type)},
    )


def _chat(
    client: TestClient,
    headers: dict[str, str],
    message: str,
    *,
    attachment_ids: list[str] | None = None,
    session_id: str | None = None,
    regenerate: bool = False,
) -> str:
    payload: dict[str, Any] = {"message": message, "regenerate": regenerate}
    if attachment_ids is not None:
        payload["attachment_ids"] = attachment_ids
    if session_id:
        payload["session_id"] = session_id
    response = client.post("/api/v1/chat", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.content.decode("utf-8")


def _session_id(stream: str) -> str:
    matched = re.search(r'"session_id":\s*"([^"]+)"', stream)
    assert matched is not None, stream[:400]
    return matched.group(1)


def _last_user_message(messages: list[dict[str, Any]] | None) -> str:
    assert messages, "模型没有收到任何消息"
    for item in reversed(messages):
        if item["role"] == "user":
            return str(item["content"])
    raise AssertionError("模型没有收到 user 消息")


def _system_message(messages: list[dict[str, Any]] | None) -> str:
    """取本轮下发的 system 提示：附件正文统一放在这里的「本会话文件」区。"""
    assert messages, "模型没有收到任何消息"
    return "\n".join(
        str(item["content"]) for item in messages if item["role"] == "system"
    )


def _tool_names() -> set[str]:
    """本轮真正下发给模型的工具名。"""
    return {item["function"]["name"] for item in (ScriptedLLM.seen_tools or [])}


# ---------------------------------------------------------------- 接口层
def test_upload_parses_text_and_returns_metadata(client: TestClient) -> None:
    headers = _register(client, "attacher")

    created = _upload(client, headers)

    assert created.status_code == 201, created.text
    attachment = created.json()
    assert attachment["filename"] == "manual.md"
    assert attachment["file_type"] == "md"
    assert attachment["size_bytes"] == len(MANUAL.encode("utf-8"))
    # 解析结果会去掉首尾空白，故字符数按 strip 后计
    assert attachment["char_count"] == len(MANUAL.strip())
    # 还没随消息发出
    assert attachment["bound"] is False


def test_upload_accepts_json_attachment(client: TestClient) -> None:
    """白名单与前端 accept 都承诺支持 json，接口层就必须真的收下（曾因解析器漏了 json 而 400）。"""
    headers = _register(client, "json-user")

    created = _upload(
        client,
        headers,
        filename="config.json",
        data='{"keyword": "Sirius-9"}'.encode("utf-8"),
        content_type="application/json",
    )

    assert created.status_code == 201, created.text
    attachment = created.json()
    assert attachment["file_type"] == "json"
    detail = client.get(f"/api/v1/attachments/{attachment['id']}", headers=headers)
    assert detail.json()["text"] == '{"keyword": "Sirius-9"}'


def test_upload_stores_long_text_without_truncation(client: TestClient) -> None:
    """长文档必须原样入库。

    回归：MySQL 的 `TEXT` 只有 65535 **字节**，上传一个 1 MB 的 PDF 就能抽出 4.9 万汉字
    （≈147 KB），插入时报 `DataError (1406) Data too long for column 'text'`，
    整个上传请求变成 500。列类型已放宽到 LONGTEXT，本用例守住「长文本不丢」。
    """
    headers = _register(client, "long-doc")
    text = "未发出的消息 测试中文内容 " * 5000
    body = text.encode("utf-8")
    assert len(body) > 65535  # 必须超过 MySQL TEXT 的字节上限才有意义

    created = _upload(client, headers, filename="long.txt", data=body)

    assert created.status_code == 201, created.text
    assert created.json()["char_count"] == len(text.strip())
    detail = client.get(f"/api/v1/attachments/{created.json()['id']}", headers=headers)
    assert detail.json()["text"] == text.strip()
    assert detail.json()["truncated"] is False


def test_unhandled_error_returns_json_envelope_with_cors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未捕获异常要变成带 CORS 头的 JSON 500。

    回归：异常若冒泡到最外层，响应不经过 CORSMiddleware，浏览器读不到任何 body，
    前端会把「服务端 500」显示成「无法连接后端服务，请确认后端已启动」，
    把「后端有 bug」误报成「后端没启动」，排查方向被带偏。
    """
    headers = _register(client, "boom")

    def _boom(*_: Any, **__: Any) -> str:
        raise RuntimeError("模拟未预期的内部错误")

    monkeypatch.setattr(attachments_api, "parse_document", _boom)

    response = client.post(
        "/api/v1/attachments",
        headers={**headers, "Origin": "http://localhost:3000"},
        files={"file": ("boom.txt", "内容".encode(), "text/plain")},
    )

    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    # 关键：500 也要能被浏览器读到（缺这个头前端就只能显示「无法连接后端服务」）
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_upload_rejects_bad_type_empty_and_oversize(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _register(client, "validator")

    bad_type = _upload(client, headers, filename="run.exe", data=b"MZ\x90\x00")
    assert bad_type.status_code == 400
    assert "UNSUPPORTED_DOCUMENT" in bad_type.text

    empty = _upload(client, headers, filename="empty.txt", data=b"")
    assert empty.status_code == 400
    assert "DOCUMENT_PARSE_ERROR" in empty.text

    # 超长文件名要在入库前拒绝，不能等到 INSERT 报 Data too long 变成 500
    too_long = _upload(client, headers, filename=f"{'长' * 300}.txt")
    assert too_long.status_code == 400
    assert "文件名过长" in too_long.text

    monkeypatch.setattr(get_settings(), "attachment_max_file_bytes", 16)
    oversize = _upload(client, headers)
    assert oversize.status_code == 413
    assert "DOCUMENT_TOO_LARGE" in oversize.text


def test_upload_requires_authentication(client: TestClient) -> None:
    response = client.post(
        "/api/v1/attachments", files={"file": ("a.txt", b"hello", "text/plain")}
    )
    assert response.status_code == 401


def test_detail_returns_parsed_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _register(client, "previewer")
    attachment_id = _upload(client, headers).json()["id"]

    detail = client.get(f"/api/v1/attachments/{attachment_id}", headers=headers)

    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert "回滚" in payload["text"]
    assert payload["truncated"] is False

    # 预览长度受 _PREVIEW_MAX_CHARS 约束，超出时置 truncated
    monkeypatch.setattr(attachments_api, "_PREVIEW_MAX_CHARS", 5)
    short = client.get(f"/api/v1/attachments/{attachment_id}", headers=headers).json()
    assert len(short["text"]) == 5
    assert short["truncated"] is True


def test_attachments_are_isolated_between_users(client: TestClient) -> None:
    owner = _register(client, "owner")
    other = _register(client, "intruder")
    attachment_id = _upload(client, owner).json()["id"]

    assert (
        client.get(f"/api/v1/attachments/{attachment_id}", headers=other).status_code == 404
    )
    assert (
        client.delete(f"/api/v1/attachments/{attachment_id}", headers=other).status_code
        == 404
    )

    removed = client.delete(f"/api/v1/attachments/{attachment_id}", headers=owner)
    assert removed.status_code == 200
    assert (
        client.get(f"/api/v1/attachments/{attachment_id}", headers=owner).status_code == 404
    )


# ---------------------------------------------------------------- 对话链路
def test_chat_injects_attachment_text_into_context(client: TestClient) -> None:
    headers = _register(client, "chatter")
    attachment_id = _upload(client, headers).json()["id"]

    _chat(client, headers, "这份手册怎么回滚？", attachment_ids=[attachment_id])

    # 正文放在 system 的「本会话文件」区（会话级资源，见 _render_session_files）
    system = _system_message(ScriptedLLM.seen_messages)
    assert "【附件 1】manual.md" in system
    assert "<附件内容>" in system
    assert "scripts/rollback.sh" in system
    # 用户消息保留原文，并点明本轮新上传了哪份文件
    content = _last_user_message(ScriptedLLM.seen_messages)
    assert content.startswith("这份手册怎么回滚？")
    assert "（本轮新上传：manual.md" in content


def test_chat_persists_attachment_metadata_and_binds_it(client: TestClient) -> None:
    headers = _register(client, "historian")
    attachment_id = _upload(client, headers).json()["id"]

    session_id = _session_id(
        _chat(client, headers, "看看这个", attachment_ids=[attachment_id])
    )

    messages = client.get(
        f"/api/v1/sessions/{session_id}/messages", headers=headers
    ).json()
    user_message = next(item for item in messages if item["role"] == "user")
    assert [item["id"] for item in user_message["attachments"]] == [attachment_id]
    assert user_message["attachments"][0]["filename"] == "manual.md"

    # 绑定后不再属于「上传了但没发送」，过期清理不会动它
    detail = client.get(f"/api/v1/attachments/{attachment_id}", headers=headers).json()
    assert detail["bound"] is True


def test_chat_rejects_missing_or_foreign_attachments(client: TestClient) -> None:
    headers = _register(client, "poster")
    other = _register(client, "snooper")
    foreign_id = _upload(client, other).json()["id"]

    missing = client.post(
        "/api/v1/chat",
        json={"message": "在吗", "attachment_ids": ["not-exist"]},
        headers=headers,
    )
    assert missing.status_code == 404
    assert "ATTACHMENT_NOT_FOUND" in missing.text

    stolen = client.post(
        "/api/v1/chat",
        json={"message": "在吗", "attachment_ids": [foreign_id]},
        headers=headers,
    )
    assert stolen.status_code == 404
    assert "ATTACHMENT_NOT_FOUND" in stolen.text


def test_chat_rejects_too_many_attachments(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "attachment_max_per_message", 1)
    headers = _register(client, "crowder")
    first = _upload(client, headers, filename="a.txt", data=b"aaa").json()["id"]
    second = _upload(client, headers, filename="b.txt", data=b"bbb").json()["id"]

    response = client.post(
        "/api/v1/chat",
        json={"message": "都看看", "attachment_ids": [first, second]},
        headers=headers,
    )

    assert response.status_code == 400
    assert "TOO_MANY_ATTACHMENTS" in response.text


def test_chat_truncates_oversized_attachment_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "attachment_max_chars", 10)
    headers = _register(client, "truncator")
    attachment_id = _upload(client, headers).json()["id"]

    _chat(client, headers, "总结一下", attachment_ids=[attachment_id])

    system = _system_message(ScriptedLLM.seen_messages)
    assert "附件过长" in system
    # 截断处必须指向工具：模型能自己把剩下的部分补读回来，而不是只能干等用户重传
    assert "read_attachment" in system
    assert "rollback.sh" not in system


def test_chat_injects_attachment_longer_than_previous_limit(client: TestClient) -> None:
    """超过旧默认上限（2 万字符）的附件必须**整篇**注入。

    回归：`ATTACHMENT_MAX_CHARS` 默认 2 万时，长文档在第 2 万字符处被截断，
    模型只能看到前 2 万字，用户在输入框里也看不出「后半段没送进去」。
    """
    headers = _register(client, "long-inject")
    text = "".join(f"第{index}行：内容要点\n" for index in range(1, 4001))
    assert 20_000 < len(text) < get_settings().attachment_max_chars

    created = _upload(
        client,
        headers,
        filename="long.txt",
        data=text.encode("utf-8"),
        content_type="text/plain",
    )
    _chat(client, headers, "把全文要点列出来", attachment_ids=[created.json()["id"]])

    system = _system_message(ScriptedLLM.seen_messages)
    assert "附件过长" not in system
    assert "第4000行：内容要点" in system  # 末尾也在上下文里，没有被腰斩


def test_regenerate_keeps_attachment(client: TestClient) -> None:
    headers = _register(client, "regenerator")
    attachment_id = _upload(client, headers).json()["id"]

    session_id = _session_id(
        _chat(client, headers, "这份手册讲了什么？", attachment_ids=[attachment_id])
    )
    _chat(
        client,
        headers,
        "这份手册讲了什么？",
        session_id=session_id,
        regenerate=True,
    )

    # 重新生成时附件按库中记录恢复，模型依然能读到正文
    assert "scripts/rollback.sh" in _system_message(ScriptedLLM.seen_messages)

    messages = client.get(
        f"/api/v1/sessions/{session_id}/messages", headers=headers
    ).json()
    users = [item for item in messages if item["role"] == "user"]
    assert len(users) == 1
    assert [item["id"] for item in users[0]["attachments"]] == [attachment_id]


def test_stale_unbound_attachments_are_purged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _register(client, "janitor")
    bound_id = _upload(client, headers, filename="bound.txt", data=b"keep me").json()["id"]
    _chat(client, headers, "先发一个", attachment_ids=[bound_id])

    orphan_id = _upload(client, headers, filename="orphan.txt", data=b"drop me").json()["id"]

    # 把 TTL 设为 0 后再次上传：触发清理，之前没发送的附件被回收
    monkeypatch.setattr(get_settings(), "attachment_ttl_seconds", 0)
    monkeypatch.setattr(attachments_api, "_last_purge_at", 0.0)
    time.sleep(0.02)
    _upload(client, headers, filename="fresh.txt", data=b"new one")

    assert client.get(f"/api/v1/attachments/{orphan_id}", headers=headers).status_code == 404
    # 已随消息发出的附件不受清理影响
    kept = client.get(f"/api/v1/attachments/{bound_id}", headers=headers)
    assert kept.status_code == 200
    assert kept.json()["bound"] is True


# ------------------------------------------------- 会话级文件（整个对话都可调用）
def test_session_files_remain_available_in_later_turns(client: TestClient) -> None:
    """上传一次后，后续每一轮都还能用这份文件。

    旧行为：附件正文只注入「上传它的那一轮」，第二轮起模型就看不见文件了，
    用户追问只能得到"我看不到附件"。
    """
    headers = _register(client, "session-files")
    attachment_id = _upload(client, headers).json()["id"]

    session_id = _session_id(
        _chat(client, headers, "这份手册怎么回滚？", attachment_ids=[attachment_id])
    )
    assert "scripts/rollback.sh" in _system_message(ScriptedLLM.seen_messages)

    # 第二轮不再传 attachment_ids：文件已经在会话里了，正文必须照旧在场
    _chat(client, headers, "那要回滚到哪个版本？", session_id=session_id)

    system = _system_message(ScriptedLLM.seen_messages)
    assert "【附件 1】manual.md" in system
    assert "scripts/rollback.sh" in system
    # 这一轮用户没上传东西，就不该出现"本轮新上传"的提示
    assert "本轮新上传" not in _last_user_message(ScriptedLLM.seen_messages)
    # 会话里有文件时才下发读取工具，模型能自己补读被截断/未展开的部分
    assert "read_attachment" in _tool_names()


def test_session_file_budget_demotes_files_to_tool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """会话总预算兜底：装不下的文件只列清单，交给 read_attachment 按段读。

    没有这层兜底，"整个对话都能用"就等于每一轮把全部附件重发一遍，
    多传几份就会把上下文顶穿（直接报 LLM_ERROR）。
    """
    monkeypatch.setattr(get_settings(), "attachment_context_max_chars", 20)
    headers = _register(client, "budgeter")
    old_id = _upload(client, headers, filename="old.md").json()["id"]
    new_id = _upload(
        client, headers, filename="new.md", data=b"NEW-FILE-BODY", content_type="text/plain"
    ).json()["id"]

    session_id = _session_id(_chat(client, headers, "先看这份", attachment_ids=[old_id]))
    _chat(client, headers, "再看这份", session_id=session_id, attachment_ids=[new_id])

    system = _system_message(ScriptedLLM.seen_messages)
    # 预算被调成 20 字：两份文件都不展开正文（避免只展开几行碎片误导模型）
    assert "【附件 1】old.md" in system
    assert "【附件 2】new.md" in system
    assert system.count("未整体展开：会话内附件总量") == 2
    assert "NEW-FILE-BODY" not in system
    assert "scripts/rollback.sh" not in system
    # 清单里必须给 id，模型才知道该读谁
    assert f'attachment_id="{old_id}"' in system
    assert f'attachment_id="{new_id}"' in system
    assert "read_attachment" in _tool_names()


def test_attachment_tool_not_offered_without_files(client: TestClient) -> None:
    """会话里没有附件时不下发 read_attachment，别让模型去调注定失败的工具。"""
    headers = _register(client, "no-files")

    _chat(client, headers, "今天几号？")

    assert "read_attachment" not in _tool_names()


def test_session_files_endpoint_lists_current_files(client: TestClient) -> None:
    """会话文件清单要反映「当前」而不是「历史」。

    消息里的附件只是发送当时的快照，删了文件它也不会变；前端靠这个接口才知道
    会话里如今还剩哪些文件（"本会话文件" 那条常驻栏就是这么来的）。
    """
    headers = _register(client, "file-list")
    manual_id = _upload(client, headers).json()["id"]
    notes_id = _upload(
        client, headers, filename="notes.txt", data=b"NOTES-BODY", content_type="text/plain"
    ).json()["id"]
    unsent_id = _upload(
        client, headers, filename="draft.txt", data=b"DRAFT-BODY", content_type="text/plain"
    ).json()["id"]

    session_id = _session_id(
        _chat(client, headers, "两份都看下", attachment_ids=[manual_id, notes_id])
    )

    listed = client.get(f"/api/v1/sessions/{session_id}/attachments", headers=headers)
    assert listed.status_code == 200, listed.text
    files = listed.json()
    # 按上传时间正序，且都已绑定到会话
    assert [item["filename"] for item in files] == ["manual.md", "notes.txt"]
    assert all(item["bound"] for item in files)
    # 上传了却没随消息发出的附件不属于任何会话
    assert unsent_id not in {item["id"] for item in files}

    # 会话不存在或不属于自己 → 404，不泄露别人会话里放的是什么文件
    outsider = _register(client, "file-list-outsider")
    assert (
        client.get(f"/api/v1/sessions/{session_id}/attachments", headers=outsider).status_code
        == 404
    )
    # 别人的会话列表同样是空的
    assert client.get("/api/v1/sessions", headers=outsider).json() == []


def test_deleting_session_file_drops_it_from_list_and_context(client: TestClient) -> None:
    """用户可以在会话里自己删文件：清单少一项、下一轮不再注入它的正文。

    会话文件默认一直保留（只有未发送的附件会被 TTL 清理），删除是用户显式动作——
    删掉之后模型也就看不到它了，避免"我以为删了它还在读"的错觉。
    """
    headers = _register(client, "file-delete")
    manual_id = _upload(client, headers).json()["id"]
    notes_id = _upload(
        client, headers, filename="notes.txt", data=b"NOTES-BODY", content_type="text/plain"
    ).json()["id"]

    session_id = _session_id(
        _chat(client, headers, "两份都看下", attachment_ids=[manual_id, notes_id])
    )

    deleted = client.delete(f"/api/v1/attachments/{manual_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text

    remaining = client.get(f"/api/v1/sessions/{session_id}/attachments", headers=headers).json()
    assert [item["filename"] for item in remaining] == ["notes.txt"]

    # 消息里的快照不动：那是"当初发过什么"的历史记录（前端据此把卡片标为已删除）
    history = client.get(f"/api/v1/sessions/{session_id}/messages", headers=headers).json()
    first_user = next(item for item in history if item["role"] == "user")
    assert [item["filename"] for item in first_user["attachments"]] == ["manual.md", "notes.txt"]
    # 原文也读不到了
    assert client.get(f"/api/v1/attachments/{manual_id}", headers=headers).status_code == 404

    # 下一轮：被删的文件不再进上下文，剩下的照旧可读
    _chat(client, headers, "剩下这份总结下", session_id=session_id)

    system = _system_message(ScriptedLLM.seen_messages)
    assert "【附件 1】notes.txt" in system
    assert "NOTES-BODY" in system
    assert "manual.md" not in system
    assert "scripts/rollback.sh" not in system
