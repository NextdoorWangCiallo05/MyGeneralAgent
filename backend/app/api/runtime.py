"""运行时配置接口：向已登录用户暴露**非敏感**的运行开关。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, Store  # noqa: F401 - 统一鉴权依赖
from app.core.embedding import get_embedder
from app.core.runtime_config import get_runtime_config
from app.schemas.runtime import RuntimeConfigPublic
from app.tools.registry import build_default_registry

router = APIRouter(tags=["runtime"])

_registry = build_default_registry()

_sandbox_tools = [
    name
    for name in _registry.available_names(sandbox_enabled=True)
    if name in {"read_file", "write_file", "python_exec"}
]


@router.get("/config", response_model=RuntimeConfigPublic, summary="读取运行时开关")
async def read_config(_: CurrentUser) -> RuntimeConfigPublic:
    """返回前端渲染所需的最小配置，绝不下发密钥等敏感项。"""
    config = await get_runtime_config().load()
    embedder = get_embedder()
    return RuntimeConfigPublic(
        sandbox_global_enabled=config.sandbox_global_enabled,
        sandbox_tools=_sandbox_tools,
        knowledge_enabled=embedder is not None,
        knowledge_backend=embedder.name if embedder else "none",
        knowledge_tool="knowledge_search" if embedder else None,
    )
