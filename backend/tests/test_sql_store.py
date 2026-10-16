"""SqlStore 仓储契约测试（以 SQLite 异步内存库作为 MySQL 的等价替身）。

覆盖：用户查询与大小写不敏感登录、密码更新、会话用户隔离与软删除、
消息自增顺序与 JSON 往返、重置令牌一次性 / 过期 / 批量作废、
知识库分块的向量写入 / 筛选 / 回填。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta

import pytest

from app.config import get_settings
from app.db import dispose_engine, init_models
from app.store import AbstractStore, get_store, reset_store
from app.store.entities import utcnow

DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
def sql_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把配置切到 SQL 后端（SQLite 内存库）。"""
    monkeypatch.setenv("STORE_BACKEND", "sql")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    get_settings.cache_clear()
    reset_store()
    yield
    reset_store()
    get_settings.cache_clear()


async def _fresh_store() -> AbstractStore:
    """重建引擎与表结构，返回全新的 SqlStore（同一事件循环内使用）。"""
    await dispose_engine()
    reset_store()
    await init_models()
    return get_store()


def _run(coro) -> object:
    return asyncio.run(coro)


# ---------------------------------------------------------------- 用户
def test_user_lifecycle(sql_env: None) -> None:
    async def scenario() -> None:
        store = await _fresh_store()

        user = await store.create_user(
            username="Alice", email="Alice@Example.com", password_hash="hash-1"
        )
        assert user.id
        assert user.email == "alice@example.com", "邮箱应统一小写入库"
        assert user.role == "user"
        assert not user.is_admin and user.is_active

        assert (await store.get_user(user.id)).username == "Alice"
        assert (await store.get_user_by_username("alice")).id == user.id
        assert (await store.get_user_by_email("ALICE@EXAMPLE.COM")).id == user.id
        # 用户名或邮箱均可登录，且大小写不敏感
        assert (await store.get_user_by_login("ALICE")).id == user.id
        assert (await store.get_user_by_login("alice@example.com")).id == user.id
        assert await store.get_user_by_login("nobody") is None
        assert len(await store.list_users()) == 1

        await store.update_password(user.id, "hash-2")
        assert (await store.get_user(user.id)).password_hash == "hash-2"

        await dispose_engine()

    _run(scenario())


# ---------------------------------------------------------------- 会话
def test_session_scoping_and_soft_delete(sql_env: None) -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        alice = await store.create_user(username="alice", email="a@x.com", password_hash="h")
        bob = await store.create_user(username="bob", email="b@x.com", password_hash="h")

        session = await store.create_session(user_id=alice.id, title="新会话", model="deepseek-flash")
        assert session.title == "新会话" and session.deleted_at is None

        assert [s.id for s in await store.list_sessions(alice.id)] == [session.id]
        assert await store.list_sessions(bob.id) == [], "会话必须按用户隔离"

        updated = await store.update_session(session, title="改名了", sandbox_enabled=True)
        assert updated.title == "改名了" and updated.sandbox_enabled is True
        # 更新不应误改其它用户的数据
        assert (await store.get_session(session.id)).title == "改名了"

        await store.delete_session(session)
        assert await store.get_session(session.id) is None, "软删除后不可见"
        assert await store.list_sessions(alice.id) == []

        await dispose_engine()

    _run(scenario())


# ---------------------------------------------------------------- 消息
def test_message_seq_and_json_roundtrip(sql_env: None) -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        user = await store.create_user(username="alice", email="a@x.com", password_hash="h")
        session = await store.create_session(user_id=user.id)

        first = await store.add_message(session_id=session.id, role="user", content="你好")
        tool_calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "calculator", "arguments": '{"expression": "1+1"}'},
            }
        ]
        second = await store.add_message(
            session_id=session.id, role="assistant", content="", tool_calls=tool_calls
        )
        third = await store.add_message(
            session_id=session.id, role="tool", content="2", tool_call_id="call_1"
        )

        assert [first.seq, second.seq, third.seq] == [0, 1, 2], "会话内 seq 应从 0 连续递增"

        history = await store.list_messages(session.id)
        assert [m.role for m in history] == ["user", "assistant", "tool"]
        assert history[1].tool_calls == tool_calls, "JSON 列应原样往返"
        assert history[2].tool_call_id == "call_1"

        await dispose_engine()

    _run(scenario())


def test_delete_messages_from_truncates_tail(sql_env: None) -> None:
    """重新生成依赖的截断：只删断点及之后，且新消息从断点续号。"""

    async def scenario() -> None:
        store = await _fresh_store()
        user = await store.create_user(username="alice", email="a@x.com", password_hash="h")
        session = await store.create_session(user_id=user.id)
        other = await store.create_session(user_id=user.id)

        for role, content in [
            ("user", "问一"),
            ("assistant", "答一"),
            ("user", "问二"),
            ("assistant", "答二"),
        ]:
            await store.add_message(session_id=session.id, role=role, content=content)
        await store.add_message(session_id=other.id, role="user", content="别的会话")

        assert await store.delete_messages_from(session.id, 2) == 2
        assert [m.content for m in await store.list_messages(session.id)] == ["问一", "答一"]
        assert len(await store.list_messages(other.id)) == 1, "其他会话不受影响"

        # 断点之后的序号可复用，历史保持连续
        again = await store.add_message(session_id=session.id, role="user", content="问二（重跑）")
        assert again.seq == 2
        assert [m.seq for m in await store.list_messages(session.id)] == [0, 1, 2]

        # 越界与不存在的会话都安全返回 0
        assert await store.delete_messages_from(session.id, 99) == 0
        assert await store.delete_messages_from("not-exist", 0) == 0

        await dispose_engine()

    _run(scenario())


# ---------------------------------------------------------------- 重置令牌
def test_reset_token_single_use_and_expiry(sql_env: None) -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        user = await store.create_user(username="alice", email="a@x.com", password_hash="h")

        token = await store.create_reset_token(user.id, "hash-abc", ttl_minutes=30)
        assert token.used_at is None

        consumed = await store.consume_reset_token("hash-abc")
        assert consumed is not None and consumed.user_id == user.id
        assert consumed.used_at is not None
        assert await store.consume_reset_token("hash-abc") is None, "令牌只能使用一次"

        # 过期令牌不可用
        await store.create_reset_token(user.id, "hash-expired", ttl_minutes=-1)
        assert await store.consume_reset_token("hash-expired") is None

        # 未知令牌
        assert await store.consume_reset_token("hash-unknown") is None

        # 批量作废：重置密码后其余未用令牌立即失效
        await store.create_reset_token(user.id, "hash-live", ttl_minutes=30)
        await store.invalidate_reset_tokens(user.id)
        assert await store.consume_reset_token("hash-live") is None

        await dispose_engine()

    _run(scenario())


# ---------------------------------------------------------------- 对话附件
def test_attachment_lifecycle_and_purge(sql_env: None) -> None:
    """附件表：写入 / 批量取回保序 / 绑定到消息 / 消息内 JSON 往返 / 过期清理。"""

    async def scenario() -> None:
        store = await _fresh_store()
        user = await store.create_user(username="alice", email="a@x.com", password_hash="h")
        session = await store.create_session(user_id=user.id)

        text = "# 手册\n内容"
        sent = await store.add_attachment(
            user_id=user.id,
            filename="manual.md",
            file_type="md",
            content_type="text/markdown",
            size_bytes=len(text.encode("utf-8")),
            text=text,
        )
        draft = await store.add_attachment(
            user_id=user.id, filename="draft.txt", file_type="txt", text="草稿"
        )
        assert sent.char_count == len(text) and not sent.bound

        # 批量取回保持入参顺序，未知 id 直接跳过
        listed = await store.list_attachments([draft.id, "not-exist", sent.id])
        assert [item.id for item in listed] == [draft.id, sent.id]

        message = await store.add_message(
            session_id=session.id,
            role="user",
            content="看看附件",
            attachments=[sent.to_meta()],
        )
        assert message.attachments == [sent.to_meta()], "附件元数据应能原样往返"
        history = await store.list_messages(session.id)
        assert [item.attachments for item in history] == [[sent.to_meta()]]

        bound = await store.bind_attachments(
            [sent.id, "not-exist"], message_id=message.id, session_id=session.id
        )
        assert bound == 1, "不存在的附件应被忽略"
        stored = await store.get_attachment(sent.id)
        assert stored is not None and stored.bound and stored.session_id == session.id

        # 清理只回收「上传了但没随消息发出」且早于阈值的附件
        purged = await store.purge_stale_attachments(before=utcnow() + timedelta(minutes=1))
        assert purged == 1
        assert await store.get_attachment(draft.id) is None
        assert await store.get_attachment(sent.id) is not None

        await store.delete_attachment(stored)
        assert await store.get_attachment(sent.id) is None

        await dispose_engine()

    _run(scenario())


# ---------------------------------------------------------------- 知识库分块
def test_chunk_embeddings_write_filter_and_backfill(sql_env: None) -> None:
    """向量列以 JSON 文本入库，且 only_embedded 筛选与回填行为正确（M4）。"""

    async def scenario() -> None:
        store = await _fresh_store()
        alice = await store.create_user(username="alice", email="a@x.com", password_hash="h")
        bob = await store.create_user(username="bob", email="b@x.com", password_hash="h")
        document = await store.create_document(
            user_id=alice.id, filename="guide.md", file_type="md"
        )

        # 只给前两块向量，第三块留空（模拟嵌入后端不可用时的上传）
        written = await store.add_chunks(
            user_id=alice.id,
            document_id=document.id,
            contents=["第一块", "第二块", "第三块"],
            embeddings=[[0.5, 0.5], [1.0, 0.0], []],
        )
        assert written == 3

        chunks = await store.list_chunks(alice.id, document_id=document.id)
        assert [c.seq for c in chunks] == [0, 1, 2], "seq 应在文档内连续"
        assert chunks[0].embedding == [0.5, 0.5], "向量应能原样往返"
        assert chunks[2].embedding is None, "空向量按未向量化处理"

        assert await store.count_chunks(alice.id, source="knowledge") == 3
        assert await store.count_chunks(alice.id, source="knowledge", only_embedded=True) == 2
        embedded = await store.list_chunks(alice.id, source="knowledge", only_embedded=True)
        assert [c.seq for c in embedded] == [0, 1]
        assert await store.list_chunks(bob.id) == [], "分块必须按用户隔离"

        # 回填缺失向量：只更新属于该用户的块
        missing = chunks[2]
        updated = await store.update_chunk_embeddings(
            user_id=alice.id,
            items=[(missing.id, [0.0, 1.0]), ("not-exist", [1.0, 1.0])],
        )
        assert updated == 1, "不存在的块应被忽略"
        assert await store.count_chunks(alice.id, source="knowledge", only_embedded=True) == 3
        assert (await store.list_chunks(alice.id, document_id=document.id))[2].embedding == [0.0, 1.0]

        # 回填同样受用户隔离约束
        assert (
            await store.update_chunk_embeddings(user_id=bob.id, items=[(missing.id, [9.0, 9.0])])
            == 0
        )
        assert (await store.list_chunks(alice.id, document_id=document.id))[2].embedding == [0.0, 1.0]

        await store.delete_chunks(user_id=alice.id, document_id=document.id)
        assert await store.count_chunks(alice.id, source="knowledge") == 0

        await dispose_engine()

    _run(scenario())


def test_large_text_columns_are_longtext_on_mysql() -> None:
    """守住 MySQL 的 TEXT 字节上限：附件正文 / 消息正文 / 向量 JSON 都可能超过 65535 字节。

    回归：上传 1 MB 的 PDF 能抽出 4.9 万汉字（≈147 KB），插入时报
    `DataError (1406) Data too long for column 'text'`，用户侧只看到「上传失败」。
    """
    from sqlalchemy.dialects import mysql, sqlite

    from app.models import AttachmentRow, EmbeddingChunkRow, MessageRow

    for table, column in (
        (AttachmentRow.__table__, "text"),
        (MessageRow.__table__, "content"),
        (EmbeddingChunkRow.__table__, "embedding"),
    ):
        compiled = table.c[column].type.compile(dialect=mysql.dialect())
        assert compiled == "LONGTEXT", f"{table.name}.{column} 在 MySQL 上是 {compiled}，装不下长文本"
        # 其它方言（SQLite）退化为普通 TEXT，不引入 MySQL 专有类型
        assert table.c[column].type.compile(dialect=sqlite.dialect()) == "TEXT"
