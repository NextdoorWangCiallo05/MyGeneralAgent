"""沙箱文件工具：读写用户沙箱目录内的 UTF-8 文本文件。

所有路径都经 `sandbox.resolve_path` 规范化与越界校验，
工具的沙箱归属来自 `ToolContext`（由 Agent 注入），模型无法指定或伪造。
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.tools.base import Tool, ToolResult
from app.tools.context import require_tool_context
from app.tools.sandbox import SandboxError, resolve_path

logger = logging.getLogger("app.tools.files")


async def _read_file(path: str) -> ToolResult:
    settings = get_settings()
    try:
        context = require_tool_context()
        target = resolve_path(context.user_id, path)
    except (RuntimeError, SandboxError) as exc:
        return ToolResult(status="error", summary=f"路径不可用：{exc}", content=f"错误：{exc}")

    if not target.exists() or not target.is_file():
        return ToolResult(
            status="error",
            summary="文件不存在",
            content=f"错误：沙箱内不存在文件 `{path}`。可先用其它方式确认路径，或直接创建。",
        )

    size = target.stat().st_size
    if size > settings.sandbox_max_file_bytes:
        return ToolResult(
            status="error",
            summary=f"文件过大（{size} 字节）",
            content=f"错误：文件大小 {size} 字节，超过上限 {settings.sandbox_max_file_bytes} 字节。",
        )

    try:
        text = target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return ToolResult(
            status="error", summary="读取失败", content=f"错误：仅支持读取 UTF-8 文本文件（{exc}）"
        )

    return ToolResult(
        status="success",
        summary=f"已读取 {path}（{len(text)} 字符）",
        content=text or "（空文件）",
        data={"path": path, "size": size},
    )


async def _write_file(path: str, content: str = "") -> ToolResult:
    settings = get_settings()
    try:
        context = require_tool_context()
        target = resolve_path(context.user_id, path)
    except (RuntimeError, SandboxError) as exc:
        return ToolResult(status="error", summary=f"路径不可用：{exc}", content=f"错误：{exc}")

    payload = content if content is not None else ""
    size = len(payload.encode("utf-8"))
    if size > settings.sandbox_max_file_bytes:
        return ToolResult(
            status="error",
            summary=f"内容过大（{size} 字节）",
            content=f"错误：写入内容 {size} 字节，超过上限 {settings.sandbox_max_file_bytes} 字节。",
        )
    if target.is_dir():
        return ToolResult(
            status="error", summary="目标路径是目录", content=f"错误：`{path}` 是目录，无法写入。"
        )

    existed = target.exists()
    try:
        # resolve_path 已保证 target 位于沙箱内，其父目录同样不会越界
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
    except OSError as exc:
        return ToolResult(status="error", summary="写入失败", content=f"错误：写入文件失败（{exc}）")

    action = "覆盖" if existed else "创建"
    return ToolResult(
        status="success",
        summary=f"已{action} {path}（{size} 字节）",
        content=f"已{action}沙箱文件 `{path}`，共 {size} 字节。",
        data={"path": path, "size": size, "overwritten": existed},
    )


read_file_tool = Tool(
    name="read_file",
    description=(
        "读取沙箱目录内的 UTF-8 文本文件。path 为沙箱内相对路径，例如 'notes/todo.md'。"
        "仅支持文本文件，超出大小上限会报错。需要会话已开启沙箱。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "沙箱内的相对路径，如 'notes/todo.md'"},
        },
        "required": ["path"],
    },
    handler=_read_file,
    readonly=True,
    sandbox_required=True,
)


write_file_tool = Tool(
    name="write_file",
    description=(
        "把文本写入沙箱目录内的文件（同名文件会被覆盖）。path 为沙箱内相对路径。"
        "该操作会改动磁盘内容，必须经用户确认后才会执行。需要会话已开启沙箱。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "沙箱内的相对路径，如 'notes/todo.md'"},
            "content": {"type": "string", "description": "要写入的完整文本内容"},
        },
        "required": ["path", "content"],
    },
    handler=_write_file,
    requires_confirmation=True,
    readonly=False,
    sandbox_required=True,
)


__all__ = ["read_file_tool", "write_file_tool"]
