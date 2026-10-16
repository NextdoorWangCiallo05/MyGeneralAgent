# Agent App

一个支持**多用户注册 / 登录 / 找回密码**、**DeepSeek 大模型**、**工具调用（ReAct）**与 **SSE 流式输出**的智能助手应用。

当前进度：**M1 骨架、M2 数据持久化与管理员后台、M3 工具扩充 / 沙箱隔离 / 人工确认均已完成**；
**M4 已完成上下文摘要压缩（FR-6）与知识库「上传 → 解析 → 分块 → 向量化 → 检索」全链路**
（含 `knowledge_search` 工具、重建索引与检索调试接口）；
**M5 已完成可观测性与限流**：结构化日志（`request_id` 贯穿全链路）、按 IP / 认证 / 对话 / 上传分档的滑动窗口限流、
Langfuse / LangSmith 可插拔链路追踪、工具调用审计（`tool_audit_logs`）；
**联网搜索已模块化**：`web_search` 的后端可插拔（Bing / DuckDuckGo / Tavily / Serper / Brave / SearXNG），
默认「自动」在免密钥后端间自动回退，管理后台可切换、可一键探测（详见下文「联网搜索」章节）；
**对话支持插入附件**：聊天输入框可选文件上传（txt / md / csv / json / pdf 等，支持拖拽与移除），
附件绑定会话后整个对话都可调用（正文注入 system 的「本会话文件」区，超预算部分由 `read_attachment` 按需读取），
气泡内可展开原文预览（详见下文「对话附件」章节）。
详细设计见 [`docs/技术设计文档.md`](docs/技术设计文档.md)。

---

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | FastAPI + Pydantic v2 + httpx（OpenAI 兼容协议，厂商可在后台切换） |
| 鉴权 | JWT（PyJWT）+ bcrypt 密码哈希 |
| 存储 | SQLAlchemy 2.0 async + Alembic（**MySQL 8+**；可切回内存后端） |
| 加密 | cryptography / Fernet（API-Key 等敏感配置的对称加密） |
| 前端 | Next.js 15（App Router）+ React 19 + TypeScript |
| 实时 | SSE（`text/event-stream`） |

---

## 目录结构

```
Agent/
├── docs/技术设计文档.md
├── scripts/
│   ├── start.ps1                # 一键启动（Windows PowerShell）
│   └── start.sh                 # 一键启动（macOS / Linux）
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口、CORS、异常处理、初始管理员
│   │   ├── config.py            # 基础设施配置（环境变量 / .env）
│   │   ├── api/                 # 路由：auth / sessions / chat / attachments / runtime / knowledge / admin
│   │   ├── core/                # agent 循环、事件、安全、加密、运行时配置、确认注册表、邮件、错误码、
│   │   │                        # 文档解析 / 分块（document_parser、chunking）、摘要压缩（summary）、
│   │   │                        # 向量化后端（embedding）、知识库检索（retriever）、
│   │   │                        # 结构化日志（logging）、限流（ratelimit）、链路追踪（tracing）、工具审计（audit）
│   │   ├── llm/                 # LLM 抽象 + DeepSeek 流式客户端
│   │   ├── search/              # 联网搜索：后端协议 + 预设表 + Bing / DuckDuckGo / Tavily / Serper / Brave / SearXNG 实现
│   │   ├── tools/               # 工具基类、注册表、沙箱隔离层、8 个内置工具
│   │   ├── db/                  # 异步引擎、会话工厂、声明式基类
│   │   ├── models/              # ORM 模型（users / sessions / messages / attachments / tokens /
│   │   │                        # system_config / knowledge_documents / embeddings / tool_audit_logs）
│   │   ├── store/               # 仓储层：抽象接口 + entities + memory / sql 实现
│   │   └── schemas/             # 请求 / 响应模型
│   ├── alembic/                 # 数据库迁移（env.py + versions/）
│   ├── alembic.ini
│   ├── tests/                   # pytest 用例
│   ├── requirements.txt
│   ├── requirements-ml.txt      # 可选：本地语义嵌入（EMBEDDING_BACKEND=local）
│   └── .env.example
└── frontend/
    └── src/
        ├── app/                 # login / register / forgot-password / reset-password / admin / (main)（对话与 /knowledge 知识库）
        ├── components/          # ChatWindow / SessionSidebar / MessageBubble / ToolCallCard / ChatComposer
        ├── hooks/               # useAuth / useChatStream
        └── lib/                 # api 客户端、类型
```

---

## 快速开始

### 0. 一键启动脚本（推荐）

免去手动建虚拟环境 / 装依赖，脚本会自动完成「环境准备 → 启动前后端 → 退出时统一停止」：

```powershell
# Windows（PowerShell）
./scripts/start.ps1                  # 准备环境并启动前后端
./scripts/start.ps1 -SkipInstall     # 跳过依赖安装检查（环境已就绪时最快）
./scripts/start.ps1 -Target backend  # 只启动后端
./scripts/start.ps1 -Setup           # 只准备环境，不启动
./scripts/start.ps1 -Stop            # 停止上次启动的服务
```

```bash
# macOS / Linux
chmod +x scripts/start.sh            # 首次需要赋予执行权限
./scripts/start.sh                   # 准备环境并启动前后端
./scripts/start.sh --skip-install
./scripts/start.sh --target backend
./scripts/start.sh --stop
```

- 首次运行会自动：创建 `backend/.venv`、安装 `requirements-dev.txt`、由 `.env.example` 生成 `backend/.env`、
  安装前端依赖、由 `.env.local.example` 生成 `frontend/.env.local`；
- **启动前做端口预检**：端口被占用会直接报错并给出占用进程，不会留下「启动了但立刻退出」的假象；
- **端口自动联动**：`-BackendPort` / `-FrontendPort` 会同步写入 `frontend/.env.local` 的 `NEXT_PUBLIC_API_BASE_URL`
  与 `backend/.env` 的 `CORS_ORIGINS` / `APP_BASE_URL`（仅在本机地址时改写，自定义过的值只提示不动）；
- **启动后轮询就绪**：后端探 `/api/v1/health`、前端探首页，就绪后再提示访问地址；
- 服务日志落在 `.run/backend.log`、`.run/frontend.log`，启动记录在 `.run/pids.json`（均已 gitignore）；
- 停止时先校验进程身份再回收进程树，避免 PID 被复用后误杀无关进程；
- 若提示脚本未签名，改用 `powershell -ExecutionPolicy Bypass -File ./scripts/start.ps1`。

> 脚本只负责「跑起来」，`JWT_SECRET` / `ENCRYPTION_KEY` 建议手动替换，
> `ADMIN_EMAIL` / `ADMIN_PASSWORD` 填好后启动会自动创建管理员。

需要一批测试账号时（1 个管理员 + 3 个普通用户，以邮箱幂等，可反复执行）：

```bash
backend/.venv/Scripts/python.exe scripts/seed_users.py    # Windows
backend/.venv/bin/python     scripts/seed_users.py        # macOS / Linux
```

账号清单写在脚本顶部的 `ACCOUNTS` 里，可自行增删；要求 `STORE_BACKEND=sql`，内存模式下账号不落库。

### 1. 后端（手动）

```bash
cd backend

python -m venv .venv
# Windows
.venv/Scripts/activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements-dev.txt

cp .env.example .env
# 编辑 .env：
#   - 建议替换 JWT_SECRET 与 ENCRYPTION_KEY
#   - DeepSeek API-Key 可留空，启动后在「管理后台」页面配置（自动加密入库）
#   - 建议填入 ADMIN_EMAIL / ADMIN_PASSWORD 以自动创建管理员

uvicorn app.main:app --reload --port 8000
```

- 接口文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/api/v1/health

> 注意：`.env` 是相对当前工作目录读取的，请在 `backend/` 目录下启动服务。

**知识库检索（可选升级语义能力）**：默认 `EMBEDDING_BACKEND=hash`，零依赖即可跑通
「上传 → 向量化 → 检索」全链路，但只有字面匹配能力。想要真正的语义检索：

```bash
pip install -r requirements-ml.txt          # torch + sentence-transformers
# .env 中改为：EMBEDDING_BACKEND=local
# 重启后端，然后到 /knowledge 页面点「全量重算」重建索引
```

### 2. 数据库（MySQL，可选）

默认 `STORE_BACKEND=memory`（内存存储，重启即丢数据，适合先跑通流程）。要启用持久化：

```bash
# 1) 建库（字符集必须是 utf8mb4）
mysql -u root -p -e "CREATE DATABASE agent_app CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

# 2) 在 backend/.env 中配置
#    STORE_BACKEND=sql
#    DATABASE_URL=mysql+aiomysql://agent:password@127.0.0.1:3306/agent_app?charset=utf8mb4

# 3) 执行迁移建表（在 backend/ 目录下）
.venv/Scripts/python.exe -m alembic upgrade head
```

常用迁移命令（均在 `backend/` 下执行）：

| 命令 | 说明 |
|------|------|
| `alembic upgrade head` | 升级到最新版本 |
| `alembic downgrade -1` | 回滚一个版本 |
| `alembic current` | 查看当前版本 |
| `alembic revision --autogenerate -m "描述"` | 依据 ORM 模型自动生成迁移 |
| `alembic check` | 校验模型与迁移是否一致 |

> 迁移与运行时共用 `.env` 中的 `DATABASE_URL`，无需在 `alembic.ini` 重复配置。
> 本地开发也可设 `AUTO_CREATE_TABLES=true` 让服务启动时自动建表，但生产请始终使用迁移。

### 3. 前端

```bash
cd frontend

npm install
cp .env.local.example .env.local   # 如需修改后端地址

npm run dev
```

打开 http://localhost:3000 → 注册账号 → 开始对话。

### 4. 找回密码（本地开发）

默认 `EMAIL_BACKEND=console`，重置邮件**不会真实发送**，而是打印在后端日志中：

```
[DEV EMAIL] to=alice@example.com
subject=重置你的密码
你好，……请在有效期内点击下面的链接设置新密码：

http://localhost:3000/reset-password?token=xxxxx
```

复制该链接到浏览器即可完成重置。要真实发信，把 `EMAIL_BACKEND` 改为 `smtp` 并填写 `SMTP_*` 配置。

---

## 管理后台（M2）

访问 http://localhost:3000/admin ，仅 `role=admin` 的用户可进入（其他用户会被自动踢回对话页）。

| 能力 | 说明 |
|------|------|
| 模型厂商 | 下拉切换 DeepSeek / OpenAI / Kimi / 通义千问 / 智谱 / 本地 Ollama / 自定义（`GET /admin/llm/providers` 下发预设） |
| API-Key | 加密后写入 `system_config`；页面只回显 `sk-1****cdef` 形式的脱敏值；旧键 `deepseek_api_key` 在 DeepSeek 厂商下继续生效 |
| 默认模型 / 服务地址 | 切换厂商时自动回落到该厂商预设；新建会话时使用，会话级 `model` 仍可覆盖 |
| 最大推理轮次 / 工具超时 | 控制 Agent 的 `MAX_STEPS` 与工具执行上限 |
| 搜索后端 | 下拉切换 `web_search` 的后端（`GET /admin/search/providers` 下发预设并联动密钥 / 地址 / 提示），密钥同样加密入库、脱敏回显 |
| 测试搜索 | `POST /admin/search/test` 用一次**真实检索**探测后端，逐个返回可用性、失败原因、耗时与前 3 条标题样例 |
| 沙箱全局开关 | 全局关停代码执行，优先级高于会话级开关（M3 工具沙箱读取） |
| 用户列表 | 平台用户一览（角色 / 状态 / 注册时间） |
| 审计日志 | `GET /api/v1/admin/audit-logs`：工具调用审计分页查询（M5，支持按用户 / 会话 / 工具 / 状态过滤） |

### 管理员账号

后端启动时会按 `.env` 中的 `ADMIN_EMAIL` / `ADMIN_PASSWORD` / `ADMIN_USERNAME` 按需创建管理员：

```env
ADMIN_EMAIL=admin@example.com
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请改成足够复杂的密码
```

留空则不创建。

### 配置分层

| 类型 | 存放位置 | 示例 |
|------|----------|------|
| 基础设施配置 | `.env`（进程启动时读取） | `DATABASE_URL`、`JWT_SECRET`、`ENCRYPTION_KEY`、`SMTP_*` |
| 业务配置 | `system_config` 表（管理后台维护，**运行时可改**） | `llm_provider`、`llm_api_key`、`llm_base_url`、`default_model`、`search_provider`、`search_api_key`、`search_base_url`、`sandbox_global_enabled` |

业务配置遵循**「库优先、环境变量兜底」**：

- 管理后台配置过的项以库中值为准，`.env` 中的同名项仅作默认值；
- 因此从 M1 升级上来的部署无需改动 `.env` 即可继续运行；
- 模型厂商同理，优先级为 **后台配置 > 环境变量 > 厂商预设**（预设见 `app/llm/providers.py`）；
  后台把地址 / 模型留空表示交还给环境变量，再不成才使用厂商预设；
- 搜索后端沿用同一套规则（预设见 `app/search/providers.py`）：默认 `auto` 只依赖免密钥后端，
  因此开箱即可用；切到 Tavily / Serper / Brave 才需要填密钥；
- 敏感项经 `ENCRYPTION_KEY` 对称加密后入库，明文只存在于服务端进程内。

> ⚠️ 更换 `ENCRYPTION_KEY` 会导致库中既有密文无法解密（读取时按「未配置」处理并回落环境变量），
> 需在管理后台重新填写 API-Key。

---

## 工具与沙箱（M3）

### 工具清单

| 工具 | 作用 | 需沙箱 | 需人工确认 |
|------|------|:------:|:----------:|
| `calculator` | 数学表达式安全求值 | 否 | 否 |
| `web_search` | 联网搜索（后端可插拔，见下文「联网搜索」） | 否 | 否 |
| `fetch_url` | 抓取网页正文并转纯文本 | 否 | 否 |
| `read_file` | 读取沙箱内文本文件 | 是 | 否 |
| `write_file` | 写入沙箱内文本文件 | 是 | **是** |
| `python_exec` | 在受限子进程中执行 Python | 是 | **是** |
| `knowledge_search` | 在**当前用户自己的知识库**中检索相关片段（M4） | 否 | 否 |
| `read_attachment` | 分段 / 关键词读取**本会话附件**正文（M4 续） | 否 | 否 |

> `read_attachment` 是会话附件「超预算降级」的兜底：附件正文默认整体注入 system 的
> 「本会话文件」区（见下文「对话附件」），放不进注入预算的部分不展开，由该工具按需读取；
> 会话里没有任何附件时它的 schema **不下发给模型**（与 `knowledge_search` 同源门控）。

### 双重门控：全局总闸 + 会话开关

沙箱类工具必须**同时**满足两个条件才可用（`Agent._sandbox_enabled`）：

1. 管理员在后台开启 `sandbox_global_enabled`（默认 **false**，fail-closed）；
2. 用户在会话侧边栏打开沙箱开关（随 `POST /chat` 的 `sandbox_enabled` 下发）。

任一条件不满足时，沙箱类工具的 schema **根本不会下发给模型**，模型不会「幻觉」出一个不可用的工具调用。
前端通过 `GET /api/v1/config` 读取全局总闸状态：未开启时开关置灰并提示原因。

### 沙箱隔离（`app/tools/sandbox.py`）

- **路径白名单**：`resolve_path()` 是文件类工具的唯一入口，先规范化再校验，拒绝绝对路径、`..` 与符号链接逃逸；
- **用户隔离**：每个用户一个根目录 `SANDBOX_ROOT/<user_id>`，跨用户访问不可能成立；
- **受限子进程**：`-I -S` 启动解释器（忽略环境变量与用户 site-packages），不使用 `shell=True`，
  参数以列表传递；每次执行使用独立临时工作目录并在结束后清理；
- **环境变量最小化**：只继承 `PATH` / `SYSTEMROOT` 等白名单项，`HOME` / `TEMP` 指向临时目录，避免宿主凭据泄漏；
- **资源限制**：执行超时强制 kill、单路输出上限、虚拟内存上限（`resource` 仅 Unix 生效，其他平台自动跳过）；
- **取消安全**：上层取消（用户停止生成 / 确认超时）时通过 `except BaseException` 回收子进程，不留残余进程。

### 人工确认交互

`write_file` 与 `python_exec` 会改动环境，执行前必须由用户授权：

```
LLM 请求调用 write_file
  └─ Agent 先 create() 登记待确认请求（避免用户秒点确认时找不到目标）
     └─ 下发 SSE 事件 tool_confirm（含 id / name / args / expires_in）
        └─ Agent 挂起等待（不占用额外连接，SSE 流保持打开）
           ├─ 用户点「允许」→ POST /chat/confirm {approved:true}  → 执行工具
           └─ 用户点「拒绝」/ 超时（TOOL_CONFIRM_TIMEOUT）/ 停止生成 → 按拒绝处理
```

- 前端 `ToolCallCard` 在待确认时**强制展开参数**并显示剩余秒数，副作用工具额外给出风险提示；
- 超时、连接中断、用户停止生成一律**按拒绝处理**，绝不默认放行；
- 确认请求保存在进程内（与「停止生成」的任务表一致），多实例部署需改为 Redis。

---

## 联网搜索（`web_search` 后端）

`app/search/` 把「搜索服务」抽象成可插拔后端：工具本身不关心用哪家，换服务只改预设表 + 一个实现类。

```
app/search/base.py        # SearchBackend 协议、SearchResult、SearchError、HTTP 辅助
app/search/providers.py   # PRESETS 预设表 + resolve_search_config() + build_search_chain()
app/search/backends/      # bing / duckduckgo / tavily / serper / brave / searxng
```

### 内置后端

| id | 名称 | 需要密钥 | 说明 |
|----|------|:--------:|------|
| `auto` | 自动（默认） | 否 | 按 `auto_order` 依次尝试免密钥后端，任一成功即返回 |
| `bing` | Bing 网页搜索 | 否 | 解析公开结果页（含 `bing.com/ck/a` 跳转链还原），国内通常可直连 |
| `duckduckgo` | DuckDuckGo（Lite） | 否 | 免登录结果页，走 POST 表单；部分网络不可达，需自备代理 |
| `tavily` | Tavily | 是 | 返回正文片段而非一行摘要，面向 LLM 优化 |
| `serper` | Serper（Google 结果） | 是 | 需要 Google 覆盖面时用 |
| `brave` | Brave Search API | 是 | 独立索引 |
| `searxng` | SearXNG（自建实例） | 可选 | 需填实例地址，并要求实例开启 json 输出 |

### 调用链与降级

1. `web_search` 取 `effective_settings()` → `resolve_search_config()`（**后台配置 > 环境变量 > 预设**）；
2. `build_search_chain()` 决定本次要试的后端：`auto` 展开为免密钥链（Bing → DuckDuckGo），
   显式指定后端时只试它自己（**失败即失败**，便于管理员定位问题，不做隐式兜底）；
3. 成功后把「来源后端」写进工具结果；有失败记录时附一句「… 不可用，已自动改用可用的后端」，
   让模型与用户都知道结果出自哪里；
4. 全部失败或后端未配置时，工具返回**可执行的下一步**（重试 / 换关键词 / 请管理员换后端 / 直接用 `fetch_url`），
   而不是一句干巴巴的错误。

### 后台配置与探测

- `GET /api/v1/admin/search/providers` 下发预设（含 `requires_api_key` / `key_hint` / 是否参与 `auto` 链），
  前端据此联动密钥占位符、地址提示与默认值；
- `POST /api/v1/admin/search/test` 做一次**真实检索**（`limit=3`）并逐个后端回报
  `ok` / `detail`（失败原因，如「请求超时」「网络不可达（可能被墙，需自备代理）」）/ `took_ms` / `sample`（前 3 条标题）；
  `provider` 留空按当前配置探测（`auto` 会测整条链），填了就临时按该后端探测——**不落库、不改配置**；
- 密钥沿用 LLM 那套加密与脱敏回显（`SECRET_KEYS` 含 `search_api_key`），留空表示保持原值不变。

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SEARCH_PROVIDER` | `auto` | 后端 id，取值见上表 |
| `SEARCH_API_KEY` | 空 | Tavily / Serper / Brave（或开了鉴权的 SearXNG）的密钥 |
| `SEARCH_BASE_URL` | 空 | 覆盖预设地址；自建 SearXNG 必填 |
| `SEARCH_LOCALE` | `zh-CN` | 传给后端的语言 / 地区偏好 |
| `SEARCH_TIMEOUT` | `12` | 单个后端的请求超时（秒）；工具整体超时按后端数放宽到 45 秒 |
| `WEB_SEARCH_MAX_RESULTS` | `5` | 每次检索条数上限（与后台「每次检索条数」同源） |

### 新增一个后端

1. 在 `app/search/backends/` 写实现（`id` / `label` + `async search()`，失败一律抛 `SearchError`）；
2. 在 `app/search/providers.py` 的 `PRESETS` 加一条，需要参与「自动」链的再给个 `auto_order`（>0）。

工具定义、系统提示与前端都无需改动。

---

## 知识库与上下文压缩（M4）

### 上下文摘要压缩（FR-6）

长对话不再无限增长：`app/core/summary.py` 在每轮组装消息前判断是否需要压缩。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `SUMMARY_TRIGGER_TOKENS` | 6000 | 历史 token 估算超过该值即触发摘要 |
| `SUMMARY_KEEP_TURNS` | 6 | 压缩时保留最近多少轮完整对话 |
| `SUMMARY_MIN_NEW_TOKENS` | 400 | 待摘要内容不足该值则跳过，避免每轮回调模型 |

- 摘要结果持久化在 `sessions.summary`，用 `summary_upto_seq` 记录已被摘要覆盖到的消息序号，
  因此**不会重复摘要**，也不会漏掉中间新产生的消息；
- 组装给模型的历史为：`[System Prompt + 历史摘要] + history[summary_upto_seq + 1:]`，
  近期对话保持完整，早期内容以一段文字压缩呈现；
- 摘要调用失败时不阻塞本轮对话，退化为「本轮不压缩」。

### 知识库：上传 → 分块 → 向量化 → 检索

上传即完成 **解析 → 分块 → 向量化 → 入库**；前端 `/knowledge` 页面可上传、查看列表、
预览分块（含「已向量化」标记）、重建索引、用问句试检索、删除文档。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/knowledge/documents` | 上传文档（multipart），解析 → 分块 → 向量化后写入 `embeddings` 表 |
| GET | `/api/v1/knowledge/documents` | 当前用户的文档列表（`embedded` 标记该文档是否已有向量） |
| GET | `/api/v1/knowledge/documents/{id}/chunks` | 分块预览（`limit` / `offset` 分页，含 `has_embedding`） |
| DELETE | `/api/v1/knowledge/documents/{id}` | 删除文档及其全部分块 |
| POST | `/api/v1/knowledge/reindex` | 重建索引：默认补齐缺失向量，`?force=true` 全量重算（切换嵌入后端后必须执行） |
| GET | `/api/v1/knowledge/search` | 检索调试：`?q=问句&top_k=5`，与 `knowledge_search` 工具共用同一实现 |

- **格式与限制**：`KNOWLEDGE_ALLOWED_TYPES`（默认 txt / md / markdown / pdf）、
  `KNOWLEDGE_MAX_FILE_BYTES`（默认 5 MB）；文本按 UTF-8 读取、失败回落 GB18030，PDF 由 `pypdf` 抽取
  （会先按 `utf-8-sig` 解码，**自动吃掉 Windows 记事本 / Excel 导出的 BOM**，不把 `\ufeff` 带进正文与分块）；
- **分块策略**（`app/core/chunking.py`）：先按空行分段，再按句子聚合到 `KNOWLEDGE_CHUNK_SIZE`（800 字），
  相邻块保留 `KNOWLEDGE_CHUNK_OVERLAP`（100 字）重叠，且重叠片段不会让分块超长；
- **权限**：文档与分块查询一律带 `user_id` 条件，跨用户访问返回 404，不泄漏资源是否存在。

#### 嵌入后端（`app/core/embedding.py`）

| `EMBEDDING_BACKEND` | 实现 | 依赖 | 说明 |
|---------------------|------|------|------|
| `hash`（默认） | `HashEmbedder` | 无 | 字符 2/3-gram → blake2b 哈希向量 + L2 归一化，零依赖跑通全链路；**只有字面匹配能力** |
| `local` | `LocalEmbedder` | `requirements-ml.txt` | 本地 `BAAI/bge-small-zh-v1.5`（sentence-transformers），真正的语义检索、CPU 可跑 |
| `none` | — | — | 关闭向量化：文档照常入库但不建索引，`knowledge_search` 不下发给模型 |

- 为什么默认 `hash`：默认部署不强制安装 torch（数 GB）。`hash` 用字符 n-gram 哈希向量，
  中文无需分词即可命中大部分字面提问，但**换一种说法就可能召回不到**（无语义泛化）；
- 想升级为语义检索：`pip install -r requirements-ml.txt` → `.env` 改 `EMBEDDING_BACKEND=local` →
  到知识库页面点「全量重算」；
- 哈希函数用 blake2b（64 bit）而非 crc32：短中文串下 crc32 雪崩性差，256/512 维时碰撞噪声足以淹没真实匹配
  （表现为「无关文档比相关文档得分更高」）；
- **切换后端必须重建索引**：不同后端的向量不在同一空间，检索时按维度一致性过滤旧向量，
  因此换后端后旧数据不会产生错误命中，只是暂时检索不到。

#### 检索链路（`app/core/retriever.py`）

查询向量化 → 取该用户全部已向量化分块（上限 `KNOWLEDGE_SEARCH_MAX_CHUNKS`）→ 余弦相似度打分 → top-k：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `KNOWLEDGE_SEARCH_TOP_K` / `..._TOP_K_MAX` | 5 / 20 | 默认与最大返回片段数 |
| `KNOWLEDGE_SEARCH_MAX_CHUNKS` | 2000 | 单次检索最多参与打分的分块数（控制延迟） |
| `KNOWLEDGE_SEARCH_MIN_SCORE` | 0.05 | 低于该相似度的片段直接丢弃 |

- **应用层暴力打分**：MySQL 8 无原生向量类型，`embeddings.embedding` 以 JSON 文本存储，逐条算余弦；
  几万 chunk 量级延迟仍在百毫秒级（装有 numpy 时走矩阵乘法，缺失时回退纯 Python），
  后续换 pgvector / 独立向量库只需替换该列与该函数；
- **用户隔离**：所有 chunk 查询带 `user_id` 条件，检索永远只发生在自己的知识库内；
- **门控与裁剪**：仅当嵌入后端可用**且**该用户已有已向量化分块时，`knowledge_search` 的 schema 才下发给模型
  （`GET /api/v1/config` 同步暴露 `knowledge_enabled` / `knowledge_backend`，前端据此置灰相关入口）；
  每个片段回喂前截断到 400 字，避免检索结果挤占上下文。

---

## 对话附件

聊天输入框可直接把文件**随消息一起发给模型**：选择即上传（拿到 `attachment_id`），发送时只传 id。
解析复用知识库那套 `document_parser`，因此支持格式、编码探测与错误码风格完全一致。

附件一旦随消息发出（绑定会话）就成为**会话级资源**：正文注入 system 的「本会话文件」区，
**整个对话的每一轮都能看到、都能追问**，不需要在后续轮次重新上传；放不进注入预算的部分
由 `read_attachment` 工具按需读取。

### 接口

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| POST | `/api/v1/attachments` | 上传附件：解析为纯文本后暂存，返回元数据（`id` / `filename` / `file_type` / `size_bytes` / `char_count`） | user |
| GET | `/api/v1/attachments/{id}` | 查看附件详情：元数据 + 解析后的正文（供前端「查看原文」，上限 10 万字符并带 `truncated` 标记） | user |
| DELETE | `/api/v1/attachments/{id}` | 删除附件（已发出的附件也可删，历史消息仅保留元数据快照） | user |

上传走 `upload` 限流档（与知识库上传同源，默认 20 次/分钟）。

### 生命周期

```
选择文件 → POST /attachments（解析入库，message_id 为空 = 「还没发出去」）
   ├─ 用户删除 / 一直没发送 → 超过 ATTACHMENT_TTL_SECONDS 由上传路径顺带清理（节流 5 分钟一次）
   └─ POST /chat 带 attachment_ids
        ├─ 发送前校验：数量 ≤ ATTACHMENT_MAX_PER_MESSAGE；逐个校验归属（缺失 / 越权 → 404）
        ├─ 落库 → bind(message_id, session_id) 绑定：附件从「待发送」升级为**会话级资源**，从此豁免 TTL 清理
        └─ 消息行只存元数据快照，重新生成按快照恢复附件（前端无需回传）
```

### 存储与上下文注入

- **正文与消息分表**：元数据快照存 `messages.attachments`（JSON），解析后的正文存 `attachments` 表，
  避免大文本把消息表撑大；绑定后正文长期可查，未绑定的按 TTL 清理；
- **附件是会话级资源**：绑定到会话后，附件正文被注入 system 的**「本会话文件」区**，
  **整个会话的每一轮都能看到**——不必在后续轮次重新上传，也不必让模型重新发起工具调用；
  用户消息里只留一行提示（仅当轮携带附件时才出现：`（本轮新上传：xxx.md；正文见上方「本会话文件」区）`），
  把附件放在 system 前缀里也顺应前缀缓存，历史被摘要压缩后正文仍在场；
- **注入格式**：带序号与边界的文本块，模型能区分「用户说的话」和「文件内容」：

  ```
  【附件 1】manual.md（markdown, 12.3 KB, 4210 字, id=att-uuid-1）
  <附件内容>
  ...
  </附件内容>
  ```

- **双层预算**：单个附件注入上限 `ATTACHMENT_MAX_CHARS`（默认 **10 万字符**，超出部分截断并提示模型）；
  全会话注入上限 `ATTACHMENT_CONTEXT_MAX_CHARS`（默认 **10 万字符**）——预算按**最新文件优先**分配，
  放不下的文件不展开正文，只留一行「（未整体展开：…用 read_attachment(attachment_id="...") 读取）」，
  交给模型按需调用 `read_attachment`（支持 `offset` / `limit` 分段与 `keyword` 命中定位）取用；
- **上下文代价**：10 万汉字 ≈ 6~10 万 token，单个超大附件就可能顶穿多数模型的上下文窗口。
  带超大文件时调小 `ATTACHMENT_MAX_PER_MESSAGE` 或 `ATTACHMENT_CONTEXT_MAX_CHARS`（放不下的自动降级为工具读取），
  或改用知识库检索（RAG）按片段喂，而不是整篇塞进上下文；
- **用户隔离**：上传 / 读取 / 删除 / 随消息发送都校验 `user_id`，跨用户一律 404；
  `read_attachment` 还额外校验**会话归属**——已绑定到其他会话的附件拒绝读取（防猜到 id 跨会话翻文件）；
- **重新生成不丢附件**：`_truncate_last_turn` 除了回滚历史，还会从库中取出该条用户消息的附件 id，
  重新走一遍「校验 → 注入」，附件被删除则跳过并记录日志。

### 前端交互（`ChatComposer`）

- 点「＋」或直接**拖拽文件进输入框**即可添加（拖入时输入框高亮），附件以 chip 展示
  （扩展名 / 文件名 / 大小 / 移除按钮）：上传中显示「解析中…」，失败标红并**在输入框下方直接写出后端给的原因**
  （chip 悬浮提示里也有），移除后可重新选择；单条消息最多 5 个，达到上限时「＋」置灰；
- 有附件正在上传时禁止发送，避免漏掉文件；上传失败的 chip 不参与发送（自动忽略）；
- 移除 chip 会同时调用 `DELETE /attachments/{id}` 清理服务端暂存的附件，不留垃圾数据；
- 用户气泡内以卡片列出附件，点击展开**懒加载原文**（`GET /attachments/{id}`），只加载一次；
- 输入框上方常驻一条**「本会话文件」**（从历史消息的附件元数据推导），让用户知道附件仍可随时追问，
  而不是只有上传那一轮有效。

### 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `ATTACHMENT_MAX_FILE_BYTES` | `5242880` | 单个附件大小上限（字节，5 MB） |
| `ATTACHMENT_ALLOWED_TYPES` | `txt,md,markdown,log,csv,json,pdf` | 允许的扩展名 |
| `ATTACHMENT_MAX_PER_MESSAGE` | `5` | 单条消息最多携带的附件数 |
| `ATTACHMENT_MAX_CHARS` | `100000` | 单个附件注入模型上下文的最大字符数（超出部分截断并提示；≈ 6~10 万 token，按模型窗口自行下调） |
| `ATTACHMENT_CONTEXT_MAX_CHARS` | `100000` | 单个会话注入附件的总字符预算，按最新文件优先分配；放不下的降级为 `read_attachment` 按需读取 |
| `ATTACHMENT_TTL_SECONDS` | `86400` | 未发送附件的保留时长（秒） |

> 超限文件 / 不支持的格式复用知识库错误码：`DOCUMENT_TOO_LARGE` / `UNSUPPORTED_DOCUMENT` / `DOCUMENT_PARSE_ERROR`。

**chip 显示「失败」时，输入框下方会给出具体原因**，常见几类：

| 原因 | 输入框下方给出的原因 | 处理 |
|------|----------------------|------|
| 格式不在白名单 | `仅支持 csv、json、log、markdown、md、pdf、txt 类型的附件` | `.docx` / `.xlsx` / 图片请先转成 PDF 或文本；要放开需同时改 `ATTACHMENT_ALLOWED_TYPES` 与解析器类型集 |
| PDF 缺解析依赖 | `解析 PDF 需要可选依赖 pypdf，请执行：pip install pypdf` | 执行 `pip install -r requirements.txt`（`pypdf>=4.2` 已在依赖清单里） |
| 文件过大 | `附件不能超过 5 MB` | 换小文件，或调 `ATTACHMENT_MAX_FILE_BYTES` |
| 空文件 | `文件内容为空` | 换有内容的文件 |
| PDF 无文本层 | `未能从 PDF 中提取到文本（可能是扫描件，需要 OCR）` | 扫描版 PDF 需先 OCR |
| 文件名超长 | `文件名过长（最多 255 个字符）` | 改短文件名后重试 |
| 服务端未捕获异常 | `服务内部错误，请稍后重试（详情见后端日志）` | 到 `.run/backend.log` 按 `request_id` 找 traceback |
| 未认证 / 登录过期 | `未认证或登录已失效` | 重新登录（前端会自动清掉本地 token） |
| 后端没启动 / 正在重启 | `无法连接后端服务：后端未启动、正在重启或响应被中断，请稍后重试` | 用 `scripts/start.ps1` 起服务；重启窗口内重试即可 |
| 改了后端代码不生效 | 旧行为仍在（如仍按旧上限截断） | `--reload` 在本机**不可靠**：日志可能出现 `Reloading...` 却几分钟后才重启、甚至一直不重启，改完请 `Ctrl+C` 后重跑 `scripts/start.ps1` |
| 触发限流 | `请求过于频繁，请 N 秒后再试` | 附件上传与知识库共用 `upload` 档（默认 20 次/分钟），稍后重试 |

---

## 可观测性与限流（M5）

### 结构化日志（`app/core/logging.py`）

- 启动时由 `setup_logging(level, fmt)` 统一配置 root logger，**不再使用 `logging.basicConfig`**；
- `request_id` / `trace_id` 存在 `contextvars` 中，由 `RequestContextMiddleware` 为每个请求生成并写入响应头
  `X-Request-ID`，因此同一次请求内（含后台任务与工具子调用）的所有日志都能串起来；
- 两种格式：`LOG_FORMAT=text`（本地可读）与 `LOG_FORMAT=json`（生产采集，字段白名单见 `EXTRA_FIELDS`）；
- 访问日志统一为 `POST /api/v1/chat -> 200 (12ms)`，慢请求定位不再需要翻业务日志。

### 限流（`app/core/ratelimit.py`）

进程内**滑动窗口**计数器（无 Redis 依赖，单实例够用），分档配额如下：

| 档位 | 配置项 | 默认（每 60s） | 触发点 |
|------|--------|:------:|--------|
| ip | `RATE_LIMIT_IP_PER_MINUTE` | 600 | 所有请求（兜底防刷量） |
| auth | `RATE_LIMIT_AUTH_PER_MINUTE` | 20 | `/auth/*`（防撞库） |
| chat | `RATE_LIMIT_CHAT_PER_MINUTE` | 30 | `POST /chat` |
| upload | `RATE_LIMIT_UPLOAD_PER_MINUTE` | 20 | 知识库上传 / 重建索引 / 附件上传 |

- 超限返回 **429**，响应体 `code=RATE_LIMITED`，并带 `Retry-After` / `X-RateLimit-*` 头；
- **中间件顺序**：`RateLimitMiddleware` 在最外层，因此限流的 429 也带 `X-Request-ID` 与 CORS 头；
- **fail-open**：限流器自身异常时放行请求，绝不因「限流组件故障」阻断业务；
- **代理透传**：默认用 `request.client.host`；仅当部署在可信网关之后才开启 `RATE_LIMIT_TRUST_PROXY`
  读取 `X-Forwarded-For` / `X-Real-IP`（否则可被伪造绕过）；
- `/api/v1/health` 免限流，便于探活。

> 已知简化：计数器在进程内存中，**多实例部署需替换为 Redis 等共享存储**（与确认注册表、运行中任务表一致）。

### 链路追踪（`app/core/tracing.py`）

一次对话 = 一个 `Turn`，其下每个 LLM 调用 / 工具执行 = 一个 `Span`（含入参、出参、状态、耗时、token）：

| 配置项 | 说明 |
|--------|------|
| `OBSERVABILITY_LOG_SPANS` | `true` 时把每个 Turn / Span 打到结构化日志（**零外部依赖**即可观测） |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | 配置后启用 Langfuse 上报 |
| `langfuse` 包 | 可选依赖；未安装或未配置时 `LangfuseSink` **自动降级为 no-op** |

- 设计为 `TracerSink` 协议 + 扇出到多个 sink（`LogSink` / `LangfuseSink`），换 LangSmith 只需新增一个 sink；
- 追踪失败不影响主流程（与限流同样的 fail-open 原则）。

### 工具调用审计（`tool_audit_logs`）

`app/core/audit.py` 在 Agent 每次工具执行后留痕，回答「谁、在哪个会话、调用了什么工具、参数、结果、耗时、是否经过确认」：

| 字段 | 说明 |
|------|------|
| `user_id` / `session_id` | 谁、在哪个会话 |
| `tool_name` | 调用的工具 |
| `args` | 参数（**脱敏**：`api_key` / `password` / `token` 等键替换为 `***`；**截断**到 2000 字符） |
| `result` | 结果摘要（截断到 500 字符） |
| `status` | `success` / `error` / `timeout` / `rejected` / `blocked` |
| `duration_ms` | 耗时 |
| `approved` | 是否为经用户确认后执行（M3 人工确认结果） |

- 拒绝、超时、报错**同样留痕**，便于回答「Agent 是否尝试过危险操作」；
- 落库失败不阻塞业务（fail-open）；时间倒序分页查询见管理后台接口：

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | `/api/v1/admin/audit-logs` | 审计记录分页查询，支持 `user_id` / `session_id` / `tool_name` / `status` / `limit` / `offset` | admin |

---

## 运行时开关接口

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | `/api/v1/config` | 返回 `sandbox_global_enabled`（含沙箱可用工具名）与知识库检索能力 `knowledge_enabled` / `knowledge_backend`（**不含任何敏感项**） | user |
| POST | `/api/v1/chat/confirm` | 回应 `tool_confirm`：`{session_id, tool_call_id, approved}` | user |

---

## 测试

```bash
cd backend
.venv/Scripts/python.exe -m pytest tests -q
```

共 196 个用例，覆盖：

- 健康检查、注册 / 登录 / 鉴权失败、参数校验、重复注册
- 找回密码全流程（含**令牌一次性**、**令牌过期/无效**、**防用户枚举**）
- 会话归属隔离（A 用户无法读取 B 用户的会话与消息）
- `/chat` 未配置 API-Key 时的 503
- **SSE 工具调用链路**：`start → text → tool_call → tool_result → text → usage → done`
- 模型异常与最大推理轮次保护
- 计算器工具的安全求值（拒绝 `__import__`、`9**9**9`、除零等）
- **仓储契约（SQL 实现）**：登录大小写不敏感、会话隔离与软删除、消息 `seq` 连续与 JSON 往返、重置令牌一次性 / 过期 / 批量作废
- **端到端持久化**：关闭并重启应用后，用户与会话仍能从数据库恢复
- **管理后台（M2）**：权限隔离（匿名 401 / 普通用户 403）、
  API-Key **密文入库且响应不泄漏明文**、后台配置后 `/chat` 立即可用、清除密钥回落环境变量、
  默认模型与会话联动、字段级局部更新、参数校验、沙箱全局开关、用户列表
- **SQL 后端的配置持久化**：直接读库校验密文，重启后配置仍生效
- **工具沙箱与抓取（M3，26 个用例）**：路径逃逸防护（`../`、空串、绝对路径、Windows 驱动器、跨用户隔离）、
  文件读写往返与大小上限、`python_exec` 捕获 stdout / 运行时错误 / **超时强杀** / **不泄漏宿主环境变量**、
  沙箱关闭时工具 schema 不下发、需确认工具被门控、`fetch_url` 拦截 `file://` / `localhost` / 私网 / 云元数据地址、
  `web_search` 走可插拔后端（后端构造全部打桩，不发真实网络请求）
- **人工确认链路（M3，8 个用例）**：允许后真正执行、拒绝后不落盘、**超时按拒绝处理**、
  会话关闭沙箱时沙箱工具不可用、失效确认请求返回 409、跨会话确认被拒、未登录 401、`/config` 暴露沙箱开关
- **上下文摘要压缩（M4）**：token 超阈值时触发摘要、摘要落库并前移 `summary_upto_seq`、
  未达阈值或增量不足时不产生摘要
- **知识库（M4）**：分块长度上限与重叠正确性、文本 / PDF 解析、上传后分块入库与列表 / 预览 / 删除、
  越权访问返回 404、未登录 401、超限文件与不支持类型被拒
- **向量化与检索（M4，13 个用例）**：嵌入向量 L2 归一化与「相关文本得分更高」、
  上传即标记 `embedded`、检索按 `user_id` 隔离且命中正确文档、
  关闭向量化时检索返回 `EMBEDDING_UNAVAILABLE`、重建索引补齐缺失向量、
  `knowledge_search` 工具片段格式、工具不跨用户泄漏、
  注册表门控（无向量时不下发 `knowledge_search`）、`/config` 暴露嵌入后端、
  Agent 在可用 / 不可用两种情况下下发或隐藏该工具、
  **SQL 后端的向量 JSON 往返 / `only_embedded` 筛选 / 回填与越权保护**
- **联网搜索（32 个用例，`tests/test_search.py`）**：四层各有覆盖——
  **解析层**：默认后端是 `auto` 且只由免密钥后端组成、显式后端只构造一个实现、仅凭预设即可解析、
  运行时地址覆盖预设地址、未知后端报错并列出可选项；
  **后端层**：Bing 结果页解析（标题 / 链接 / 摘要、`bing.com/ck/a` 跳转链还原、条数上限、页面结构变化时报错）、
  DuckDuckGo 走 POST 表单、API 后端缺密钥报 `SearchNotConfiguredError`、SearXNG 缺实例地址报错且请求带 `format=json`、
  Tavily / Serper / Brave 的请求形状与响应解析（全部打桩，不发真实网络请求）；
  **工具层**：结果渲染带来源后端与条数、首个后端失败自动切下一个并附提示、全后端失败与未配置时的降级文案、空关键词被拒；
  **接口层**：`/admin/search/providers` 与 `/admin/search/test` 的权限（匿名 401 / 普通用户 403）、
  后台切换后端即生效、密钥**密文入库**、非法后端被配置校验拒绝、探测接口逐个后端返回状态与耗时
- **对话附件（19 个用例，`tests/test_chat_attachments.py`）**：接口层——上传解析出元数据、
  **`.json` 等结构化文本真的能上传**（白名单与前端 accept 都承诺支持 json，解析器不得漏）、
  **超 65535 字节的长正文原样入库不截断**（MySQL `TEXT` 上限，见下）、
  非法类型 / 空文件 / 超长文件名 / 超限文件被拒、未登录 401、详情截断标记、**用户隔离**（他人附件 404）、
  **未捕获异常返回带 CORS 头的 JSON 500**（否则浏览器读不到 body，会把 500 误报成「后端没启动」）；
  链路层——正文按 `【附件 N】文件名` + `<附件内容>` 注入 system 的「本会话文件」区、
  **上传后的后续轮次不传 id 也能看到正文**（会话级可用，工具照常下发）、
  **全会话预算不足时降级为「未整体展开 + read_attachment 提示」**（列 id 让模型自己取）、
  **会话里没有附件时不下发 `read_attachment`**、
  元数据随消息落库并完成绑定、附件缺失 / 越权时发送报 404、超过单条消息上限报 400、
  `ATTACHMENT_MAX_CHARS` 生效为截断、**4 万字的附件整篇注入**（旧默认 2 万字符会把后半段悄悄截掉）、
  **重新生成按库中快照恢复附件**、TTL 清理只删未绑定附件
- **附件读取工具（5 个用例，`tests/test_attachment_tool.py`）**：按 offset / limit 分段读取与越界夹取、
  模型把参数传成字符串时的兜底、`keyword` 命中（返回上下文窗口）与未命中、空 id / 不存在的 id 给出可纠正的错误、
  **他人附件读不到**、**已绑定到其他会话的附件拒绝读取**、
  注册表按 `attachments_enabled` 门控该工具、工具只读且不需确认 / 沙箱
- **SQL 仓储（追加）**：附件写入 / 批量取回保序 / 消息 `attachments` JSON 往返 / 绑定 / 过期清理 / 删除、
  **大文本列在 MySQL 方言下编译为 `LONGTEXT`**（模型层的字节上限守门）
- **限流 / 可观测性 / 审计（M5，16 个用例）**：滑动窗口计数与前 N 次放行、
  不同 key 互不影响、关闭开关即全部放行、超限抛 `RATE_LIMITED` 且带 `Retry-After`、
  IP 档 429（`health` 免限流）、认证档配额更严、成功响应带 `X-RateLimit-*`、
  Tracer 埋点扇出到 sink、无 sink 时全链路 no-op、
  一次对话产出 2 个 `llm.chat` + 1 个 `tool.call` span、
  审计参数脱敏 / 截断、正常调用留痕、拒绝调用也留痕（`status=rejected`）、
  管理后台审计查询与权限（匿名 401 / 普通用户 403）

> 测试默认跑在内存后端（零依赖）；SQL 仓储与持久化用例使用临时 SQLite 异步库，
> 与 MySQL 共享同一套 SQLAlchemy 模型与迁移，因此无需本地 MySQL 即可回归。

---

## 核心设计要点

### Agent ReAct 循环

`app/core/agent.py` 是唯一的编排入口，以异步生成器逐步产出事件：

```
用户输入 → LLM(带 tools) → 若有 tool_calls：
    ├─ 落库 assistant(tool_calls)
    ├─ 逐个执行工具 → 产出 tool_call 事件
    │    └─ 若需人工确认：产出 tool_confirm → 挂起等待 POST /chat/confirm
    │         └─ 拒绝 / 超时 / 停止生成 → status=rejected，工具不执行
    ├─ 产出 tool_result → 结果以 role=tool 回喂模型
    └─ 回到 LLM（最多 MAX_STEPS 轮）
否则 → 落库 assistant 文本 → 产出 usage / done
```

### 模型厂商适配（`app/llm/`）

`app/llm/openai_compat.py` 用**一份客户端**承载所有 OpenAI 兼容厂商，切换厂商的影响面收敛在配置解析处：

- `resolve_llm_config()` 是唯一解析入口，优先级 **后台配置 > 环境变量 > 厂商预设**，
  `build_llm_client()` 据此构造客户端；客户端自身不读环境变量，参数全部来自 `LLMConfig`；
- 预设表 `app/llm/providers.py` 描述每家的 `base_url` / 常用模型 / 是否需要 API-Key /
  是否支持 `stream_options`，新增厂商只需加一行；
- 本地部署（Ollama）不需要密钥，此时不下发 `Authorization` 头；缺密钥时报 `API_KEY_NOT_CONFIGURED`；
- 旧键 `deepseek_api_key` / `deepseek_base_url` 在 DeepSeek 厂商下等效新键，老 `.env` 零改动。

### SSE 事件协议

| 事件 | 载荷 |
|------|------|
| `start` | `message_id`, `session_id` |
| `thinking` | `delta`（reasoning_content） |
| `text` | `delta` |
| `tool_call` | `id`, `name`, `args` |
| `tool_confirm` | `id`, `name`, `args`, `expires_in`（需人工确认，流挂起等待 `POST /chat/confirm`） |
| `tool_result` | `id`, `status`, `summary`（`status` 取值：`success` / `error` / `timeout` / `rejected`） |
| `usage` | `prompt_tokens`, `completion_tokens` |
| `done` | `finish_reason` |
| `error` | `code`, `message` |

### Mermaid 作图

模型侧「愿意画」+ 前端「画得出来」，两层配合：

- **模型侧**：`SYSTEM_PROMPT` 第 9 条要求流程、架构、时序、状态、ER、排期、占比等结构化内容
  主动附 Mermaid 图，并给出防解析失败的语法约束（节点 id 避开保留字、文案含符号时加双引号、
  不用 `%%{init}%%` / `click` / HTML 标签），同时限定「每轮最多 1~2 张、简单内容不硬塞图」；
- **前端侧**：`MermaidBlock` 按需 `import("mermaid")`（不进首屏包），渲染串行化并做卸载竞态保护，
  随站点主题在 `default` / `dark` 间切换后重渲染；
- **降级**：生成中先显示源码并提示「回答完成后渲染」；语法错误时回退源码 + `Parse error` 提示，
  回答正文不受影响。

### 错误码

`UNAUTHORIZED` / `FORBIDDEN` / `INVALID_CREDENTIALS` / `USER_EXISTS` / `VALIDATION_ERROR` /
`INVALID_RESET_TOKEN` / `SESSION_NOT_FOUND` / `API_KEY_NOT_CONFIGURED` / `LLM_ERROR` /
`MAX_STEPS_EXCEEDED` / `CONFIRMATION_NOT_FOUND` / `EMBEDDING_UNAVAILABLE` / `RATE_LIMITED` /
`ATTACHMENT_NOT_FOUND` / `TOO_MANY_ATTACHMENTS`

统一响应体：`{ "code": "...", "message": "..." }`

### 数据访问层（M2）

`app/store/` 以**抽象仓储接口**隔离持久化实现，上层（API / Agent）只依赖 `AbstractStore`：

```
app/models/           # ORM 模型（SQLAlchemy 2.0 Mapped[...]）
app/db/base.py        # 异步 engine / sessionmaker / Base（惰性创建）
app/store/entities.py # 领域实体（dataclass），两种实现的统一返回类型
app/store/base.py     # AbstractStore 抽象契约
app/store/memory.py   # 进程内实现（开发 / 测试）
app/store/sql.py      # SQLAlchemy 实现（MySQL / SQLite）
app/store/__init__.py # get_store()：按 STORE_BACKEND 选择实现
```

- **切换零成本**：`STORE_BACKEND=memory|sql`，业务代码无需改动；
- **行为一致**：SQL 实现每次调用独立事务，提交后把 ORM 行转成领域实体返回，
  因此不受 session 生命周期影响，与内存实现同构；
- **时间口径统一**：入库存 naive UTC（MySQL DATETIME 不带时区），
  读取时补回 `tzinfo=UTC`，跨方言行为一致；
- **迁移**：`0001_initial_schema.py` 建立四张表（users / sessions / messages / password_reset_tokens），
  `0002_system_config.py` 新增 `system_config`，`0003_session_summary.py` 增加会话摘要字段，
  `0004_knowledge_base.py` 新增知识库两张表，`0005_tool_audit_logs.py` 新增工具审计表，
  `0006_chat_attachments.py` 新增 `attachments` 表并给 `messages` 加附件元数据列，
  `0007_long_text_columns.py` 把 `attachments.text` / `messages.content` / `embeddings.embedding`
  从 `TEXT` 放宽到 `LONGTEXT`（MySQL 的 `TEXT` 只有 65535 **字节**，一个 4 万汉字的附件正文就装不下），
  `alembic check` 可校验模型与迁移是否漂移。

### 业务配置读取层（M2 续）

`app/core/runtime_config.py` 提供 `RuntimeConfigService`，负责 `system_config` 的读写：

- **接入面极窄**：`chat.py` 只调用 `effective_settings()`，拿到的是把库中值合并进
  `Settings` 的副本（不污染进程级单例），LLM 客户端与 Agent 复用同一份配置；
- **缓存与失效**：进程内 TTL 缓存（`RUNTIME_CONFIG_TTL`，默认 15 秒），
  写入后立即失效；同时校验环境变量指纹，`Settings` 变化时旧缓存自动失效；
- **加解密**：`app/core/crypto.py` 用 Fernet（AES-128-CBC + HMAC-SHA256），
  密钥由 `ENCRYPTION_KEY` 经 SHA-256 派生，因此任意长度的随机串都可直接使用；
- **失败降级**：解密失败（密钥被更换）不抛异常，按「未配置」处理并回落环境变量，
  单个坏值不会导致服务不可用。

---

## 已知简化与里程碑进度

| 项 | 当前实现 | 计划 |
|----|----------|------|
| 存储 | 默认内存，可切 MySQL 持久化 | ~~M2~~ **已完成**（SQLAlchemy 2.0 async + Alembic） |
| 数据访问 | 仓储抽象 + 内存/SQL 双实现，按配置切换 | ~~M2~~ **已完成** |
| 模型厂商 | 预设表 + `OpenAICompatClient`，后台切换即生效（DeepSeek / OpenAI / Kimi / 通义 / 智谱 / Ollama / 自定义） | 新增厂商只需在 `providers.py` 加一行 |
| 令牌存储 | 前端 localStorage | 后续改为 httpOnly Cookie + Refresh Token |
| 工具数量 | 8 个（calculator / web_search / fetch_url / read_file / write_file / python_exec / knowledge_search / read_attachment） | ~~M3 + M4~~ **已完成** |
| 附件注入口径 | 附件绑定会话后成为**会话级资源**，正文注入 system 的「本会话文件」区，整个会话各轮都可见；超预算的文件降级为 `read_attachment` 按需读取（非多模态文件上传），模型按文本读取 | 后续可扩展图片 / 语音等多模态 |
| 联网搜索后端 | `app/search/` 协议 + 预设表，默认 `auto`（Bing → DuckDuckGo 自动回退），可在后台切到 Tavily / Serper / Brave / SearXNG 并一键探测 | **已完成**（新增后端 = 一个实现类 + 一条预设） |
| 工具沙箱 | 受限子进程 + 目录白名单 + 资源限制，全局总闸 + 会话开关双重门控 | ~~M3~~ **已完成** |
| 人工确认 | 确认注册表 + SSE `tool_confirm` + `POST /chat/confirm` + 前端确认卡片 | ~~M3~~ **已完成** |
| 确认 / 运行中任务表 | 进程内（`ConfirmationRegistry`、`_running_tasks`） | 多实例部署改 Redis |
| 沙箱隔离强度 | 受限子进程（`resource` 内存限制仅 Unix 生效） | 后续升级独立容器 / 微虚拟机 |
| Markdown 代码高亮 | 基础代码块样式 | 后续接入语法高亮 |
| Mermaid 图 | 系统提示引导模型在结构化场景主动作图；前端按需加载 mermaid 渲染，生成中与语法错误均回退源码 | **已完成** |
| 限流 | 进程内滑动窗口，按 IP / 认证 / 对话 / 上传分档，超限 429 + `Retry-After`，fail-open | ~~M5~~ **已完成**（多实例部署改 Redis） |
| 结构化日志 | `request_id` / `trace_id` 贯穿全链路，text / json 双格式，`X-Request-ID` 回传前端 | ~~M5~~ **已完成** |
| 链路追踪 | `Tracer` + `TracerSink` 扇出，`LogSink` 零依赖、`LangfuseSink` 可选并自动降级 | ~~M5~~ **已完成** |
| 工具审计 | `tool_audit_logs` 记录工具 / 参数（脱敏）/ 结果 / 耗时 / 状态 / 是否确认，管理后台可分页查询 | ~~M5~~ **已完成** |
| 上下文摘要压缩 | 触发阈值 + 保留轮次 + 增量下限，摘要持久化于 `sessions.summary` | ~~M4（FR-6）~~ **已完成** |
| 知识库文档 | 上传 / 解析 / 分块 / 预览 / 删除（MySQL 存储，向量列为 JSON 文本） | ~~M4~~ **已完成** |
| 向量化与检索 | 可插拔嵌入后端（默认零依赖 `hash`，可选本地 BGE）+ `knowledge_search` 工具 + 重建索引 / 检索调试接口 | ~~M4~~ **已完成**；语义质量取决于所选嵌入后端 |
| 向量存储 | JSON 文本 + 应用层余弦暴力检索（数万 chunk 量级） | 数据量上来后换 pgvector / 独立向量库 |
| 对话附件 | 输入框上传 / 拖拽（txt / md / csv / json / pdf 等），绑定后**整个会话可用**（system 注入 + `read_attachment` 按需读取）、气泡内可预览原文；未发送附件按 TTL 清理 | **已完成**（正文与消息分表存储） |
| 停止生成 | 单实例内存任务表 | 多实例部署改 Redis |

> 关于「思考过程」：`deepseek-flash` 不返回 `reasoning_content`，此时中间推理文本会作为 `text` 事件直接流式展示；接入 `deepseek-reasoner` 后 `thinking` 事件会自动填充。
