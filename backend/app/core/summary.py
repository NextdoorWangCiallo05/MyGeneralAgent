"""上下文压缩（FR-6）：把较早的对话轮次摘要成一段文字，控制送入模型的历史长度。

对应《技术设计文档》4.3 节的上下文组装策略：

```
[System Prompt]
[历史对话摘要 (summary)]   ← 本模块产出
[最近 N 轮完整对话]
[本轮用户输入]
```

设计要点：

- **按「轮」压缩**：一轮 = 一条 `user` 消息及其后续 `assistant` / `tool` 消息。
  这样绝不会把 `assistant(tool_calls)` 与它的 `tool` 结果拆散——拆散会被模型判为非法消息序列；
- **增量摘要**：`sessions.summary_upto_seq` 记录摘要已覆盖到的 `messages.seq`，
  下一轮只摘要新增部分，避免把同一段历史反复喂给模型；
- **失败降级**：摘要调用或写入失败时退回旧摘要（必要时保留完整历史），绝不影响本轮对话。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.llm.client import ChunkKind, LLMClient, LLMRequestError
from app.store.base import AbstractStore
from app.store.entities import Message, Session

logger = logging.getLogger("app.summary")

#: 生成摘要时使用的系统提示
SUMMARY_SYSTEM_PROMPT = (
    "你是对话压缩器。请把给定的对话历史压缩成简洁的中文摘要，"
    "务必保留：用户的目标与偏好、已确认的事实与结论、关键数字与文件名、"
    "以及尚未完成的任务与待办。不要编造内容，不要输出寒暄、标题或解释。"
)

#: 单条消息在摘要输入中的截断长度（字符），避免压缩本身消耗过量 token
_MAX_MESSAGE_CHARS = 800
_MAX_TOOL_CHARS = 400


@dataclass(slots=True)
class ContextPlan:
    """本轮送入模型时的上下文计划。"""

    #: 需要注入 system 消息的历史摘要（可能为空）
    summary: str = ""
    #: `history` 中从此下标开始保留（之前的部分已被摘要覆盖）
    keep_from: int = 0


# ---------------------------------------------------------------- token 估算
def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：ASCII 约 4 字符 1 token，非 ASCII（中文）约 1 字符 1 token。

    只用于「是否触发压缩」的判断，宁可高估也不低估，因此不做精确分词。
    """
    if not text:
        return 0
    ascii_count = sum(1 for char in text if ord(char) < 128)
    return ascii_count // 4 + (len(text) - ascii_count)


def estimate_messages_tokens(messages: Sequence[Message]) -> int:
    """估算一批消息的 token 总量（含 tool_calls 的 JSON 体积）。"""
    total = 0
    for message in messages:
        total += estimate_tokens(message.content or "")
        if message.tool_calls:
            total += estimate_tokens(json.dumps(message.tool_calls, ensure_ascii=False))
    return total


# ---------------------------------------------------------------- 轮次切分
def split_turns(history: Sequence[Message]) -> list[list[Message]]:
    """按 `user` 消息把历史切成轮次，轮次内的 tool 消息跟随其所属轮次。"""
    turns: list[list[Message]] = []
    for message in history:
        if message.role == "user" or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return turns


# ---------------------------------------------------------------- 压缩决策
async def plan_context(
    *,
    session: Session,
    history: Sequence[Message],
    llm: LLMClient,
    store: AbstractStore,
    settings: Settings,
) -> ContextPlan:
    """决定本轮上下文的摘要与保留范围。

    返回的 `keep_from` 一定落在轮次边界上，因此不会产生「半截」的工具调用序列。
    """
    existing = session.summary or ""
    if estimate_messages_tokens(history) <= settings.summary_trigger_tokens:
        return ContextPlan(summary=existing, keep_from=0)

    turns = split_turns(history)
    keep_turns = max(1, settings.summary_keep_turns)
    if len(turns) <= keep_turns:
        return ContextPlan(summary=existing, keep_from=0)

    # 待压缩前缀：除最近 keep_turns 轮之外的完整轮次
    compressible = [message for turn in turns[:-keep_turns] for message in turn]
    boundary = len(compressible)
    summarized_end = _summarized_end(history, session.summary_upto_seq)

    pending = [message for message in compressible if message.seq > session.summary_upto_seq]
    if not pending:
        # 该前缀此前已摘要过，直接复用，节省上下文
        return ContextPlan(summary=existing, keep_from=boundary)

    fallback = min(boundary, summarized_end)
    if estimate_messages_tokens(pending) < settings.summary_min_new_tokens:
        # 新增内容太少，不值得多调一次模型；只丢掉已摘要过的部分
        return ContextPlan(summary=existing, keep_from=fallback)

    summary = await _summarize(llm, previous=existing, messages=pending, model=session.model or None)
    if not summary:
        return ContextPlan(summary=existing, keep_from=fallback)

    try:
        await store.update_session(
            session,
            summary=summary,
            summary_upto_seq=compressible[-1].seq,
        )
    except Exception:  # noqa: BLE001 - 摘要落库失败不应中断对话
        logger.warning("会话 %s 的摘要写入失败", session.id, exc_info=True)

    logger.info(
        "会话 %s 已压缩 %s 条历史消息（seq<=%s）", session.id, len(pending), compressible[-1].seq
    )
    return ContextPlan(summary=summary, keep_from=boundary)


def _summarized_end(history: Sequence[Message], summary_upto_seq: int) -> int:
    """已摘要消息在 `history` 中的结束下标（保守值，用于降级路径）。"""
    end = 0
    for index, message in enumerate(history):
        if message.seq <= summary_upto_seq:
            end = index + 1
    return end


# ---------------------------------------------------------------- 摘要生成
async def _summarize(
    llm: LLMClient,
    *,
    previous: str,
    messages: Sequence[Message],
    model: str | None,
) -> str:
    """调用模型生成（滚动合并后的）摘要，失败返回空串。"""
    transcript = _render(messages)
    if not transcript:
        return ""

    prompt = (
        f"【已有摘要】\n{previous.strip() or '（无）'}\n\n"
        f"【新增对话】\n{transcript}\n\n"
        "请输出合并后的新摘要（不超过 400 字），只输出摘要正文。"
    )
    parts: list[str] = []
    try:
        async for chunk in llm.chat_stream(
            [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            model=model,
            temperature=0.2,
        ):
            if chunk.kind == ChunkKind.CONTENT.value:
                parts.append(chunk.text)
    except LLMRequestError as exc:
        logger.warning("生成对话摘要失败: %s", exc)
        return ""
    return "".join(parts).strip()


def _render(messages: Sequence[Message]) -> str:
    """把历史消息渲染成便于模型压缩的纯文本。"""
    lines: list[str] = []
    for message in messages:
        if message.role == "tool":
            lines.append(f"[工具结果] {_clip(message.content, _MAX_TOOL_CHARS)}")
            continue
        text = _clip(message.content, _MAX_MESSAGE_CHARS)
        if message.role == "user":
            lines.append(f"用户：{text}")
        elif message.role == "assistant":
            if message.tool_calls:
                names = ", ".join(
                    ((call.get("function") or {}).get("name") or "tool")
                    for call in message.tool_calls
                )
                lines.append(f"助手：（调用工具 {names}）{text}".rstrip())
            else:
                lines.append(f"助手：{text}")
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


__all__ = [
    "ContextPlan",
    "estimate_messages_tokens",
    "estimate_tokens",
    "plan_context",
    "split_turns",
]
