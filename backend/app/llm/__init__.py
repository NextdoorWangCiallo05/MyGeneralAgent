"""LLM 接入层：统一接口 + OpenAI 兼容协议实现 + 厂商预设。

换一家模型时，改动集中在 `app/llm/providers.py`：
- 新增云端厂商：往 `PRESETS` 加一条；
- 本地部署 / 企业网关：用 `ollama` 或 `custom` 预设，在后台填服务地址与模型名。
"""

from app.llm.client import (
    ChatChunk,
    ChunkKind,
    LLMClient,
    LLMNotConfiguredError,
    LLMRequestError,
)
from app.llm.openai_compat import OpenAICompatClient
from app.llm.providers import (
    DEFAULT_PROVIDER_ID,
    PRESETS,
    LLMConfig,
    ProviderPreset,
    build_llm_client,
    get_preset,
    list_presets,
    provider_not_found_message,
    resolve_llm_config,
)

__all__ = [
    "ChatChunk",
    "ChunkKind",
    "DEFAULT_PROVIDER_ID",
    "LLMClient",
    "LLMConfig",
    "LLMNotConfiguredError",
    "LLMRequestError",
    "OpenAICompatClient",
    "PRESETS",
    "ProviderPreset",
    "build_llm_client",
    "get_preset",
    "list_presets",
    "provider_not_found_message",
    "resolve_llm_config",
]
