"""DuckDuckGo Lite（免密钥）：抓取免登录结果页并解析。

对能直连 DDG 的网络是零配置可用项，也是「自动」后端链里的第二顺位。
"""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import parse_qs, unquote, urlparse

from app.search.base import SearchError, SearchResult, request_text, strip_tags

_ENDPOINT = "https://lite.duckduckgo.com/lite/"

_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)
_CLASS_RE = re.compile(r"""class=["']([^"']*)["']""", re.I)
_SNIPPET_RE = re.compile(
    r"""<td\b[^>]*class=["']result-snippet["'][^>]*>(.*?)</td>""", re.S | re.I
)


def _unwrap(href: str) -> str:
    """结果链接是跳转形式（uddg=...），这里还原出真实地址。"""
    if "uddg=" in href:
        query = parse_qs(urlparse(href).query)
        if query.get("uddg"):
            return unquote(query["uddg"][0])
    if href.startswith("//"):
        return f"https:{href}"
    return href


def parse_results(markup: str, *, limit: int) -> list[SearchResult]:
    """从结果页 HTML 中提取标题 / 链接 / 摘要（纯函数，便于单测）。

    不假设 `href` 与 `class` 的先后顺序：先取锚点整体，再分别抽取属性。
    """
    snippets = [strip_tags(item) for item in _SNIPPET_RE.findall(markup or "")]
    results: list[SearchResult] = []
    for attributes, title_html in _ANCHOR_RE.findall(markup or ""):
        classes = _CLASS_RE.search(attributes)
        if not classes or "result-link" not in classes.group(1):
            continue
        href = _HREF_RE.search(attributes)
        if href is None:
            continue

        title = strip_tags(title_html)
        url = _unwrap(html_lib.unescape(href.group(1)))
        if not title or not url.startswith(("http://", "https://")):
            continue
        index = len(results)
        results.append(
            SearchResult(
                title=title, url=url, snippet=snippets[index] if index < len(snippets) else ""
            )
        )
        if len(results) >= limit:
            break
    return results


class DuckDuckGoBackend:
    """DuckDuckGo Lite 后端。"""

    id = "duckduckgo"
    label = "DuckDuckGo（Lite）"

    def __init__(self, *, timeout: float = 20.0) -> None:
        self._timeout = timeout

    async def search(self, query: str, *, limit: int) -> list[SearchResult]:
        markup = await request_text(
            _ENDPOINT, data={"q": query}, method="POST", timeout=self._timeout
        )
        results = parse_results(markup, limit=limit)
        if not results:
            raise SearchError("未从结果页解析出条目（可能被风控拦截或页面结构变化）")
        return [
            SearchResult(title=item.title, url=item.url, snippet=item.snippet, source=self.label)
            for item in results
        ]


__all__ = ["DuckDuckGoBackend", "parse_results"]
