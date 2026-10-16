"""会话附件读取工具：让模型在整个对话里都能取回上传文件的正文。

附件是**会话级**资源：上传一次后，`Agent` 会在每一轮的「本会话文件」区里列出它们。
但受 `ATTACHMENT_CONTEXT_MAX_CHARS` 总预算限制，放不进上下文的文件只会出现在清单里
（标注「未整体展开」），模型就用本工具分段（offset/limit）或按关键词（keyword）把内容取回来。

为什么不让模型每次都用工具读：小文件直接展开，模型能立刻看到正文、不必多一轮工具调用；
只有大到放不下时才降级为按需读取，既保住"整个对话可用"，又不让每轮 prompt 爆掉。
"""

from __future__ import annotations

import re
from typing import Any

from app.store import get_store
from app.store.entities import Attachment
from app.tools.base import Tool, ToolResult
from app.tools.context import require_tool_context

__all__ = ["attachment_read_tool"]

#: 单次返回的正文上限：够读一大段，又不至于一次灌满上下文
MAX_CHARS = 20_000
#: 未显式指定 limit 时的默认长度
DEFAULT_CHARS = 4_000
#: 关键词命中时，命中位置前后各取多少字符作为上下文
HIT_WINDOW = 120
#: 单次最多列出几处命中
MAX_HITS = 5
#: 命中总数统计上限（中文单字关键词可能命中上千处，不必数完）
MAX_HIT_SCAN = 1_000


def _coerce_int(value: Any, default: int) -> int:
    """宽松解析模型给的 offset/limit：字符串、浮点、None 都能兜住。"""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _keyword_result(attachment: Attachment, keyword: str) -> ToolResult:
    """按关键词返回命中片段（只给命中位置附近的窗口，避免整篇灌进上下文）。"""
    text = attachment.text or ""
    total = len(text)
    positions: list[int] = []
    hits = 0
    for match in re.finditer(re.escape(keyword), text, re.IGNORECASE):
        hits += 1
        if len(positions) < MAX_HITS:
            positions.append(match.start())
        if hits >= MAX_HIT_SCAN:
            break

    if not positions:
        return ToolResult(
            status="success",
            summary=f"{attachment.filename} 未命中「{keyword}」",
            content=(
                f"附件「{attachment.filename}」（共 {total} 字）里没有出现「{keyword}」。"
                "可以换一个关键词，或用 offset 从头分段通读。"
            ),
            data={
                "attachment_id": attachment.id,
                "filename": attachment.filename,
                "keyword": keyword,
                "hits": 0,
                "total_chars": total,
            },
        )

    lines = [f"【附件】{attachment.filename}（共 {total} 字；关键词「{keyword}」命中 {hits} 处）"]
    for index, start in enumerate(positions, start=1):
        window_start = max(0, start - HIT_WINDOW)
        window_end = min(total, start + len(keyword) + HIT_WINDOW)
        lines.append(f"<命中 {index}>（自第 {window_start + 1} 字起）")
        lines.append(text[window_start:window_end].strip())
    if hits > len(positions):
        lines.append(f"（只列出前 {len(positions)} 处命中，实际命中 {hits} 处，可换更具体的关键词缩小范围）")
    lines.append(f"（需要命中处的完整上下文时，用 offset 读取，例如 offset={max(0, positions[0] - HIT_WINDOW)}）")

    return ToolResult(
        status="success",
        summary=f"{attachment.filename} 命中「{keyword}」{hits} 处",
        content="\n".join(lines),
        data={
            "attachment_id": attachment.id,
            "filename": attachment.filename,
            "keyword": keyword,
            "hits": hits,
            "offsets": positions,
            "total_chars": total,
        },
    )


async def _read_attachment(
    attachment_id: str = "",
    offset: Any = 0,
    limit: Any = DEFAULT_CHARS,
    keyword: str = "",
) -> ToolResult:
    """读附件正文：给了 keyword 就返回命中片段，否则按 offset/limit 返回一段。"""
    context = require_tool_context()
    target = (attachment_id or "").strip()
    if not target:
        return ToolResult(
            status="error",
            summary="缺少 attachment_id",
            content="错误：`attachment_id` 不能为空，请使用「本会话文件」清单里给出的 id。",
        )

    attachment = await get_store().get_attachment(target)
    if attachment is None or attachment.user_id != context.user_id:
        return ToolResult(
            status="error",
            summary="附件不存在",
            content="错误：该 attachment_id 不存在或不属于当前用户，请只用「本会话文件」清单里给出的 id。",
        )
    # 未绑定会话的附件（刚上传、还没随消息发出）允许读；已绑定到**其他会话**的一律拒绝：
    # 否则模型只要猜到一个 id 就能跨会话翻文件，等于绕过用户隔离
    if attachment.session_id and attachment.session_id != context.session_id:
        return ToolResult(
            status="error",
            summary="附件不属于当前会话",
            content="错误：该附件上传于其他会话，当前会话无法读取。若用户确实需要，请让他在本会话里重新上传。",
        )

    filename = attachment.filename
    text = attachment.text or ""
    total = len(text)
    if not total:
        return ToolResult(
            status="success",
            summary=f"{filename} 没有可读正文",
            content=(
                f"附件「{filename}」没有可读正文（可能是扫描版 PDF 或空文件），"
                "请如实告知用户，不要编造内容。"
            ),
            data={"attachment_id": attachment.id, "filename": filename, "total_chars": 0},
        )

    wanted = (keyword or "").strip()
    if wanted:
        return _keyword_result(attachment, wanted)

    start = max(0, min(_coerce_int(offset, 0), total))
    size = max(1, min(_coerce_int(limit, DEFAULT_CHARS), MAX_CHARS))
    end = min(total, start + size)
    lines = [
        f"【附件】{filename}（共 {total} 字；本次返回第 {start + 1}~{end} 字）",
        "<附件内容>",
        text[start:end],
        "</附件内容>",
    ]
    if end < total:
        lines.append(f"（后面还有 {total - end} 字，可继续用 offset={end} 读取，或用 keyword 定位）")
    elif start > 0:
        lines.append("（已读到文件末尾）")

    return ToolResult(
        status="success",
        summary=f"读取 {filename} 第 {start + 1}~{end} 字",
        content="\n".join(lines),
        data={
            "attachment_id": attachment.id,
            "filename": filename,
            "offset": start,
            "limit": size,
            "total_chars": total,
        },
    )


attachment_read_tool = Tool(
    name="read_attachment",
    description=(
        "读取本会话已上传附件的正文内容。用户上传的文件在整个对话里都可用："
        "长文件可用 offset/limit 分段读取，想定位某段内容可直接给 keyword。"
        "当「本会话文件」区里某个文件标注了「未整体展开」时，必须先调用本工具读到内容再回答，"
        "不要凭文件名或常识猜测文件内容；也不要谎称文件不可读。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "attachment_id": {
                "type": "string",
                "description": "「本会话文件」区里给出的附件 id（形如 9f2c…）",
            },
            "keyword": {
                "type": "string",
                "description": "可选：要定位的关键词，给出后返回命中片段（含前后文），适合长文件查找",
            },
            "offset": {
                "type": "integer",
                "description": "可选：起始字符位置，从 0 开始，默认 0",
            },
            "limit": {
                "type": "integer",
                "description": f"可选：本次读取的字符数，默认 {DEFAULT_CHARS}，最大 {MAX_CHARS}",
            },
        },
        "required": ["attachment_id"],
    },
    handler=_read_attachment,
    readonly=True,
    attachment_required=True,
    timeout=15.0,
)
