"""pytest 全局配置：把测试与开发者本机的 `.env` 解耦。

本机 `.env` 可能指向真实 MySQL（STORE_BACKEND=sql、DATABASE_URL=mysql+aiomysql://...）。
测试必须可重复、不依赖外部服务，也不能污染真实数据，因此这里在收集阶段就
强制默认使用内存后端；确需 SQL 的用例再通过各自 fixture 显式覆盖为临时库。

环境变量优先级高于 .env 文件，故此处赋值可以稳定压过本机配置。
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

import pytest

# ---- 存储：默认锁定内存后端（需要 SQL 的用例自行 monkeypatch 覆盖）----
os.environ["STORE_BACKEND"] = "memory"
os.environ["DATABASE_URL"] = ""
os.environ["AUTO_CREATE_TABLES"] = "false"

# ---- 沙箱（M3）：落到系统临时目录，既不污染工作区也保证用例之间互不干扰 ----
os.environ["SANDBOX_ROOT"] = tempfile.mkdtemp(prefix="agent-sandbox-")
# 默认关闭，需要沙箱的用例显式开启（与生产 fail-closed 语义一致）
os.environ["SANDBOX_GLOBAL_ENABLED"] = "false"

# ---- 向量化（M4）：锁定零依赖的 hash 后端，避免测试依赖本机 .env 的 local 模型 ----
os.environ["EMBEDDING_BACKEND"] = "hash"

# ---- 外部依赖：清空真实凭据，避免测试发起真实网络调用或创建初始管理员 ----
os.environ["DEEPSEEK_API_KEY"] = ""  # 旧键：仅在厂商为 deepseek 时兼容生效
os.environ["LLM_PROVIDER"] = "deepseek"  # 固定默认厂商，避免随本机 .env 漂移
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_BASE_URL"] = ""
os.environ["DEFAULT_MODEL"] = ""
os.environ["ADMIN_EMAIL"] = ""
os.environ["ADMIN_PASSWORD"] = ""
# 联网搜索：固定为免密钥的默认后端，避免随本机 .env 漂移或触发真实请求
os.environ["SEARCH_PROVIDER"] = "auto"
os.environ["SEARCH_API_KEY"] = ""
os.environ["SEARCH_BASE_URL"] = ""
os.environ["EMAIL_BACKEND"] = "console"
os.environ["SMTP_HOST"] = ""


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> Iterator[None]:
    """限流器是进程内全局状态，逐用例重置，避免计数跨用例累积导致误判 429。"""
    from app.core.ratelimit import reset_rate_limiter

    reset_rate_limiter()
    yield
    reset_rate_limiter()
