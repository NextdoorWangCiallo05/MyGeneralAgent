"""应用配置：基础设施配置从环境变量 / .env 读取。

模型的接入参数（厂商、密钥、服务地址、默认模型）在运行时可由数据库 system_config
表覆盖，见 app/core/runtime_config.py；最终如何解析成一次调用见 app/llm/providers.py。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 应用
    app_name: str = "Agent App API"
    api_prefix: str = "/api/v1"

    # 存储后端：memory（进程内，便于本地开发/测试） | sql（MySQL，持久化）
    store_backend: str = "memory"
    # 形如 mysql+aiomysql://user:password@127.0.0.1:3306/agent_app?charset=utf8mb4
    database_url: str = ""
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle: int = 3600
    # 启动时自动建表（开发便利）；生产请用 `alembic upgrade head`
    auto_create_tables: bool = False

    @model_validator(mode="after")
    def _check_store_backend(self) -> "Settings":
        backend = self.store_backend.strip().lower()
        if backend not in {"memory", "sql"}:
            raise ValueError("STORE_BACKEND 只能是 memory 或 sql")
        if backend == "sql" and not self.database_url:
            raise ValueError("STORE_BACKEND=sql 时必须配置 DATABASE_URL")
        return self

    # ---- LLM：OpenAI 兼容厂商，可选项见 app/llm/providers.py 的 PRESETS ----
    # 厂商 id：deepseek | openai | moonshot | dashscope | zhipu | ollama | custom
    llm_provider: str = "deepseek"
    # 通用接入参数：留空则回落到所选厂商的预设
    llm_api_key: str = ""
    llm_base_url: str = ""
    # 默认模型：留空表示用所选厂商预设的默认模型
    default_model: str = ""
    llm_timeout: float = 60.0
    # 旧键（保留兼容）：厂商为 deepseek 时等价于 llm_api_key / llm_base_url
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    max_steps: int = 10
    tool_timeout: float = 30.0

    # ---- M3：工具沙箱（文件 / 代码类工具的隔离执行环境）----
    # 沙箱根目录，每个用户一个子目录（相对路径以进程工作目录为基准）
    sandbox_root: str = ".sandbox"
    # 沙箱总闸：管理员可在后台覆盖。默认关闭（fail-closed），
    # 需全局开启后会话级开关才会生效，见《技术设计文档》4.8。
    sandbox_global_enabled: bool = False
    # 沙箱内子进程的执行超时（秒）、虚拟内存上限（MB，0 表示不限制）
    sandbox_exec_timeout: float = 10.0
    sandbox_memory_mb: int = 256
    # 子进程单路输出上限、单文件读写上限（字节）
    sandbox_max_output_bytes: int = 65_536
    sandbox_max_file_bytes: int = 262_144

    # ---- M3：联网工具与人工确认 ----
    http_timeout: float = 20.0
    # 网页抓取的单次响应上限（字节）与搜索返回条数
    fetch_max_bytes: int = 1_048_576
    web_search_max_results: int = 5

    # ---- 联网搜索（见 app/search/）----
    # 搜索后端：auto | bing | duckduckgo | tavily | serper | brave | searxng
    search_provider: str = "auto"
    search_api_key: str = ""
    # 留空则使用所选后端的预设地址（自建 SearXNG 必须填实例地址）
    search_base_url: str = ""
    # 结果语言偏好（Bing 的 mkt / setlang 参数）
    search_locale: str = "zh-CN"
    # 单个搜索后端的请求超时（秒）；「自动」模式会依次尝试多个后端
    search_timeout: float = 12.0
    # 需确认的工具等待用户操作的最长时间（秒），超时按拒绝处理
    tool_confirm_timeout: float = 120.0

    # ---- M4：上下文压缩（FR-6，见 app/core/summary.py）----
    # 送入模型的历史 token 估算值超过该阈值时，把较早轮次摘要成一段文字
    summary_trigger_tokens: int = 6000
    # 压缩时保留最近多少轮完整对话（一轮 = 一条 user 消息及其后续消息）
    summary_keep_turns: int = 6
    # 新增待摘要内容不足该 token 数时先不调用模型，避免每轮都产生摘要开销
    summary_min_new_tokens: int = 400

    # ---- 对话附件（见 app/api/attachments.py）----
    # 单个附件的大小上限（字节）
    attachment_max_file_bytes: int = 5 * 1024 * 1024
    # 允许作为附件上传的扩展名（逗号分隔）；解析复用知识库的文档解析器
    attachment_allowed_types: str = "txt,md,markdown,log,csv,json,pdf"
    # 单条消息可携带的附件数量上限
    attachment_max_per_message: int = 5
    # 单个附件注入模型上下文的字符上限（超出部分截断并提示）。
    # 默认 10 万字符 ≈ 6~10 万 token，单个超大附件就可能占满 64k 上下文窗口，
    # 因此带超大文件时建议同时调小 ATTACHMENT_MAX_PER_MESSAGE，或改用知识库检索。
    attachment_max_chars: int = 100_000
    # 整个会话的附件注入总预算（字符）：新上传的文件优先整体展开，放不下的只列清单，
    # 模型可用 read_attachment 工具按段读取。附件是会话级资源，每一轮都会重新注入。
    attachment_context_max_chars: int = 100_000
    # 上传后未随消息发出的附件保留时长（秒），超时自动清理
    attachment_ttl_seconds: int = 24 * 60 * 60

    # ---- M4：知识库（RAG，见 app/api/knowledge.py）----
    # 单个文档的大小上限（字节）
    knowledge_max_file_bytes: int = 5 * 1024 * 1024
    # 文本分块的目标长度与相邻块重叠长度（字符），见 app/core/chunking.py
    knowledge_chunk_size: int = 800
    knowledge_chunk_overlap: int = 100
    # 允许上传的扩展名（逗号分隔）
    knowledge_allowed_types: str = "txt,md,markdown,pdf"

    # ---- M4：向量化与检索（见 app/core/embedding.py、app/core/retriever.py）----
    # 嵌入后端：hash（零依赖，字符 n-gram 匹配）/ local（sentence-transformers 本地模型）/ none（关闭）
    embedding_backend: str = "hash"
    # local 模式使用的模型（首次运行会从 HuggingFace 下载）
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    # 向量维度：hash 模式的哈希空间大小；local 模式为期望维度（以模型实际输出为准）
    embedding_dim: int = 512
    # 检索：默认 / 最大返回片段数
    knowledge_search_top_k: int = 5
    knowledge_search_top_k_max: int = 20
    # 单次检索最多参与打分的文本块数（控制延迟，按文档内顺序取前 N 条）
    knowledge_search_max_chunks: int = 2000
    # 低于该相似度的片段直接丢弃
    knowledge_search_min_score: float = 0.05

    # ---- M5：限流（见 app/core/ratelimit.py）----
    # 总开关；当前是单进程内滑动窗口，多实例部署需换成 Redis 计数器（见 README「已知简化」）
    rate_limit_enabled: bool = True
    # 配额窗口（秒）
    rate_limit_window_seconds: int = 60
    # 每个 IP 在窗口内的总请求数（所有接口的兜底，防单 IP 刷接口）
    rate_limit_ip_per_minute: int = 600
    # 每个 IP 在窗口内的登录 / 注册 / 忘记密码 / 重置密码次数（防撞库、防邮件轰炸、防枚举）
    rate_limit_auth_per_minute: int = 20
    # 每个登录用户在窗口内的对话次数（模型调用是主要成本项）
    rate_limit_chat_per_minute: int = 30
    # 每个登录用户在窗口内的上传 / 重建索引次数（解析与向量化有明显 CPU 开销）
    rate_limit_upload_per_minute: int = 20
    # 部署在可信反向代理后时，用 X-Forwarded-For 首段作为客户端 IP
    rate_limit_trust_proxy: bool = False

    # ---- M5：可观测性（见 app/core/logging.py、app/core/tracing.py）----
    # 日志格式：text（开发可读）/ json（生产，单行 JSON，便于采集）
    log_format: str = "text"
    log_level: str = "INFO"
    # Langfuse 全链路追踪：公钥 + 私钥都配置且装好 SDK 时自动启用，否则静默降级为 no-op
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    # 未接 Langfuse 时，是否把链路埋点写成结构化日志（本地排查 / 验收用）
    observability_log_spans: bool = False

    # 鉴权
    # 开发默认值；生产必须通过环境变量覆盖为 >=32 字节的随机串
    jwt_secret: str = "dev-only-insecure-secret-change-me-please"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24

    # 敏感业务配置（system_config 中的 API-Key 等）的对称加密密钥
    # 开发默认值；生产必须覆盖，且一旦更换，库中既有密文将无法解密
    encryption_key: str = "dev-only-insecure-encryption-key-change-me"
    # 业务配置进程内缓存时长（秒），0 表示不缓存
    runtime_config_ttl: float = 15.0

    # 初始管理员（启动时按需创建）
    admin_username: str = "admin"
    admin_email: str = ""
    admin_password: str = ""

    # 找回密码
    reset_token_expire_minutes: int = 30
    app_base_url: str = "http://localhost:3000"

    # 邮件
    email_backend: str = "console"  # console | smtp
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "no-reply@example.com"
    smtp_tls: bool = True

    # CORS
    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def knowledge_allowed_type_list(self) -> list[str]:
        return [item.strip().lower() for item in self.knowledge_allowed_types.split(",") if item.strip()]

    @property
    def attachment_allowed_type_list(self) -> list[str]:
        return [item.strip().lower() for item in self.attachment_allowed_types.split(",") if item.strip()]

    @property
    def embedding_backend_name(self) -> str:
        """归一化后的嵌入后端名（hash / local / none）。"""
        return (self.embedding_backend or "").strip().lower()

    @property
    def langfuse_enabled(self) -> bool:
        """是否具备启用 Langfuse 的配置（SDK 是否可用由 app.core.tracing 判断）。"""
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache
def get_settings() -> Settings:
    """进程内单例配置。"""
    return Settings()
