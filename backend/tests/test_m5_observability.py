"""M5：限流 / 可观测性 / 工具审计测试。

覆盖：
- 限流：滑动窗口计数、按 scope 分级配额、超限 429 带 Retry-After、关闭开关即放行、fail-open；
- 可观测性：Tracer 埋点扇出、未配置时降级为 no-op、一次对话产出 llm/tool 两类 span；
- 审计：写库内容（工具名 / 状态 / 耗时 / 是否确认）、参数脱敏与截断、管理后台查询与权限。
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
from app.core.audit import dump_args, sanitize, record_tool_call
from app.core.ratelimit import RateLimiter, reset_rate_limiter
from app.core.runtime_config import get_runtime_config, reset_runtime_config
from app.core.tracing import Span, Tracer, Turn, reset_tracer
from app.llm.client import ChatChunk, ChunkKind
from app.main import app
from app.store import MemoryStore, get_store, reset_store

PASSWORD = "secret123"
TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "calculator", "arguments": '{"expression": "6*7"}'},
}


class ScriptedLLM:
    """按脚本逐轮流式返回的 LLM 替身。"""

    scripts: list[list[ChatChunk]] = []

    def __init__(self, *_: Any, **__: Any) -> None:
        self._turn = -1

    async def chat_stream(
        self, *_: Any, **__: Any
    ) -> AsyncIterator[ChatChunk]:
        self._turn += 1
        script = ScriptedLLM.scripts[min(self._turn, len(ScriptedLLM.scripts) - 1)]
        for chunk in script:
            yield chunk


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_store()
    reset_rate_limiter()
    reset_tracer()
    reset_runtime_config()
    ScriptedLLM.scripts = []
    monkeypatch.setattr(get_settings(), "llm_api_key", "test-key")
    yield
    reset_store()
    reset_rate_limiter()
    reset_tracer()
    reset_runtime_config()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"username": "auditor", "email": "auditor@example.com", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# ================================================================ 限流
def test_sliding_window_counts_and_blocks() -> None:
    settings = get_settings().model_copy(update={"rate_limit_ip_per_minute": 3})

    async def scenario() -> None:
        limiter = RateLimiter(settings)
        # 窗口内前 3 次放行，第 4 次起被拦截
        decisions = [await limiter.check("ip", "1.2.3.4") for _ in range(4)]
        assert all(d is not None for d in decisions)
        assert [d.allowed for d in decisions] == [True, True, True, False]  # type: ignore[union-attr]
        blocked = decisions[-1]
        assert blocked is not None and blocked.retry_after >= 1
        assert blocked.headers()["Retry-After"] == str(blocked.retry_after)

        # 不同 key 互不影响
        other = await limiter.check("ip", "9.9.9.9")
        assert other is not None and other.allowed is True

    asyncio.run(scenario())


def test_disabled_limiter_always_allows() -> None:
    settings = get_settings().model_copy(update={"rate_limit_enabled": False})

    async def scenario() -> None:
        limiter = RateLimiter(settings)
        for _ in range(20):
            assert await limiter.check("ip", "1.2.3.4") is None

    asyncio.run(scenario())


def test_enforce_raises_with_retry_after_header() -> None:
    from app.core.errors import RateLimitedError

    settings = get_settings().model_copy(update={"rate_limit_auth_per_minute": 2})

    async def scenario() -> None:
        limiter = RateLimiter(settings)
        for _ in range(2):
            await limiter.enforce("auth", "user-1")
        with pytest.raises(RateLimitedError) as excinfo:
            await limiter.enforce("auth", "user-1")
        assert excinfo.value.headers.get("Retry-After")

    asyncio.run(scenario())


def test_ip_rate_limit_returns_429(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_ip_per_minute", 2)
    reset_rate_limiter()

    # health 免限流
    for _ in range(5):
        assert client.get("/api/v1/health").status_code == 200

    # 前 2 次未登录得 401（已被计数），第 3 次被限流拦截
    assert client.get("/api/v1/sessions").status_code == 401
    assert client.get("/api/v1/sessions").status_code == 401
    blocked = client.get("/api/v1/sessions")
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "RATE_LIMITED"
    assert blocked.headers["Retry-After"]


def test_auth_scope_has_stricter_quota(client: TestClient) -> None:
    # 默认 auth 配额 20，远低于 IP 兜底 600：连续 21 次登录必被拦
    statuses = [
        client.post(
            "/api/v1/auth/login", json={"username": "nobody", "password": "wrong123"}
        ).status_code
        for _ in range(21)
    ]
    assert statuses[0] == 401
    assert 429 in statuses
    assert statuses[-1] == 429


def test_rate_limit_headers_present_on_success(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get("/api/v1/sessions", headers=auth_headers)
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"]
    assert response.headers["X-RateLimit-Remaining"] is not None


# ================================================================ 可观测性
class RecordingSink:
    """记录所有回调，便于断言埋点扇出。"""

    name = "recording"

    def __init__(self) -> None:
        self.started: list[Turn] = []
        self.spans: list[Span] = []
        self.ended: list[Turn] = []

    def turn_started(self, turn: Turn) -> None:
        self.started.append(turn)

    def span_ended(self, turn: Turn, span: Span) -> None:
        self.spans.append(span)

    def turn_ended(self, turn: Turn) -> None:
        self.ended.append(turn)

    def flush(self) -> None:  # pragma: no cover - 无副作用
        return None


def test_tracer_fans_out_to_sinks() -> None:
    sink = RecordingSink()
    tracer = Tracer([sink])
    assert tracer.enabled is True

    turn = tracer.start_turn(name="chat.turn", user_id="u1", session_id="s1", input="你好")
    with turn.span("llm.chat", model="deepseek-flash") as span:
        span.update(output="hi", prompt_tokens=3, completion_tokens=1)
    turn.end(status="ok")

    assert len(sink.started) == 1
    assert [s.name for s in sink.spans] == ["llm.chat"]
    assert sink.spans[0].duration_ms >= 0
    assert sink.ended[0].status == "ok"


def test_tracer_without_sink_is_noop() -> None:
    tracer = Tracer([])
    assert tracer.enabled is False
    # 无 sink 时所有操作都不应抛异常
    turn = tracer.start_turn(user_id="u1", session_id="s1")
    turn.span("tool.call", tool="calculator").end(status="success")
    turn.end()


def test_agent_emits_llm_and_tool_spans(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ScriptedLLM.scripts = [
        [
            ChatChunk(kind=ChunkKind.TOOL_CALLS.value, tool_calls=[TOOL_CALL]),
            ChatChunk(kind=ChunkKind.DONE.value),
        ],
        [
            ChatChunk(kind=ChunkKind.CONTENT.value, text="结果是 42。"),
            ChatChunk(kind=ChunkKind.USAGE.value, usage={"prompt_tokens": 5, "completion_tokens": 2}),
            ChatChunk(kind=ChunkKind.DONE.value),
        ],
    ]
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: ScriptedLLM())

    sink = RecordingSink()
    reset_tracer(Tracer([sink]))

    response = client.post("/api/v1/chat", json={"message": "6*7"}, headers=auth_headers)
    assert response.status_code == 200

    names = [span.name for span in sink.spans]
    assert names.count("llm.chat") == 2
    assert names.count("tool.call") == 1
    tool_span = next(s for s in sink.spans if s.name == "tool.call")
    assert tool_span.metadata["tool"] == "calculator"
    assert tool_span.status == "success"
    assert sink.ended and sink.ended[0].status == "ok"


# ================================================================ 审计
def test_sanitize_masks_secrets_and_truncates() -> None:
    payload = {
        "query": "hello",
        "api_key": "sk-live-abcdef",
        "nested": {"password": "p@ss", "token": "t"},
        "long": "x" * 800,
    }
    cleaned = sanitize(payload)
    assert cleaned["api_key"] == "***"
    assert cleaned["nested"]["password"] == "***"
    assert cleaned["nested"]["token"] == "***"
    assert cleaned["query"] == "hello"
    assert cleaned["long"].endswith("…")
    assert len(cleaned["long"]) <= 501


def test_dump_args_is_json_and_bounded() -> None:
    text = dump_args({"expression": "1+1"})
    assert json.loads(text) == {"expression": "1+1"}
    assert len(dump_args({"big": "y" * 5000})) <= 2000


def test_record_tool_call_persists_masked_entry() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        entry = await record_tool_call(
            store,
            user_id="u1",
            session_id="s1",
            tool_name="fetch_url",
            args={"url": "https://x.com", "api_key": "sk-secret"},
            status="error",
            summary="抓取失败",
            error="连接超时",
            duration_ms=1234,
            approved=True,
        )
        assert entry is not None

        logs = await store.list_audit_logs("u1")
        assert len(logs) == 1
        record = logs[0]
        assert record.tool_name == "fetch_url"
        assert record.status == "error"
        assert record.duration_ms == 1234
        assert record.approved is True
        assert "sk-secret" not in record.args
        assert "***" in record.args
        assert "连接超时" in record.result
        assert await store.count_audit_logs("u1") == 1
        assert await store.count_audit_logs("someone-else") == 0

    asyncio.run(scenario())


def test_audit_written_after_tool_call(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ScriptedLLM.scripts = [
        [
            ChatChunk(kind=ChunkKind.TOOL_CALLS.value, tool_calls=[TOOL_CALL]),
            ChatChunk(kind=ChunkKind.DONE.value),
        ],
        [ChatChunk(kind=ChunkKind.CONTENT.value, text="42"), ChatChunk(kind=ChunkKind.DONE.value)],
    ]
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: ScriptedLLM())

    assert client.post("/api/v1/chat", json={"message": "6*7"}, headers=auth_headers).status_code == 200

    async def scenario() -> list[Any]:
        return await get_store().list_audit_logs(None)

    logs = asyncio.run(scenario())
    assert len(logs) == 1
    assert logs[0].tool_name == "calculator"
    assert logs[0].status == "success"
    assert "6*7" in logs[0].args
    assert logs[0].approved is False


def test_audit_records_rejected_tool(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """需人工确认的工具被拒绝时同样留痕（status=rejected）。"""
    write_call = {
        "id": "call_w",
        "type": "function",
        "function": {"name": "write_file", "arguments": '{"path": "a.txt", "content": "hi"}'},
    }
    ScriptedLLM.scripts = [
        [
            ChatChunk(kind=ChunkKind.TOOL_CALLS.value, tool_calls=[write_call]),
            ChatChunk(kind=ChunkKind.DONE.value),
        ],
        [ChatChunk(kind=ChunkKind.CONTENT.value, text="好的"), ChatChunk(kind=ChunkKind.DONE.value)],
    ]
    monkeypatch.setattr(chat_api, "build_llm_client", lambda *_: ScriptedLLM())

    # 全局开启沙箱 + 会话开启沙箱 + 将确认一律判为拒绝
    async def enable_sandbox() -> None:
        await get_runtime_config().update(
            {"sandbox_global_enabled": True}, updated_by="test"
        )

    async def reject(*_: Any, **__: Any) -> bool:
        return False

    asyncio.run(enable_sandbox())
    monkeypatch.setattr(get_settings(), "tool_confirm_timeout", 0.01)
    monkeypatch.setattr("app.core.confirmations.ConfirmationRegistry.wait_future", reject)

    client.post(
        "/api/v1/chat",
        json={"message": "写文件", "sandbox_enabled": True},
        headers=auth_headers,
    )

    async def scenario() -> list[Any]:
        return await get_store().list_audit_logs(None)

    logs = asyncio.run(scenario())
    assert logs and logs[0].tool_name == "write_file"
    assert logs[0].status == "rejected"


# ================================================================ 管理后台查询
ADMIN_EMAIL = "root@example.com"


@pytest.fixture
def admin_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("ADMIN_USERNAME", "root")
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "llm_api_key", "test-key")
    reset_rate_limiter()
    reset_runtime_config()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()
    reset_rate_limiter()


@pytest.fixture
def admin_headers(admin_client: TestClient) -> dict[str, str]:
    response = admin_client.post(
        "/api/v1/auth/login", json={"username": ADMIN_EMAIL, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_admin_audit_logs_requires_admin(
    admin_client: TestClient, auth_headers: dict[str, str]
) -> None:
    assert admin_client.get("/api/v1/admin/audit-logs").status_code == 401
    assert (
        admin_client.get("/api/v1/admin/audit-logs", headers=auth_headers).status_code == 403
    )


def test_admin_can_query_audit_logs(
    admin_client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def seed() -> None:
        await record_tool_call(
            get_store(),
            user_id="u1",
            session_id="s1",
            tool_name="calculator",
            args={"expression": "2+2"},
            status="success",
            summary="4",
            duration_ms=5,
        )

    asyncio.run(seed())

    response = admin_client.get("/api/v1/admin/audit-logs", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["tool_name"] == "calculator"

    filtered = admin_client.get(
        "/api/v1/admin/audit-logs", params={"tool_name": "missing"}, headers=admin_headers
    )
    assert filtered.json()["total"] == 0
