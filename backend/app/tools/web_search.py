"""联网搜索工具：调用可插拔的搜索后端（见 `app/search/`）。

后端由管理后台配置：默认「自动」依次尝试免密钥的 Bing / DuckDuckGo，
也可切到 Tavily / Serper / Brave（需 API-Key）或自建 SearXNG。
本模块只负责：解析运行时配置 → 逐个后端尝试 → 把结果整理成模型易用的文本。
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from app.core.runtime_config import get_runtime_config
from app.search import (
    SearchBackend,
    SearchError,
    SearchNotConfiguredError,
    SearchResult,
    build_search_chain,
    resolve_search_config,
)
from app.tools.base import Tool, ToolResult

logger = logging.getLogger("app.tools.web_search")

_FOOTER = (
    "以上为实时检索结果：回答时请引用对应链接并说明来源，"
    "结果中未出现的事实不要编造；需要正文时对链接调用 fetch_url。"
)

#: 提示模型/用户下一步能做什么，避免「搜索失败」成为死路
_FAILURE_HINT = (
    "可提示用户：① 稍后重试或换关键词；② 由管理员在后台「搜索后端」里换成可用的服务"
    "（免密钥的 Bing / DuckDuckGo，或填 API-Key 使用 Tavily / Serper / Brave / SearXNG）；"
    "③ 若已知具体网址，直接用 fetch_url 抓取。"
)


async def _search_with_fallback(
    query: str, *, backends: list[SearchBackend], limit: int
) -> tuple[list[SearchResult], list[str], SearchBackend]:
    """依次尝试后端，返回 (结果, 失败记录, 生效后端)；全部失败时抛 `SearchError`。"""
    failures: list[str] = []
    for backend in backends:
        try:
            results = await backend.search(query, limit=limit)
        except SearchError as exc:
            logger.warning("搜索后端 %s 失败: %s", backend.id, exc)
            failures.append(f"{backend.label}（{exc}）")
            continue
        return results, failures, backend
    raise SearchError("；".join(failures) or "没有可尝试的搜索后端")


def _render(query: str, results: list[SearchResult], backend: SearchBackend, failures: list[str]) -> str:
    lines = [f"搜索「{query}」命中 {len(results)} 条（来源：{backend.label}）："]
    for index, item in enumerate(results, start=1):
        lines.append(f"\n{index}. {item.title}\n   {item.url}\n   {item.snippet}".rstrip())
    if failures:
        lines.append(f"\n（注：{'；'.join(failures)} 不可用，已自动改用可用的后端。）")
    lines.append(f"\n{_FOOTER}")
    return "\n".join(lines)


async def _web_search(query: str) -> ToolResult:
    text = (query or "").strip()
    if not text:
        return ToolResult(status="error", summary="搜索关键词为空", content="错误：query 不能为空")

    settings = await get_runtime_config().effective_settings()
    try:
        config = resolve_search_config(settings)
        backends = build_search_chain(config)
        if not backends:
            raise SearchNotConfiguredError("没有可用的搜索后端，请在管理后台选择搜索后端")
    except SearchNotConfiguredError as exc:
        logger.warning("搜索后端未就绪: %s", exc)
        return ToolResult(
            status="error",
            summary=f"搜索后端未就绪：{exc}",
            content=f"错误：联网搜索不可用（{exc}）。\n{_FAILURE_HINT}",
        )

    try:
        results, failures, backend = await _search_with_fallback(
            text, backends=backends, limit=config.max_results
        )
    except SearchError as exc:
        logger.warning("联网搜索全部失败: %s", exc)
        return ToolResult(
            status="error",
            summary=f"联网搜索失败：{exc}",
            content=f"错误：联网搜索失败（{exc}）。\n{_FAILURE_HINT}",
            data={"query": text, "results": [], "provider": config.provider},
        )

    return ToolResult(
        status="success",
        summary=f"{backend.label} 找到 {len(results)} 条结果",
        content=_render(text, results, backend, failures),
        data={
            "query": text,
            "provider": config.provider,
            "backend": backend.id,
            "results": [asdict(item) for item in results],
        },
    )


web_search_tool = Tool(
    name="web_search",
    description=(
        "联网搜索实时信息。当问题涉及最新事件、时效性数据、近期发布的内容，"
        "或你不确定的事实时使用，不要凭记忆编造。参数 query 为搜索关键词，尽量具体。"
        "返回多条结果的标题、链接与摘要（含来源后端），需要正文时再对链接调用 fetch_url。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词，例如 'FastAPI 0.115 新特性'"},
        },
        "required": ["query"],
    },
    handler=_web_search,
    readonly=True,
    # 「自动」模式会依次尝试多个后端，超时按「单后端超时 × 后端数 + 余量」放宽
    timeout=45.0,
)


__all__ = ["web_search_tool"]
