"""管理员接口：系统配置与用户管理（对应《技术设计文档》7.2 节）。

全部端点要求 `role == "admin"`。配置中的敏感项（模型 API-Key）
读取时脱敏、写入时加密，明文不会离开服务端进程。
"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Query

from app.api.deps import AdminUser, Store
from app.core.runtime_config import KEY_SANDBOX_GLOBAL_ENABLED, get_runtime_config
from app.llm.providers import DEFAULT_PROVIDER_ID, list_presets
from app.schemas.admin import (
    AdminConfigPublic,
    AdminConfigUpdate,
    AdminUserListResponse,
    AuditLogListResponse,
    AuditLogPublic,
    LLMProviderListResponse,
    LLMProviderPublic,
    SandboxToggleRequest,
    SearchProbeResult,
    SearchProviderListResponse,
    SearchProviderPublic,
    SearchTestRequest,
    SearchTestResponse,
)
from app.schemas.auth import UserPublic
from app.search import (
    DEFAULT_PROVIDER_ID as DEFAULT_SEARCH_PROVIDER_ID,
    SearchError,
    SearchNotConfiguredError,
    build_search_chain,
    list_presets as list_search_presets,
    resolve_search_config,
)

router = APIRouter(prefix="/admin", tags=["admin"])


async def _public_config() -> AdminConfigPublic:
    """当前配置的脱敏视图。"""
    return AdminConfigPublic(**await get_runtime_config().public_view())


@router.get("/config", response_model=AdminConfigPublic, summary="读取系统配置")
async def read_config(admin: AdminUser) -> AdminConfigPublic:
    """返回当前生效的业务配置，API-Key 以 `sk-1****cdef` 形式脱敏。"""
    return await _public_config()


@router.put("/config", response_model=AdminConfigPublic, summary="更新系统配置")
async def update_config(payload: AdminConfigUpdate, admin: AdminUser) -> AdminConfigPublic:
    """仅更新请求体中显式提交的字段，其余保持不变。"""
    changes = payload.model_dump(exclude_unset=True)
    if changes:
        await get_runtime_config().update(changes, updated_by=admin.id)
    return await _public_config()


@router.get(
    "/llm/providers",
    response_model=LLMProviderListResponse,
    summary="可选模型厂商",
)
async def list_llm_providers(admin: AdminUser) -> LLMProviderListResponse:
    """内置的 OpenAI 兼容厂商预设，供后台选择厂商并联动服务地址与模型建议。"""
    config = await get_runtime_config().load()
    items = [
        LLMProviderPublic(
            id=preset.id,
            label=preset.label,
            base_url=preset.base_url,
            models=list(preset.models),
            default_model=preset.default_model,
            requires_api_key=preset.requires_api_key,
            hint=preset.hint,
        )
        for preset in list_presets()
    ]
    return LLMProviderListResponse(
        total=len(items),
        items=items,
        active=(config.llm_provider or DEFAULT_PROVIDER_ID).strip().lower(),
    )


def _elapsed_ms(started: float) -> int:
    """从 `time.perf_counter()` 起点算起的毫秒数。"""
    return int((time.perf_counter() - started) * 1000)


@router.get(
    "/search/providers",
    response_model=SearchProviderListResponse,
    summary="可选搜索后端",
)
async def list_search_providers(admin: AdminUser) -> SearchProviderListResponse:
    """内置的搜索后端预设，供后台选择后端并联动密钥 / 地址 / 提示。"""
    config = await get_runtime_config().load()
    items = [
        SearchProviderPublic(
            id=preset.id,
            label=preset.label,
            base_url=preset.base_url,
            requires_api_key=preset.requires_api_key,
            key_hint=preset.key_hint,
            hint=preset.hint,
            auto=preset.auto_order > 0,
        )
        for preset in list_search_presets()
    ]
    return SearchProviderListResponse(
        total=len(items),
        items=items,
        active=(config.search_provider or DEFAULT_SEARCH_PROVIDER_ID).strip().lower(),
    )


@router.post(
    "/search/test",
    response_model=SearchTestResponse,
    summary="测试搜索后端连通性",
)
async def test_search(payload: SearchTestRequest, admin: AdminUser) -> SearchTestResponse:
    """用真实请求依次探测后端，直接告诉管理员当前网络下哪个可用。

    只做一次检索、不落库；`provider` 留空表示按当前配置探测（自动模式会探测整条链）。
    """
    settings = await get_runtime_config().effective_settings()
    if payload.provider:
        settings = settings.model_copy(
            update={"search_provider": payload.provider.strip().lower()}
        )
    query = (payload.query or "OpenAI").strip() or "OpenAI"

    try:
        search_config = resolve_search_config(settings)
        backends = build_search_chain(search_config)
    except SearchNotConfiguredError as exc:
        return SearchTestResponse(query=query, provider="", ok=False, message=str(exc))

    probes: list[SearchProbeResult] = []
    for backend in backends:
        started = time.perf_counter()
        try:
            found = await backend.search(query, limit=3)
        except SearchError as exc:
            probes.append(
                SearchProbeResult(
                    provider=backend.id,
                    label=backend.label,
                    ok=False,
                    detail=str(exc),
                    took_ms=_elapsed_ms(started),
                )
            )
            continue
        probes.append(
            SearchProbeResult(
                provider=backend.id,
                label=backend.label,
                ok=True,
                detail=f"返回 {len(found)} 条",
                took_ms=_elapsed_ms(started),
                sample=[item.title for item in found[:3]],
            )
        )
    return SearchTestResponse(
        query=query,
        provider=search_config.provider,
        ok=any(item.ok for item in probes),
        results=probes,
    )


@router.put("/sandbox", response_model=AdminConfigPublic, summary="设置沙箱全局开关")
async def toggle_sandbox(
    payload: SandboxToggleRequest, admin: AdminUser
) -> AdminConfigPublic:
    """全局关停沙箱：关闭后所有会话都不得执行用户代码。"""
    await get_runtime_config().update(
        {KEY_SANDBOX_GLOBAL_ENABLED: payload.enabled},
        updated_by=admin.id,
    )
    return await _public_config()


@router.get("/users", response_model=AdminUserListResponse, summary="用户列表")
async def list_users(admin: AdminUser, store: Store) -> AdminUserListResponse:
    """平台用户一览（M2 暂不分页）。"""
    users = await store.list_users()
    return AdminUserListResponse(
        total=len(users),
        items=[UserPublic.model_validate(user) for user in users],
    )


@router.get(
    "/audit-logs",
    response_model=AuditLogListResponse,
    summary="工具调用审计日志",
)
async def list_audit_logs(
    admin: AdminUser,
    store: Store,
    user_id: Optional[str] = Query(default=None, description="按用户过滤，缺省查看全部"),
    session_id: Optional[str] = Query(default=None, description="按会话过滤"),
    tool_name: Optional[str] = Query(default=None, description="按工具名过滤"),
    status: Optional[str] = Query(default=None, description="按状态过滤：success/error/timeout/rejected/blocked"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AuditLogListResponse:
    """按时间倒序查询工具调用审计记录，用于事后追责与排障（M5）。"""
    logs = await store.list_audit_logs(
        user_id,
        session_id=session_id,
        tool_name=tool_name,
        status=status,
        limit=limit,
        offset=offset,
    )
    total = await store.count_audit_logs(
        user_id, session_id=session_id, tool_name=tool_name, status=status
    )
    return AuditLogListResponse(
        total=total,
        items=[AuditLogPublic.model_validate(log) for log in logs],
    )
