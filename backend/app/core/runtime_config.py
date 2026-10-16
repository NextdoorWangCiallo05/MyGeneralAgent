"""业务配置的运行时访问层（`system_config` 表）。

对应《技术设计文档》11.3 节的两层配置：
- **基础设施配置**走 `.env`（数据库、JWT、加密密钥……），由 `app.config` 读取；
- **业务配置**存 `system_config` 表，由管理员在后台维护，运行时可改。

本模块负责后者，并遵循两条规则：

1. **库优先、环境变量兜底**——未做后台配置时行为与 M1 完全一致，
   已有部署无需改动 `.env` 即可继续运行；
2. **敏感项密文入库**（见 `app.core.crypto`），
   读取时解密，对外展示一律脱敏，明文只在本进程内流转。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.core.crypto import decrypt_value, encrypt_value, mask_secret
from app.llm.client import LLMNotConfiguredError
from app.llm.providers import DEFAULT_PROVIDER_ID, resolve_llm_config
from app.search import SearchError, build_search_chain, resolve_search_config
from app.store import AbstractStore, ConfigItem, get_store

logger = logging.getLogger("app.config")

# ---------------------------------------------------------------- 配置键
KEY_LLM_PROVIDER = "llm_provider"
KEY_LLM_API_KEY = "llm_api_key"
KEY_LLM_BASE_URL = "llm_base_url"
KEY_DEFAULT_MODEL = "default_model"
# 旧键：厂商为 deepseek 时等价于 llm_api_key / llm_base_url，保留以兼容已有部署
KEY_DEEPSEEK_API_KEY = "deepseek_api_key"
KEY_DEEPSEEK_BASE_URL = "deepseek_base_url"
KEY_SANDBOX_GLOBAL_ENABLED = "sandbox_global_enabled"
KEY_MAX_STEPS = "max_steps"
KEY_TOOL_TIMEOUT = "tool_timeout"
# 联网搜索（见 app/search/）
KEY_SEARCH_PROVIDER = "search_provider"
KEY_SEARCH_API_KEY = "search_api_key"
KEY_SEARCH_BASE_URL = "search_base_url"
KEY_SEARCH_MAX_RESULTS = "web_search_max_results"

#: 配置键 -> 字段名。`RuntimeConfig` 与 `Settings` 对这些键使用同一个字段名，
#: 因此兜底值、指纹、合并、可写键集合都从这一张表派生，新增配置只改这一处。
_CONFIG_FIELDS: tuple[tuple[str, str], ...] = (
    (KEY_LLM_PROVIDER, "llm_provider"),
    (KEY_LLM_API_KEY, "llm_api_key"),
    (KEY_LLM_BASE_URL, "llm_base_url"),
    (KEY_DEFAULT_MODEL, "default_model"),
    (KEY_DEEPSEEK_API_KEY, "deepseek_api_key"),
    (KEY_DEEPSEEK_BASE_URL, "deepseek_base_url"),
    (KEY_SEARCH_PROVIDER, "search_provider"),
    (KEY_SEARCH_API_KEY, "search_api_key"),
    (KEY_SEARCH_BASE_URL, "search_base_url"),
    (KEY_SEARCH_MAX_RESULTS, "web_search_max_results"),
    (KEY_SANDBOX_GLOBAL_ENABLED, "sandbox_global_enabled"),
    (KEY_MAX_STEPS, "max_steps"),
    (KEY_TOOL_TIMEOUT, "tool_timeout"),
)

#: 敏感配置键：入库前必须加密
SECRET_KEYS = frozenset({KEY_LLM_API_KEY, KEY_DEEPSEEK_API_KEY, KEY_SEARCH_API_KEY})

#: 允许在后台维护的全部配置键
SUPPORTED_KEYS = frozenset(key for key, _ in _CONFIG_FIELDS)


class RuntimeConfig(BaseModel):
    """一次读取得到的业务配置快照。

    `llm_api_key` / `deepseek_api_key` 为**明文**，仅供后端内部使用，
    绝不可直接下发前端（对外一律走 `public_view()`）。
    """

    #: 模型厂商 id，取值见 app/llm/providers.py
    llm_provider: str = "deepseek"
    llm_api_key: str = ""
    #: 留空表示跟随所选厂商的预设地址
    llm_base_url: str = ""
    #: 留空表示跟随所选厂商的预设模型
    default_model: str = ""
    deepseek_api_key: str = ""
    deepseek_base_url: str = ""
    #: 搜索后端 id，取值见 app/search/providers.py（auto 表示内置免密钥后端依次尝试）
    search_provider: str = "auto"
    search_api_key: str = ""
    #: 留空表示跟随所选搜索后端的预设地址
    search_base_url: str = ""
    web_search_max_results: int = 5
    sandbox_global_enabled: bool = False
    max_steps: int = 10
    tool_timeout: float = 30.0
    #: 最近一次后台修改时间；从未修改过则为 None
    updated_at: Optional[datetime] = None


class RuntimeConfigService:
    """业务配置的读写入口（进程内单例，见 `get_runtime_config`）。"""

    def __init__(
        self,
        store: AbstractStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._cache: RuntimeConfig | None = None
        self._cached_at: float = 0.0
        self._cache_key: tuple[Any, ...] = ()
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- 依赖
    @property
    def store(self) -> AbstractStore:
        return self._store or get_store()

    @property
    def settings(self) -> Settings:
        return self._settings or get_settings()

    # ---------------------------------------------------------------- 读取
    def _defaults(self) -> RuntimeConfig:
        """环境变量侧的兜底值（后台未配置时生效）。"""
        settings = self.settings
        return RuntimeConfig(**{field: getattr(settings, field) for _, field in _CONFIG_FIELDS})

    def _fingerprint(self) -> tuple[Any, ...]:
        """环境变量侧的取值指纹，用于让缓存随 `Settings` 变化自动失效。"""
        settings = self.settings
        return tuple(getattr(settings, field) for _, field in _CONFIG_FIELDS)

    def _is_fresh(self, *, force: bool, ttl: float, key: tuple[Any, ...]) -> bool:
        return (
            not force
            and ttl > 0
            and self._cache is not None
            and self._cache_key == key
            and time.monotonic() - self._cached_at < ttl
        )

    async def load(self, *, force: bool = False) -> RuntimeConfig:
        """读取配置快照；带 TTL 缓存，`force=True` 时强制回源。"""
        ttl = self.settings.runtime_config_ttl
        key = self._fingerprint()
        if self._is_fresh(force=force, ttl=ttl, key=key):
            return self._cache  # type: ignore[return-value]

        async with self._lock:
            # 双检：并发请求只触发一次回源
            if self._is_fresh(force=force, ttl=ttl, key=key):
                return self._cache  # type: ignore[return-value]

            config = await self._load_from_store()
            self._cache = config
            self._cached_at = time.monotonic()
            self._cache_key = key
            return config

    async def _load_from_store(self) -> RuntimeConfig:
        """从仓储读取全部配置行，缺失项回落到环境变量。"""
        settings = self.settings
        fallback: dict[str, str] = {
            key: _serialize(getattr(settings, field)) for key, field in _CONFIG_FIELDS
        }

        rows: dict[str, ConfigItem] = {row.key: row for row in await self.store.list_configs()}
        data: dict[str, Any] = {}
        for key, default in fallback.items():
            row = rows.get(key)
            if row is None:
                data[key] = default
                continue
            value = decrypt_value(row.value) if row.encrypted else row.value
            # 解密失败按「未配置」处理，回落到环境变量
            data[key] = default if value is None else value

        stamps = [row.updated_at for row in rows.values() if row.updated_at]
        try:
            return RuntimeConfig(**data, updated_at=max(stamps) if stamps else None)
        except ValidationError as exc:
            logger.error("系统配置值不合法，已整体回落到环境变量默认值: %s", exc)
            return self._defaults()

    async def effective_settings(self) -> Settings:
        """把库中配置合并进 `Settings`，供 Agent / LLM 客户端直接复用。

        返回的是**副本**，不会污染 `get_settings()` 的进程级单例。
        """
        config = await self.load()
        return self.settings.model_copy(
            update={field: getattr(config, field) for _, field in _CONFIG_FIELDS}
        )

    async def public_view(self) -> dict[str, Any]:
        """供管理接口下发的脱敏视图。

        表单字段（`llm_provider` / `llm_base_url` / `default_model`）回显**库中存的值**，
        留空代表「跟随厂商预设」；另外给出 `effective_*`，让管理员看到真正生效的是什么。
        """
        config = await self.load()
        settings = await self.effective_settings()
        try:
            resolved = resolve_llm_config(settings)
        except LLMNotConfiguredError:
            # 例如刚切到「自定义」还没填地址：表单仍要能正常回显
            resolved = None
        # 与 resolve_llm_config 保持同一口径：旧键 deepseek_api_key 只在 deepseek 厂商下计入，
        # 否则界面会显示「已配置」而 /chat 仍报未配置，两者矛盾。
        provider_id = (settings.llm_provider or DEFAULT_PROVIDER_ID).strip().lower()
        using_legacy = provider_id == DEFAULT_PROVIDER_ID and not settings.llm_api_key
        api_key = settings.llm_api_key or (settings.deepseek_api_key if using_legacy else "")
        search_provider, search_label, search_base_url, search_chain = self._effective_search(settings)
        return {
            "llm_provider": config.llm_provider,
            "llm_api_key": mask_secret(api_key) if api_key else "",
            "llm_api_key_configured": bool(api_key),
            "llm_api_key_from_legacy": bool(using_legacy and settings.deepseek_api_key),
            "llm_base_url": config.llm_base_url,
            "default_model": config.default_model,
            "effective_provider": resolved.provider if resolved else config.llm_provider,
            "effective_provider_label": resolved.label if resolved else "",
            "effective_base_url": resolved.base_url if resolved else "",
            "effective_model": resolved.model if resolved else "",
            "search_provider": config.search_provider,
            "search_api_key": mask_secret(settings.search_api_key) if settings.search_api_key else "",
            "search_api_key_configured": bool(settings.search_api_key),
            "search_base_url": config.search_base_url,
            "web_search_max_results": config.web_search_max_results,
            "effective_search_provider": search_provider,
            "effective_search_label": search_label,
            "effective_search_base_url": search_base_url,
            "effective_search_chain": search_chain,
            "sandbox_global_enabled": config.sandbox_global_enabled,
            "max_steps": config.max_steps,
            "tool_timeout": config.tool_timeout,
            "updated_at": config.updated_at,
        }

    def _effective_search(self, settings: Settings) -> tuple[str, str, str, list[str]]:
        """搜索配置的生效结果：解析失败时返回空值，保证后台表单仍能正常回显。"""
        try:
            search = resolve_search_config(settings)
            chain = [backend.label for backend in build_search_chain(search)]
        except SearchError:
            return "", "", "", []
        return search.provider, search.label, search.base_url, chain

    # ---------------------------------------------------------------- 写入
    async def update(
        self, values: dict[str, Any], *, updated_by: str | None = None
    ) -> RuntimeConfig:
        """写入若干配置项，随后失效缓存。

        - 未知键直接忽略（由管理接口的请求模型先行校验）；
        - 敏感项自动加密后入库；
        - 空值代表「清除该配置」，删除对应行以便回落到环境变量。
        """
        for key, value in values.items():
            if key not in SUPPORTED_KEYS or value is None:
                continue

            text = _serialize(value)
            if text == "":
                await self.store.delete_config(key)
                continue

            secret = key in SECRET_KEYS
            await self.store.set_config(
                key,
                encrypt_value(text) if secret else text,
                encrypted=secret,
                updated_by=updated_by,
            )

        self.invalidate()
        return await self.load(force=True)

    def invalidate(self) -> None:
        """丢弃缓存，使下一次读取回源（配置变更后调用）。"""
        self._cache = None
        self._cached_at = 0.0
        self._cache_key = ()


def _serialize(value: Any) -> str:
    """把配置值统一序列化为字符串存储。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# ---------------------------------------------------------------- 单例
_service: RuntimeConfigService | None = None


def get_runtime_config() -> RuntimeConfigService:
    """进程内单例（与仓储单例同生命周期）。"""
    global _service
    if _service is None:
        _service = RuntimeConfigService()
    return _service


def reset_runtime_config() -> None:
    """清空单例（测试或切换存储后端时使用）。"""
    global _service
    _service = None


__all__ = [
    "KEY_DEFAULT_MODEL",
    "KEY_DEEPSEEK_API_KEY",
    "KEY_DEEPSEEK_BASE_URL",
    "KEY_LLM_API_KEY",
    "KEY_LLM_BASE_URL",
    "KEY_LLM_PROVIDER",
    "KEY_MAX_STEPS",
    "KEY_SANDBOX_GLOBAL_ENABLED",
    "KEY_SEARCH_API_KEY",
    "KEY_SEARCH_BASE_URL",
    "KEY_SEARCH_MAX_RESULTS",
    "KEY_SEARCH_PROVIDER",
    "KEY_TOOL_TIMEOUT",
    "SECRET_KEYS",
    "SUPPORTED_KEYS",
    "RuntimeConfig",
    "RuntimeConfigService",
    "get_runtime_config",
    "reset_runtime_config",
]
