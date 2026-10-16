"""管理员接口的请求 / 响应模型（对应《技术设计文档》7.2 节）。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.llm.providers import get_preset, provider_not_found_message
from app.schemas.auth import UserPublic
from app.search.providers import (
    get_preset as get_search_preset,
    provider_not_found_message as search_provider_not_found_message,
)


class AdminConfigPublic(BaseModel):
    """系统配置的对外视图。

    `llm_api_key` 恒为脱敏值（如 `sk-1****cdef`），真实密钥不会以任何形式离开服务端。
    表单字段（`llm_provider` / `llm_base_url` / `default_model`）回显库中存的值，
    留空代表跟随厂商预设；`effective_*` 是最终真正生效的参数。
    """

    #: 厂商 id，可选值来自 app/llm/providers.py
    llm_provider: str = ""
    llm_api_key: str = ""
    #: 是否已配置可用密钥，供前端展示「已配置 / 未配置」状态
    llm_api_key_configured: bool = False
    #: 密钥来自旧版 deepseek_api_key（前端据此提示迁移）
    llm_api_key_from_legacy: bool = False
    llm_base_url: str = ""
    default_model: str = ""
    effective_provider: str = ""
    effective_provider_label: str = ""
    effective_base_url: str = ""
    effective_model: str = ""
    #: 搜索后端 id，可选值来自 app/search/providers.py
    search_provider: str = ""
    search_api_key: str = ""
    search_api_key_configured: bool = False
    #: 留空表示跟随所选后端的预设地址（自建 SearXNG 必填）
    search_base_url: str = ""
    web_search_max_results: int = 5
    effective_search_provider: str = ""
    effective_search_label: str = ""
    effective_search_base_url: str = ""
    #: 实际会依次尝试的后端名（「自动」模式下多于一个）
    effective_search_chain: list[str] = Field(default_factory=list)
    sandbox_global_enabled: bool = False
    max_steps: int = 10
    tool_timeout: float = 30.0
    updated_at: Optional[datetime] = None


class AdminConfigUpdate(BaseModel):
    """更新系统配置。

    只提交需要变更的字段，未出现的字段保持原值（依赖 `exclude_unset`）。
    传空字符串表示**清除**该配置，随后回落到环境变量或厂商预设。

    `deepseek_api_key` / `deepseek_base_url` 为旧键，厂商为 deepseek 时仍生效，
    新配置请用 `llm_api_key` / `llm_base_url`。
    """

    llm_provider: Optional[str] = Field(default=None, max_length=32)
    llm_api_key: Optional[str] = Field(default=None, max_length=512)
    llm_base_url: Optional[str] = Field(default=None, max_length=512)
    default_model: Optional[str] = Field(default=None, max_length=128)
    deepseek_api_key: Optional[str] = Field(default=None, max_length=512)
    deepseek_base_url: Optional[str] = Field(default=None, max_length=512)
    max_steps: Optional[int] = Field(default=None, ge=1, le=50)
    tool_timeout: Optional[float] = Field(default=None, gt=0, le=300)
    # 联网搜索：留空表示清除（回落环境变量，再不成才用预设）
    search_provider: Optional[str] = Field(default=None, max_length=32)
    search_api_key: Optional[str] = Field(default=None, max_length=512)
    search_base_url: Optional[str] = Field(default=None, max_length=512)
    web_search_max_results: Optional[int] = Field(default=None, ge=1, le=20)

    @field_validator("llm_provider")
    @classmethod
    def _validate_provider(cls, value: Optional[str]) -> Optional[str]:
        if not value:
            return value
        provider_id = value.strip().lower()
        if get_preset(provider_id) is None:
            raise ValueError(provider_not_found_message(value))
        return provider_id

    @field_validator("llm_base_url", "deepseek_base_url")
    @classmethod
    def _validate_base_url(cls, value: Optional[str]) -> Optional[str]:
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("服务地址必须以 http:// 或 https:// 开头")
        return value.rstrip("/") if value else value

    @field_validator("search_provider")
    @classmethod
    def _validate_search_provider(cls, value: Optional[str]) -> Optional[str]:
        if not value:
            return value
        provider_id = value.strip().lower()
        if get_search_preset(provider_id) is None:
            raise ValueError(search_provider_not_found_message(value))
        return provider_id

    @field_validator("search_base_url")
    @classmethod
    def _validate_search_base_url(cls, value: Optional[str]) -> Optional[str]:
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("搜索服务地址必须以 http:// 或 https:// 开头")
        return value.rstrip("/") if value else value


class LLMProviderPublic(BaseModel):
    """一家可选厂商的预设，供后台渲染下拉与模型名建议。"""

    id: str
    label: str
    base_url: str
    models: list[str] = Field(default_factory=list)
    default_model: str = ""
    requires_api_key: bool = True
    hint: str = ""


class LLMProviderListResponse(BaseModel):
    """厂商预设列表。`active` 为当前生效的厂商 id。"""

    total: int
    items: list[LLMProviderPublic]
    active: str


class SearchProviderPublic(BaseModel):
    """一种可选搜索后端的预设。"""

    id: str
    label: str
    base_url: str = ""
    requires_api_key: bool = True
    key_hint: str = ""
    hint: str = ""
    #: 是否参与「自动」模式的依次尝试
    auto: bool = False


class SearchProviderListResponse(BaseModel):
    """搜索后端预设列表。`active` 为当前生效的后端 id。"""

    total: int
    items: list[SearchProviderPublic]
    active: str


class SearchTestRequest(BaseModel):
    """搜索连通性测试：只做一次真实检索，不落库、不影响会话。"""

    query: str = Field(default="OpenAI", max_length=200)
    #: 临时指定后端进行探测（留空表示按当前配置）
    provider: Optional[str] = Field(default=None, max_length=32)


class SearchProbeResult(BaseModel):
    """单个后端的探测结果。"""

    provider: str
    label: str
    ok: bool
    #: 成功时为「返回 N 条」，失败时为中文原因
    detail: str = ""
    took_ms: int = 0
    #: 前几条标题，便于确认结果是否对路
    sample: list[str] = Field(default_factory=list)


class SearchTestResponse(BaseModel):
    """探测汇总：`ok` 为任一后端可用。"""

    query: str
    provider: str
    ok: bool
    results: list[SearchProbeResult] = Field(default_factory=list)
    message: str = ""


class SandboxToggleRequest(BaseModel):
    """沙箱全局开关。"""

    enabled: bool


class AdminUserListResponse(BaseModel):
    """用户列表（M2 暂不分页，用户量增长后再补）。"""

    total: int
    items: list[UserPublic]


class AuditLogPublic(BaseModel):
    """工具调用审计记录（M5）。`args` / `result` 为脱敏截断后的 JSON 文本。"""

    id: str
    user_id: str
    session_id: str
    tool_name: str
    args: str = "{}"
    result: str = "{}"
    status: str = "success"
    duration_ms: int = 0
    approved: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditLogListResponse(BaseModel):
    """审计记录分页视图。"""

    total: int
    items: list[AuditLogPublic]


__all__ = [
    "AdminConfigPublic",
    "AdminConfigUpdate",
    "AdminUserListResponse",
    "AuditLogListResponse",
    "AuditLogPublic",
    "LLMProviderListResponse",
    "LLMProviderPublic",
    "SandboxToggleRequest",
    "SearchProbeResult",
    "SearchProviderListResponse",
    "SearchProviderPublic",
    "SearchTestRequest",
    "SearchTestResponse",
]
