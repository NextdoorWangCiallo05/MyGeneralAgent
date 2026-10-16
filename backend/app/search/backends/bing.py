"""Bing 网页搜索（免密钥）：抓取公开结果页并解析。

选它做默认后端的原因：免密钥、国内可直连、结果质量稳定。
解析逻辑抽成纯函数 `parse_results`，页面结构变化时只改正则即可回归。
"""

from __future__ import annotations

import base64
import re
from urllib.parse import parse_qs, urlparse

from app.search.base import SearchError, SearchResult, request_text, strip_tags

_BLOCK_SPLIT_RE = re.compile(r'<li[^>]*class="[^"]*\bb_algo\b[^"]*"', re.I)
_H2_ANCHOR_RE = re.compile(r"<h2\b[^>]*>\s*<a\b([^>]*)>(.*?)</a>", re.S | re.I)
_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)
#: 摘要的几种常见容器，按优先级依次尝试
_SNIPPET_RES = (
    re.compile(r'<p\b[^>]*class="[^"]*\bb_lineclamp\w*\b[^"]*"[^>]*>(.*?)</p>', re.S | re.I),
    re.compile(r'<p\b[^>]*class="[^"]*\bb_snippet\b[^"]*"[^>]*>(.*?)</p>', re.S | re.I),
    re.compile(r'<div\b[^>]*class="[^"]*\bb_caption\b[^"]*"[^>]*>(.*?)</div>', re.S | re.I),
)


def _decode_bing_link(href: str) -> str:
    """Bing 有时把结果链接包成 /ck/a?...&u=a1<base64>，这里还原真实地址。"""
    if "bing.com/ck/a" not in href:
        return href
    raw = (parse_qs(urlparse(href).query).get("u") or [""])[0]
    if not raw.startswith("a1"):
        return ""
    payload = raw[2:]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
    except (ValueError, UnicodeDecodeError):  # pragma: no cover - 解码失败按丢弃处理
        return ""


def _snippet_of(block: str) -> str:
    for pattern in _SNIPPET_RES:
        found = pattern.search(block)
        if found:
            text = strip_tags(found.group(1))
            if text:
                return text
    return ""


def parse_results(markup: str, *, limit: int) -> list[SearchResult]:
    """从 Bing 结果页 HTML 中提取标题 / 链接 / 摘要（纯函数，便于单测）。

    逐块解析而不是整页大正则：结果块之间夹着样式表等噪声，整页匹配容易串味。
    """
    results: list[SearchResult] = []
    blocks = _BLOCK_SPLIT_RE.split(markup or "")
    # 第一段是 <li> 之前的头部，不含结果
    for block in blocks[1:]:
        anchor = _H2_ANCHOR_RE.search(block)
        if anchor is None:
            continue
        href = _HREF_RE.search(anchor.group(1))
        if href is None:
            continue
        title = strip_tags(anchor.group(2))
        url = _decode_bing_link(href.group(1).strip())
        if not title or not url.startswith(("http://", "https://")):
            continue
        results.append(SearchResult(title=title, url=url, snippet=_snippet_of(block)))
        if len(results) >= limit:
            break
    return results


class BingBackend:
    """Bing 网页搜索后端。"""

    id = "bing"
    label = "Bing 网页搜索"

    def __init__(
        self,
        *,
        base_url: str = "https://www.bing.com",
        locale: str = "zh-CN",
        timeout: float = 20.0,
    ) -> None:
        self._base_url = (base_url or "https://www.bing.com").rstrip("/")
        self._locale = locale or "zh-CN"
        self._timeout = timeout

    async def search(self, query: str, *, limit: int) -> list[SearchResult]:
        markup = await request_text(
            f"{self._base_url}/search",
            params={
                "q": query,
                "mkt": self._locale,
                "setlang": self._locale,
                "count": str(max(limit, 5)),
            },
            timeout=self._timeout,
        )
        results = parse_results(markup, limit=limit)
        if not results:
            raise SearchError("未从结果页解析出条目（可能被风控拦截或页面结构变化）")
        return [
            SearchResult(
                title=item.title, url=item.url, snippet=item.snippet, source=self.label
            )
            for item in results
        ]


__all__ = ["BingBackend", "parse_results"]
