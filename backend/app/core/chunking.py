"""文本分块（M4）：把解析后的文档正文切成适合检索的片段。

策略（对应《技术设计文档》4.4 节 RAG 管线）：

- 先按**空行分段**，段落过长时再按**中英文句末标点**切句；
- 片段目标长度 `knowledge_chunk_size`（默认 800 字符），
  相邻片段保留 `knowledge_chunk_overlap` 字符重叠，避免答案正好被切在边界上；
- 输出保持原文顺序，与 `embeddings.seq` 一一对应，便于前端按序号回溯。
"""

from __future__ import annotations

import re

DEFAULT_CHUNK_SIZE = 800
DEFAULT_OVERLAP = 100

#: 低于该长度的尾块会被并入前一块，避免产生无检索价值的碎片
_MIN_CHUNK_CHARS = 40

_SENTENCE_BOUNDARY = "。！？!?；;"
_PARA_SPLIT = re.compile(r"\n{2,}")


def normalize_text(text: str) -> str:
    """统一换行、去掉 NUL、清理行尾空白并压缩连续空行。"""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = [line.rstrip() for line in cleaned.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def split_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    """把正文切成带重叠的片段列表（空文本返回空列表）。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须为正整数")
    overlap = max(0, min(overlap, chunk_size // 2))

    normalized = normalize_text(text)
    if not normalized:
        return []

    chunks: list[str] = []
    buffer = ""
    for paragraph in _paragraphs(normalized):
        for piece in _split_paragraph(paragraph, chunk_size):
            if not buffer:
                buffer = piece
            elif len(buffer) + 1 + len(piece) <= chunk_size:
                buffer = f"{buffer}\n{piece}"
            else:
                chunks.append(buffer)
                buffer = _carry_over(buffer, piece, overlap, chunk_size)
    if buffer:
        chunks.append(buffer)
    return _merge_tail(chunks)


def _paragraphs(text: str) -> list[str]:
    return [block.strip() for block in _PARA_SPLIT.split(text) if block.strip()]


def _split_paragraph(paragraph: str, chunk_size: int) -> list[str]:
    """超长段落按句子边界再切；单句仍超长则硬切。"""
    if len(paragraph) <= chunk_size:
        return [paragraph]

    pieces: list[str] = []
    buffer = ""
    for sentence in _sentences(paragraph):
        if len(sentence) > chunk_size:
            if buffer:
                pieces.append(buffer)
                buffer = ""
            pieces.extend(_hard_split(sentence, chunk_size))
        elif not buffer:
            buffer = sentence
        elif len(buffer) + len(sentence) <= chunk_size:
            buffer += sentence
        else:
            pieces.append(buffer)
            buffer = sentence
    if buffer:
        pieces.append(buffer)
    return pieces


def _sentences(paragraph: str) -> list[str]:
    """按句末标点切句（保留标点）。"""
    sentences: list[str] = []
    buffer = ""
    for char in paragraph:
        buffer += char
        if char in _SENTENCE_BOUNDARY:
            sentences.append(buffer)
            buffer = ""
    if buffer:
        sentences.append(buffer)
    return sentences


def _hard_split(text: str, chunk_size: int) -> list[str]:
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


def _carry_over(previous: str, piece: str, overlap: int, chunk_size: int) -> str:
    """把上一片段尾部若干字符作为新片段的前缀（重叠窗口），且不使片段超长。"""
    if overlap <= 0:
        return piece
    room = chunk_size - len(piece) - 1  # 预留一个换行符
    if room <= 0:
        return piece
    return f"{previous[-min(overlap, room):]}\n{piece}"


def _merge_tail(chunks: list[str]) -> list[str]:
    """把过短的尾块并入前一块。"""
    if len(chunks) >= 2 and len(chunks[-1]) < _MIN_CHUNK_CHARS:
        tail = chunks.pop()
        chunks[-1] = f"{chunks[-1]}\n{tail}"
    return chunks


__all__ = ["DEFAULT_CHUNK_SIZE", "DEFAULT_OVERLAP", "normalize_text", "split_text"]
