"""厂商预设与接入配置解析。

「可配置其他家」在这里被定义成一张表：新增一家云端服务，通常只需在 `PRESETS`
里加一条；本地部署或企业网关则用 `ollama` / `custom`，由管理员在后台直接填
服务地址与模型名。

配置来源优先级：后台运行时配置（system_config 表） > 环境变量 / .env > 厂商预设。
解析统一走 `resolve_llm_config()`，因此 `deepseek_*` 这类旧键仍然有效。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.llm.client import LLMNotConfiguredError
from app.llm.openai_compat import OpenAICompatClient

if TYPE_CHECKING:  # 仅类型检查期需要，避免与配置层互相导入
    from app.config import Settings
    from app.llm.client import LLMClient


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """一家 OpenAI 兼容服务的最小描述。"""

    id: str
    label: str
    #: 兼容端点前缀，客户端会拼上 `/chat/completions`
    base_url: str
    #: 供后台下拉建议的模型名，管理员可以填表外的任意值
    models: tuple[str, ...]
    default_model: str
    #: 本地部署通常不校验密钥
    requires_api_key: bool = True
    #: 少数端点不接受 stream_options.include_usage
    stream_usage: bool = True
    hint: str = ""


PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com",
        models=("deepseek-chat", "deepseek-reasoner"),
        default_model="deepseek-chat",
        hint="官方端点不加 /v1；经由第三方平台转售时请按平台的地址填写。",
    ),
    ProviderPreset(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        models=("gpt-4o-mini", "gpt-4o", "gpt-4.1", "gpt-4.1-mini", "o4-mini"),
        default_model="gpt-4o-mini",
        hint="国内网络通常需要自备代理，代理地址填在服务地址里。",
    ),
    ProviderPreset(
        id="moonshot",
        label="月之暗面 Kimi",
        base_url="https://api.moonshot.cn/v1",
        models=("moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k", "kimi-k2-0711-preview"),
        default_model="moonshot-v1-8k",
    ),
    ProviderPreset(
        id="dashscope",
        label="阿里云百炼（通义千问）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        models=("qwen-plus", "qwen-max", "qwen-turbo", "qwen2.5-72b-instruct"),
        default_model="qwen-plus",
        hint="使用「兼容模式」地址，密钥为百炼的 DashScope API-Key。",
    ),
    ProviderPreset(
        id="zhipu",
        label="智谱 AI（GLM）",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        models=("glm-4-flash", "glm-4-air", "glm-4-plus"),
        default_model="glm-4-flash",
        hint="服务地址已含 /v4，密钥为开放平台的 API-Key。",
    ),
    ProviderPreset(
        id="ollama",
        label="本地 Ollama",
        base_url="http://localhost:11434/v1",
        models=("qwen2.5:7b", "llama3.1:8b"),
        default_model="qwen2.5:7b",
        requires_api_key=False,
        stream_usage=False,
        hint="模型名要写成 Ollama 里的完整名字（如 qwen2.5:7b）。",
    ),
    ProviderPreset(
        id="custom",
        label="自定义（代理 / 私有部署）",
        base_url="",
        models=(),
        default_model="",
        stream_usage=False,
        hint="任意 OpenAI 兼容端点：One-API、vLLM、企业网关等，需自行填写服务地址与模型名。",
    ),
)

_BY_ID: dict[str, ProviderPreset] = {preset.id: preset for preset in PRESETS}

#: 兼容旧配置：未显式指定厂商时沿用 DeepSeek
DEFAULT_PROVIDER_ID = "deepseek"


def list_presets() -> tuple[ProviderPreset, ...]:
    return PRESETS


def get_preset(provider_id: str) -> ProviderPreset | None:
    return _BY_ID.get((provider_id or "").strip().lower())


def provider_not_found_message(provider_id: str) -> str:
    return f"未知的模型厂商 {provider_id!r}，可选：" + "、".join(_BY_ID)


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """一次对话真正使用的接入参数（含明文密钥，只在进程内流转）。"""

    provider: str
    label: str
    base_url: str
    model: str
    api_key: str
    timeout: float
    requires_api_key: bool
    stream_usage: bool


def resolve_llm_config(settings: Settings) -> LLMConfig:
    """把多层配置解析成一份确定的接入参数。

    旧键兼容：`deepseek_api_key` / `deepseek_base_url` 在厂商为 deepseek 时等价于
    `llm_api_key` / `llm_base_url`，因此升级前的 .env 不需要改动。
    """
    provider_id = (settings.llm_provider or DEFAULT_PROVIDER_ID).strip().lower()
    preset = get_preset(provider_id)
    if preset is None:
        raise LLMNotConfiguredError(provider_not_found_message(settings.llm_provider))

    is_deepseek = provider_id == DEFAULT_PROVIDER_ID
    legacy_key = settings.deepseek_api_key if is_deepseek else ""
    legacy_base_url = settings.deepseek_base_url if is_deepseek else ""

    base_url = (settings.llm_base_url or legacy_base_url or preset.base_url).rstrip("/")
    if not base_url:
        raise LLMNotConfiguredError(f"{preset.label} 的服务地址（base_url）未配置")

    model = settings.default_model or preset.default_model
    if not model:
        raise LLMNotConfiguredError(f"{preset.label} 未指定模型名")

    return LLMConfig(
        provider=provider_id,
        label=preset.label,
        base_url=base_url,
        model=model,
        api_key=settings.llm_api_key or legacy_key,
        timeout=settings.llm_timeout,
        requires_api_key=preset.requires_api_key,
        stream_usage=preset.stream_usage,
    )


def build_llm_client(config: LLMConfig) -> LLMClient:
    """按解析结果构造客户端：一份实现，接任意家。"""
    return OpenAICompatClient(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        timeout=config.timeout,
        label=config.label,
        requires_api_key=config.requires_api_key,
        stream_usage=config.stream_usage,
    )
