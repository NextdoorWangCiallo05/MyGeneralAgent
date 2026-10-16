"""需要 API-Key（或实例地址）的搜索后端：Tavily / Serper / Brave / SearXNG。

四个后端的差异只在「请求长什么样」与「响应字段叫什么」，因此共用一个
`_ApiBackend` 基类，子类各自实现 `_build_request` 与 `_parse`。
"""

from __future__ import annotations

from typing import Any

from app.search.base import (
    SearchError,
    SearchNotConfiguredError,
    SearchResult,
    request_json,
)


class _ApiBackendBase:
    """JSON 搜索 API 的通用流程：校验配置 → 组装请求 → 解析响应。"""

    id = "api"
    label = "搜索 API"
    requires_api_key = True

    def __init__(self, *, api_key: str = "", base_url: str = "", timeout: float = 20.0) -> None:
        self._api_key = (api_key or "").strip()
        self._base_url = (base_url or "").rstrip("/")
        self._timeout = timeout

    # ------------------------------------------------------------ 子类实现
    def _build_request(
        self, query: str, limit: int
    ) -> tuple[str, dict[str, Any] | None, dict[str, str], dict[str, Any] | None]:
        """返回 (url, params, headers, payload)。"""
        raise NotImplementedError

    def _parse(self, data: Any, limit: int) -> list[SearchResult]:
        raise NotImplementedError

    # ------------------------------------------------------------ 通用流程
    def _check_config(self) -> None:
        if self.requires_api_key and not self._api_key:
            raise SearchNotConfiguredError(f"{self.label} 未配置 API-Key")

    async def search(self, query: str, *, limit: int) -> list[SearchResult]:
        self._check_config()
        url, params, headers, payload = self._build_request(query, limit)
        data = await request_json(
            url, params=params, headers=headers, payload=payload, timeout=self._timeout
        )
        results = self._parse(data, limit)
        if not results:
            raise SearchError("接口返回 0 条结果")
        return [
            SearchResult(title=item.title, url=item.url, snippet=item.snippet, source=self.label)
            for item in results
        ]


class TavilyBackend(_ApiBackendBase):
    """Tavily：为 LLM 优化，直接返回正文摘要。"""

    id = "tavily"
    label = "Tavily"

    def _build_request(self, query, limit):
        url = f"{self._base_url or 'https://api.tavily.com'}/search"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        payload = {
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
            "include_answer": False,
        }
        return url, None, headers, payload

    def _parse(self, data, limit):
        items = (data or {}).get("results") or []
        return [
            SearchResult(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                # Tavily 的 content 往往是正文片段，比一行摘要信息量更大
                snippet=" ".join(str(item.get("content") or "").split()),
            )
            for item in items[:limit]
            if str(item.get("url") or "").startswith("http")
        ]


class SerperBackend(_ApiBackendBase):
    """Serper：返回 Google 结果。"""

    id = "serper"
    label = "Serper（Google 结果）"

    def _build_request(self, query, limit):
        url = f"{self._base_url or 'https://google.serper.dev'}/search"
        headers = {"X-API-KEY": self._api_key}
        payload = {"q": query, "num": limit, "gl": "cn", "hl": "zh-cn"}
        return url, None, headers, payload

    def _parse(self, data, limit):
        items = (data or {}).get("organic") or []
        return [
            SearchResult(
                title=str(item.get("title") or ""),
                url=str(item.get("link") or ""),
                snippet=" ".join(str(item.get("snippet") or "").split()),
            )
            for item in items[:limit]
            if str(item.get("link") or "").startswith("http")
        ]


class BraveBackend(_ApiBackendBase):
    """Brave Search API。"""

    id = "brave"
    label = "Brave Search API"

    def _build_request(self, query, limit):
        url = f"{self._base_url or 'https://api.search.brave.com'}/res/v1/web/search"
        headers = {"X-Subscription-Token": self._api_key}
        params = {"q": query, "count": limit}
        return url, params, headers, None

    def _parse(self, data, limit):
        items = ((data or {}).get("web") or {}).get("results") or []
        results: list[SearchResult] = []
        for item in items[:limit]:
            url = str(item.get("url") or "")
            if not url.startswith("http"):
                continue
            snippet = str(item.get("description") or "")
            # Brave 的 description 含 <strong> 高亮标签
            snippet = " ".join(snippet.replace("<strong>", "").replace("</strong>", "").split())
            results.append(
                SearchResult(title=str(item.get("title") or ""), url=url, snippet=snippet)
            )
        return results


class SearxngBackend(_ApiBackendBase):
    """SearXNG：自建或私有实例，免密钥（需实例开启 json 输出）。"""

    id = "searxng"
    label = "SearXNG（自建实例）"
    requires_api_key = False

    def _check_config(self) -> None:
        if not self._base_url:
            raise SearchNotConfiguredError("SearXNG 未填写实例地址（base_url）")

    def _build_request(self, query, limit):
        url = f"{self._base_url}/search"
        params = {"q": query, "format": "json", "language": "zh-CN"}
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        return url, params, headers, None

    def _parse(self, data, limit):
        items = (data or {}).get("results") or []
        return [
            SearchResult(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=" ".join(str(item.get("content") or "").split()),
            )
            for item in items[:limit]
            if str(item.get("url") or "").startswith("http")
        ]


__all__ = ["BraveBackend", "SearxngBackend", "SerperBackend", "TavilyBackend"]
