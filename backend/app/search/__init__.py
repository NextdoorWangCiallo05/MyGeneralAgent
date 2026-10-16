"""联网搜索：可插拔的搜索后端（见 README「联网搜索」）。

对外入口只有三个：`resolve_search_config()` 解析配置、`build_search_chain()`
得到要尝试的后端列表、遍历调用 `SearchBackend.search()`。
"""

from app.search.base import (
    SearchBackend,
    SearchError,
    SearchNotConfiguredError,
    SearchOutcome,
    SearchResult,
)
from app.search.providers import (
    DEFAULT_PROVIDER_ID,
    PRESETS,
    SearchConfig,
    SearchPreset,
    build_search_backend,
    build_search_chain,
    get_preset,
    list_presets,
    provider_not_found_message,
    resolve_search_config,
)

__all__ = [
    "DEFAULT_PROVIDER_ID",
    "PRESETS",
    "SearchBackend",
    "SearchConfig",
    "SearchError",
    "SearchNotConfiguredError",
    "SearchOutcome",
    "SearchPreset",
    "SearchResult",
    "build_search_backend",
    "build_search_chain",
    "get_preset",
    "list_presets",
    "provider_not_found_message",
    "resolve_search_config",
]
