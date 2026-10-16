"""M3 工具与沙箱测试。

覆盖四类风险：
1. **路径逃逸**——`../`、绝对路径、跨用户访问一律拒绝；
2. **受限子进程**——环境变量不泄漏、超时可强制终止、仅标准库可用；
3. **工具裁剪**——沙箱关闭时不下发文件 / 代码类工具；
4. **确认门控**——副作用工具未确认时只返回 needs_confirmation，不产生副作用。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterator
from typing import Any

import pytest

from app.config import get_settings
from app.tools.context import ToolContext, reset_tool_context, set_tool_context
from app.tools.fetch_url import assert_public_url, html_to_text
from app.tools.files import read_file_tool, write_file_tool
from app.tools.python_exec import python_exec_tool
from app.tools.registry import build_default_registry
from app.tools.sandbox import SandboxError, resolve_path, user_root
from app.search.backends.duckduckgo import parse_results

USER = "sandbox-user"


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    """在同步用例中驱动协程。"""
    return asyncio.run(coro)


@pytest.fixture
def sandboxed() -> Iterator[None]:
    """绑定一个开启了沙箱的工具上下文。"""
    token = set_tool_context(ToolContext(user_id=USER, session_id="s1", sandbox_enabled=True))
    get_settings().sandbox_global_enabled = True
    try:
        yield
    finally:
        reset_tool_context(token)
        get_settings().sandbox_global_enabled = False


# ---------------------------------------------------------------- 路径隔离
@pytest.mark.parametrize(
    "path",
    ["", "   ", "../secret.txt", "notes/../../secret.txt", "/etc/passwd", "C:\\Windows\\win.ini"],
)
def test_resolve_path_blocks_escape(path: str) -> None:
    with pytest.raises(SandboxError):
        resolve_path(USER, path)


def test_resolve_path_allows_nested_relative() -> None:
    resolved = resolve_path(USER, "notes/todo.md")
    assert resolved.name == "todo.md"
    assert resolved.parent.name == "notes"
    assert user_root(USER) in resolved.parents


def test_user_sandboxes_are_isolated() -> None:
    alice = resolve_path("alice", "shared.txt")
    bob = resolve_path("bob", "shared.txt")
    assert alice != bob
    with pytest.raises(SandboxError):
        resolve_path("alice", "../bob/shared.txt")


# ---------------------------------------------------------------- 文件工具
def test_write_then_read_roundtrip(sandboxed: None) -> None:
    write = run(write_file_tool.handler(path="notes/demo.txt", content="你好，沙箱"))
    assert write.status == "success"
    assert write.data["overwritten"] is False

    read = run(read_file_tool.handler(path="notes/demo.txt"))
    assert read.status == "success"
    assert read.content == "你好，沙箱"

    again = run(write_file_tool.handler(path="notes/demo.txt", content="覆盖"))
    assert again.status == "success"
    assert again.data["overwritten"] is True


def test_read_missing_file_reports_error(sandboxed: None) -> None:
    result = run(read_file_tool.handler(path="nope.txt"))
    assert result.status == "error"
    assert "不存在" in result.summary


def test_read_file_over_limit_is_rejected(sandboxed: None) -> None:
    run(write_file_tool.handler(path="big.txt", content="x" * 100))
    limit = get_settings().sandbox_max_file_bytes
    get_settings().sandbox_max_file_bytes = 8
    try:
        result = run(read_file_tool.handler(path="big.txt"))
    finally:
        get_settings().sandbox_max_file_bytes = limit
    assert result.status == "error"
    assert "文件过大" in result.summary


def test_file_tools_reject_escape_without_touching_disk(sandboxed: None) -> None:
    result = run(write_file_tool.handler(path="../escaped.txt", content="bad"))
    assert result.status == "error"
    assert not (user_root(USER).parent / "escaped.txt").exists()


# ---------------------------------------------------------------- 受限子进程
def test_python_exec_captures_stdout(sandboxed: None) -> None:
    result = run(python_exec_tool.handler(code="print(6 * 7)"))
    assert result.status == "success"
    assert result.data["exit_code"] == 0
    assert "42" in result.content


def test_python_exec_does_not_leak_host_environment(sandboxed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "must-not-leak")
    result = run(python_exec_tool.handler(code="import os; print(os.environ.get('JWT_SECRET'))"))
    assert result.status == "success"
    assert "None" in result.content
    assert "must-not-leak" not in result.content


def test_python_exec_reports_runtime_error(sandboxed: None) -> None:
    result = run(python_exec_tool.handler(code="raise ValueError('boom')"))
    assert result.status == "error"
    assert "boom" in result.content


def test_python_exec_timeout_terminates_process(sandboxed: None) -> None:
    original = get_settings().sandbox_exec_timeout
    get_settings().sandbox_exec_timeout = 1.0
    try:
        result = run(python_exec_tool.handler(code="while True: pass"))
    finally:
        get_settings().sandbox_exec_timeout = original
    assert result.status == "timeout"
    assert "超时" in result.summary


# ---------------------------------------------------------------- 工具裁剪
def test_sandbox_tools_hidden_until_enabled() -> None:
    registry = build_default_registry()
    without = {item["function"]["name"] for item in registry.schemas_for(sandbox_enabled=False)}
    with_sandbox = {item["function"]["name"] for item in registry.schemas_for(sandbox_enabled=True)}

    assert {"calculator", "web_search", "fetch_url"} <= without
    assert {"read_file", "write_file", "python_exec"}.isdisjoint(without)
    assert {"read_file", "write_file", "python_exec"} <= with_sandbox


def test_confirmation_tools_are_gated() -> None:
    registry = build_default_registry()
    pending = run(registry.execute("write_file", {"path": "a.txt", "content": "x"}))
    assert pending.status == "needs_confirmation"

    # 即使调用方声称已确认，缺少工具上下文（无法确定沙箱归属）也必须失败
    approved = run(registry.execute("write_file", {"path": "a.txt", "content": "x"}, approved=True))
    assert approved.status == "error"


# ---------------------------------------------------------------- 联网工具防护
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/admin",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
    ],
)
def test_fetch_url_blocks_non_public_targets(url: str) -> None:
    with pytest.raises(ValueError):
        run(assert_public_url(url))


def test_html_to_text_strips_markup() -> None:
    text = html_to_text(
        "<html><head><style>a{color:red}</style></head>"
        "<body><h1>标题</h1><p>正文 &amp; 更多</p><script>x()</script></body></html>"
    )
    assert "标题" in text
    assert "正文 & 更多" in text
    assert "<" not in text
    assert "color:red" not in text


def test_web_search_parses_redirect_links() -> None:
    markup = """
    <a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=1"
       class="result-link">示例 A</a>
    <td class="result-snippet">摘要 A</td>
    <a class="result-link" href="https://example.org/b">示例 B</a>
    <td class="result-snippet">摘要 B</td>
    """
    results = parse_results(markup, limit=5)
    assert [item.title for item in results] == ["示例 A", "示例 B"]
    assert results[0].url == "https://example.com/a"
    assert results[0].snippet == "摘要 A"
    assert results[1].url == "https://example.org/b"


def test_web_search_respects_limit() -> None:
    markup = "".join(
        f'<a class="result-link" href="https://example.com/{i}">标题{i}</a>' for i in range(10)
    )
    assert len(parse_results(markup, limit=3)) == 3
