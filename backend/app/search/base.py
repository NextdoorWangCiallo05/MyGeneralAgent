"""搜索后端的公共契约与 HTTP 辅助。

上层（`web_search` 工具）只依赖 `SearchBackend` 协议与 `SearchResult`，
换服务只改 `app/search/providers.py` 的预设表，因此新增一家搜索服务
不会影响工具定义、提示词与前端。
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

#: 免密钥后端多为结果页抓取，默认 UA 会被拦，这里伪装成常见浏览器
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.S | re.I)


class SearchError(RuntimeError):
    """搜索失败（网络不可达、限流、解析为空等）。

    异常信息是**面向用户/模型的中文短句**，会被拼进工具结果直接展示。
    """


class SearchNotConfiguredError(SearchError):
    """后端缺少必要配置，多为缺 API-Key 或实例地址。"""


@dataclass(slots=True)
class SearchResult:
    """一条搜索命中。"""

    title: str
    url: str
    snippet: str = ""
    #: 产出该条目的后端名（如「Bing 网页搜索」），用于回答时标注来源
    source: str = ""


@dataclass(slots=True)
class SearchOutcome:
    """一次搜索调用的结果（含实际生效的后端，便于容错链记录）。"""

    backend_id: str
    backend_label: str
    results: list[SearchResult] = field(default_factory=list)


class SearchBackend(Protocol):
    """一种搜索服务的实现。"""

    id: str
    label: str

    async def search(self, query: str, *, limit: int) -> list[SearchResult]:
        """执行搜索，失败一律抛 `SearchError`。"""
        ...


def strip_tags(fragment: str) -> str:
    """去掉 HTML 标签、脚本与多余空白，返回纯文本。"""
    text = _SCRIPT_RE.sub("", fragment or "")
    text = _TAG_RE.sub("", text)
    return " ".join(html_lib.unescape(text).split())


def describe_http_error(exc: httpx.HTTPError) -> str:
    """把 httpx 异常翻译成可读原因（网络不可达是最常见的一种）。"""
    if isinstance(exc, httpx.TimeoutException):
        return "请求超时"
    if isinstance(exc, httpx.ConnectError):
        return "网络不可达（可能被墙，需自备代理）"
    return str(exc) or exc.__class__.__name__


def default_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if extra:
        headers.update(extra)
    return headers


async def request_text(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
    data: dict[str, Any] | None = None,
    method: str = "GET",
) -> str:
    """请求并返回响应文本；失败抛 `SearchError`（工具调用频率下无需连接池）。"""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.request(
                method, url, params=params, data=data, headers=default_headers(headers)
            )
            response.raise_for_status()
            return response.text
    except httpx.HTTPStatusError as exc:
        raise SearchError(f"HTTP {exc.response.status_code}（{url}）") from exc
    except httpx.HTTPError as exc:
        raise SearchError(describe_http_error(exc)) from exc


async def request_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
    payload: dict[str, Any] | None = None,
    method: str | None = None,
) -> Any:
    """请求 JSON 接口；失败抛 `SearchError`。"""
    verb = method or ("POST" if payload is not None else "GET")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.request(
                verb,
                url,
                params=params,
                json=payload,
                headers=default_headers({"Accept": "application/json", **(headers or {})}),
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {401, 403}:
            raise SearchError("鉴权失败（API-Key 可能无效或无权限）") from exc
        if status == 429:
            raise SearchError("触发限流（免费额度可能已用尽）") from exc
        raise SearchError(f"HTTP {status}（{url}）") from exc
    except httpx.HTTPError as exc:
        raise SearchError(describe_http_error(exc)) from exc
    except ValueError as exc:  # 响应不是 JSON
        raise SearchError(f"响应不是合法 JSON：{exc}") from exc


__all__ = [
    "DEFAULT_USER_AGENT",
    "SearchBackend",
    "SearchError",
    "SearchNotConfiguredError",
    "SearchOutcome",
    "SearchResult",
    "default_headers",
    "describe_http_error",
    "request_json",
    "request_text",
    "strip_tags",
]
