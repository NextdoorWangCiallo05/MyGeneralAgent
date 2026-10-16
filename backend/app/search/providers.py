"""搜索后端预设与接入配置解析。

与 `app/llm/providers.py` 同一套思路：能选的搜索服务定义成一张表，
新增一家通常只需在 `PRESETS` 里加一条 + 一个 `backends/*.py` 实现。

配置来源优先级：后台运行时配置（system_config 表） > 环境变量 / .env > 预设。
解析统一走 `resolve_search_config()`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.search.backends import (
    BingBackend,
    BraveBackend,
    DuckDuckGoBackend,
    SearxngBackend,
    SerperBackend,
    TavilyBackend,
)
from app.search.base import SearchBackend, SearchError, SearchNotConfiguredError

if TYPE_CHECKING:  # 仅类型检查期需要，避免与配置层互相导入
    from app.config import Settings


@dataclass(frozen=True, slots=True)
class SearchPreset:
    """一家搜索服务的最小描述。"""

    id: str
    label: str
    #: 服务地址；免密钥的结果页后端用官方站点，自建类留空由管理员填写
    base_url: str = ""
    requires_api_key: bool = True
    #: 密钥形态提示，渲染在后台输入框的占位符上
    key_hint: str = ""
    hint: str = ""
    #: 「自动」模式下参与尝试的顺序，0 表示不参与
    auto_order: int = 0


PRESETS: tuple[SearchPreset, ...] = (
    SearchPreset(
        id="auto",
        label="自动（免密钥后端依次尝试）",
        requires_api_key=False,
        hint="按 Bing → DuckDuckGo 顺序尝试，任一成功即返回；开箱可用，无需密钥。",
    ),
    SearchPreset(
        id="bing",
        label="Bing 网页搜索",
        base_url="https://www.bing.com",
        requires_api_key=False,
        hint="抓取公开结果页，免密钥、国内可直连；高峰期可能触发风控。",
        auto_order=1,
    ),
    SearchPreset(
        id="duckduckgo",
        label="DuckDuckGo（Lite）",
        requires_api_key=False,
        hint="抓取免登录结果页；部分网络（如国内直连）不可达，需自备代理。",
        auto_order=2,
    ),
    SearchPreset(
        id="tavily",
        label="Tavily",
        base_url="https://api.tavily.com",
        key_hint="tvly-...",
        hint="返回正文片段而非一行摘要，信息量更大；免费额度每月 1000 次。",
    ),
    SearchPreset(
        id="serper",
        label="Serper（Google 结果）",
        base_url="https://google.serper.dev",
        key_hint="在 serper.dev 获取的密钥",
        hint="返回 Google 结果，适合需要 Google 覆盖面的场景。",
    ),
    SearchPreset(
        id="brave",
        label="Brave Search API",
        base_url="https://api.search.brave.com",
        key_hint="Brave 控制台的订阅令牌",
        hint="独立索引，免费额度每月 2000 次。",
    ),
    SearchPreset(
        id="searxng",
        label="SearXNG（自建实例）",
        requires_api_key=False,
        key_hint="实例若开启了鉴权再填",
        hint="填自建实例地址；需在实例设置中开启 json 输出格式。",
    ),
)

_BY_ID: dict[str, SearchPreset] = {preset.id: preset for preset in PRESETS}

#: 默认走「自动」：不要求任何密钥，且能在网络环境变化时自行挑可用后端
DEFAULT_PROVIDER_ID = "auto"


def list_presets() -> tuple[SearchPreset, ...]:
    return PRESETS


def get_preset(provider_id: str) -> SearchPreset | None:
    return _BY_ID.get((provider_id or "").strip().lower())


def provider_not_found_message(provider_id: str) -> str:
    return f"未知的搜索后端 {provider_id!r}，可选：" + "、".join(_BY_ID)


@dataclass(frozen=True, slots=True)
class SearchConfig:
    """一次搜索真正使用的接入参数（含明文密钥，只在进程内流转）。"""

    provider: str
    label: str
    base_url: str
    api_key: str
    locale: str
    max_results: int
    timeout: float
    requires_api_key: bool


def resolve_search_config(settings: Settings) -> SearchConfig:
    """把多层配置解析成一份确定的接入参数。"""
    provider_id = (settings.search_provider or DEFAULT_PROVIDER_ID).strip().lower()
    preset = get_preset(provider_id)
    if preset is None:
        raise SearchNotConfiguredError(provider_not_found_message(settings.search_provider))

    return SearchConfig(
        provider=provider_id,
        label=preset.label,
        base_url=(settings.search_base_url or preset.base_url).rstrip("/"),
        api_key=(settings.search_api_key or "").strip(),
        locale=settings.search_locale or "zh-CN",
        max_results=max(1, settings.web_search_max_results),
        timeout=settings.search_timeout,
        requires_api_key=preset.requires_api_key,
    )


def build_search_backend(config: SearchConfig, provider_id: str = "") -> SearchBackend:
    """按解析结果构造单个后端；`provider_id` 用于「自动」链里逐个构造。"""
    target = (provider_id or config.provider).strip().lower()
    if target == "bing":
        return BingBackend(
            base_url=config.base_url or "https://www.bing.com",
            locale=config.locale,
            timeout=config.timeout,
        )
    if target == "duckduckgo":
        return DuckDuckGoBackend(timeout=config.timeout)
    if target == "tavily":
        return TavilyBackend(
            api_key=config.api_key, base_url=config.base_url, timeout=config.timeout
        )
    if target == "serper":
        return SerperBackend(
            api_key=config.api_key, base_url=config.base_url, timeout=config.timeout
        )
    if target == "brave":
        return BraveBackend(
            api_key=config.api_key, base_url=config.base_url, timeout=config.timeout
        )
    if target == "searxng":
        return SearxngBackend(
            api_key=config.api_key, base_url=config.base_url, timeout=config.timeout
        )
    raise SearchNotConfiguredError(provider_not_found_message(target))


def build_search_chain(config: SearchConfig) -> list[SearchBackend]:
    """给出本次搜索要依次尝试的后端列表。

    - 指定后端时只有它自己（失败即失败，便于管理员定位问题）；
    - 「自动」时按预设里的 `auto_order` 排列免密钥后端，逐个尝试直到成功。
    """
    if config.provider != DEFAULT_PROVIDER_ID:
        return [build_search_backend(config)]

    ordered = sorted(
        (preset for preset in PRESETS if preset.auto_order > 0),
        key=lambda preset: preset.auto_order,
    )
    backends: list[SearchBackend] = []
    for preset in ordered:
        try:
            backends.append(build_search_backend(config, preset.id))
        except SearchError:  # pragma: no cover - 预设表内置项不会走到
            continue
    return backends


__all__ = [
    "DEFAULT_PROVIDER_ID",
    "PRESETS",
    "SearchConfig",
    "SearchPreset",
    "build_search_backend",
    "build_search_chain",
    "get_preset",
    "list_presets",
    "provider_not_found_message",
    "resolve_search_config",
]
