"""知识库检索工具（M4）：只读、免确认，结果严格限定在当前用户的知识库内。"""

from __future__ import annotations

from app.config import get_settings
from app.core.embedding import get_embedder
from app.core.retriever import search_knowledge
from app.store import get_store
from app.tools.base import Tool, ToolResult
from app.tools.context import require_tool_context

#: 回喂给模型的单条片段最大字符数（避免检索结果挤占上下文）
SNIPPET_CHARS = 400


def _snippet(content: str) -> str:
    text = " ".join((content or "").split())
    if len(text) <= SNIPPET_CHARS:
        return text
    return text[:SNIPPET_CHARS] + "…"


async def _knowledge_search(query: str, top_k: int = 0) -> ToolResult:
    context = require_tool_context()
    settings = get_settings()
    embedder = get_embedder(settings)
    if embedder is None:
        return ToolResult(
            status="error",
            summary="知识库检索不可用",
            content="错误：当前未启用向量化（EMBEDDING_BACKEND=none），无法检索知识库。请如实告知用户该功能当前不可用。",
        )

    text = (query or "").strip()
    if not text:
        return ToolResult(status="error", summary="检索词为空", content="错误：`query` 不能为空")

    hits = await search_knowledge(
        get_store(),
        context.user_id,
        text,
        top_k=top_k or None,
        embedder=embedder,
        settings=settings,
    )
    if not hits:
        return ToolResult(
            status="success",
            summary="知识库中未找到相关内容",
            content=(
                "知识库中没有与该查询相关的片段（可能尚未上传相关文档、尚未构建索引，"
                "或者问题与已上传内容无关）。请如实告知用户，不要编造资料内容。"
            ),
            data={"query": text, "hits": 0},
        )

    lines = [f"从用户知识库中检索到 {len(hits)} 个相关片段（按相似度降序）："]
    for index, hit in enumerate(hits, start=1):
        lines.append(
            f"\n【片段 {index}】来源：{hit.filename}"
            f"（第 {hit.seq + 1} 块，相似度 {hit.score:.3f}）\n{_snippet(hit.content)}"
        )
    lines.append("\n请基于以上片段回答，并标注引用的文档名；若片段与问题无关，请如实说明。")
    return ToolResult(
        status="success",
        summary=f"检索到 {len(hits)} 个片段（来自 {hits[0].filename} 等）",
        content="\n".join(lines),
        data={
            "query": text,
            "hits": len(hits),
            "documents": sorted({hit.filename for hit in hits}),
            "top_score": round(hits[0].score, 4),
        },
    )


knowledge_search_tool = Tool(
    name="knowledge_search",
    description=(
        "在用户个人的知识库（其上传的文档）中检索最相关的原文片段。"
        "当用户的问题可能与其上传的资料、文档、笔记有关，或用户提到「我的文档 / 我的资料 / 知识库」时，"
        "应优先调用该工具再作答。参数 query 建议传完整的自然语言问句。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索用的问句或关键词，例如「项目的部署流程是什么」"},
            "top_k": {
                "type": "integer",
                "description": "返回片段数量，默认 5，最大 20",
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["query"],
    },
    handler=_knowledge_search,
    requires_confirmation=False,
    readonly=True,
    knowledge_required=True,
    # 首次调用可能触发本地模型加载，超时放宽
    timeout=60.0,
)


__all__ = ["knowledge_search_tool"]
