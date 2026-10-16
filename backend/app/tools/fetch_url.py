"""网页抓取工具：取回单个 URL 的正文文本。

安全约束（对应《技术设计文档》4.8 的沙箱要求）：

- 拒绝内网 / 环回 / 链路本地地址，避免模型被诱导访问内部服务（SSRF）；
- 手动逐跳跟随重定向，**每一跳都重新校验**目标主机，防止跳转绕过校验；
- 用流式读取限制响应体积，只接受文本类内容，超限即截断。
"""

from __future__ import annotations

import asyncio
import html as html_lib
import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.tools.base import Tool, ToolResult

logger = logging.getLogger("app.tools.fetch_url")

_USER_AGENT = "Mozilla/5.0 (compatible; AgentApp/1.0; +https://example.com/bot)"
_MAX_REDIRECTS = 3
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml")
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal"}

_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript|template)\b.*?</\1>", re.S | re.I)
_BREAK_RE = re.compile(r"</?(?:p|div|br|li|h[1-6]|tr|section|article|header|footer)\b[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\u00a0]+")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """内网 / 环回 / 链路本地 / 保留地址一律拒绝。"""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def assert_public_url(url: str) -> None:
    """校验目标为可公开访问的 http(s) 地址，否则抛 ValueError。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("只支持 http / https 链接")

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("链接缺少主机名")
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        raise ValueError("禁止访问内网地址")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise ValueError(f"域名解析失败：{host}") from exc

    for info in infos:
        try:
            if _is_blocked_ip(ipaddress.ip_address(info[4][0])):
                raise ValueError("禁止访问内网地址")
        except ValueError as exc:
            if "禁止访问内网地址" in str(exc):
                raise
    if not infos:
        raise ValueError(f"无法解析主机：{host}")


async def _http_get(url: str) -> tuple[str, str, str]:
    """抓取页面并返回 (最终地址, content-type, 正文)，独立函数便于测试替换。"""
    settings = get_settings()
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.5",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    current = url
    async with httpx.AsyncClient(
        timeout=settings.http_timeout, follow_redirects=False, headers=headers
    ) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            await assert_public_url(current)
            async with client.stream("GET", current) as response:
                if response.is_redirect and response.headers.get("location"):
                    current = str(httpx.URL(current).join(response.headers["location"]))
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if not content_type.lower().startswith(_TEXT_TYPES):
                    raise ValueError(f"不支持的内容类型：{content_type or '未知'}")

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= settings.fetch_max_bytes:
                        break
                body = b"".join(chunks)[: settings.fetch_max_bytes]
                return current, content_type, body.decode("utf-8", "replace")
    raise ValueError("重定向次数过多")


def html_to_text(markup: str) -> str:
    """把 HTML 粗略转换为可读文本（纯函数，便于单测）。"""
    text = _SCRIPT_STYLE_RE.sub(" ", markup)
    text = _BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    text = _SPACE_RE.sub(" ", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


async def _fetch_url(url: str) -> ToolResult:
    target = (url or "").strip()
    if not target:
        return ToolResult(status="error", summary="链接为空", content="错误：url 不能为空")

    try:
        final_url, content_type, body = await _http_get(target)
    except (ValueError, httpx.HTTPError) as exc:
        logger.info("网页抓取失败 %s: %s", target, exc)
        return ToolResult(
            status="error",
            summary=f"抓取失败：{exc}",
            content=f"错误：无法抓取该网页（{exc}）。请更换链接或依据已有信息回答。",
        )

    is_markup = "html" in content_type.lower() or "xml" in content_type.lower()
    text = html_to_text(body) if is_markup else body.strip()
    if not text:
        return ToolResult(status="error", summary="页面正文为空", content="错误：未提取到正文内容")
    if "html" not in content_type.lower() and "json" not in content_type.lower():
        text = f"[{content_type}]\n{text}"

    return ToolResult(
        status="success",
        summary=f"已抓取 {final_url}（{len(text)} 字符）",
        content=text,
        data={"url": final_url, "content_type": content_type, "length": len(text)},
    )


fetch_url_tool = Tool(
    name="fetch_url",
    description=(
        "抓取指定网页的正文文本。适合在 web_search 得到链接后深入了解具体内容。"
        "参数 url 必须是完整的 http/https 链接。仅支持公开可访问的文本类页面。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "完整的 http/https 链接"},
        },
        "required": ["url"],
    },
    handler=_fetch_url,
    readonly=True,
    timeout=25.0,
)


__all__ = ["assert_public_url", "fetch_url_tool", "html_to_text"]
