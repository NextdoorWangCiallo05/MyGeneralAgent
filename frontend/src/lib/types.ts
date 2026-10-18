/** 前后端共享的视图模型。 */

export type Session = {
  id: string;
  title: string;
  model: string;
  sandbox_enabled: boolean;
  /** 早期轮次的对话摘要（FR-6 上下文压缩），为空表示尚未生成。 */
  summary: string;
  created_at: string;
  updated_at: string;
};

export type ServerToolCall = {
  id: string;
  type?: string;
  function?: { name?: string; arguments?: string };
};

export type ServerMessage = {
  id: string;
  session_id: string;
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  seq: number;
  tool_calls?: ServerToolCall[] | null;
  tool_call_id?: string | null;
  /** 随消息上传的附件元数据（正文按需另取，见 fetchAttachment） */
  attachments?: AttachmentMeta[];
  prompt_tokens: number;
  completion_tokens: number;
  created_at: string;
};

/** 对话附件元数据：上传后由 `POST /api/v1/attachments` 返回，随消息一起下发。 */
export type AttachmentMeta = {
  id: string;
  filename: string;
  /** 归一化扩展名，如 txt / md / pdf */
  file_type: string;
  size_bytes: number;
  /** 解析出的正文字符数 */
  char_count: number;
};

/** 附件详情：比元数据多一段解析后的纯文本（供「查看原文」）。 */
export type AttachmentDetail = AttachmentMeta & {
  content_type: string;
  created_at: string;
  /** 是否已随某条消息发出（未发出的会在 TTL 后被清理） */
  bound: boolean;
  text: string;
  truncated: boolean;
};

export type ToolCallStatus =
  | "running"
  | "success"
  | "error"
  | "timeout"
  | "needs_confirmation"
  | "rejected";

export type ToolCallView = {
  id: string;
  name: string;
  args: Record<string, unknown>;
  status: ToolCallStatus;
  summary?: string;
  /** 待确认工具的剩余等待秒数，仅用于提示用户 */
  expiresIn?: number;
};

/** 运行时开关（后端 `/config`，不含任何敏感项）。 */
export type RuntimeConfig = {
  sandbox_global_enabled: boolean;
  sandbox_tools: string[];
  /** 是否启用向量化（false 时知识库检索不可用） */
  knowledge_enabled: boolean;
  /** 当前嵌入后端名，如 hash / local:BAAI/bge-small-zh-v1.5 / none */
  knowledge_backend: string;
  /** 知识库检索工具名（启用时下发，便于前端提示能力范围） */
  knowledge_tool: string | null;
  /** 是否支持「思考模式」：厂商没有可确认的推理模型时为 false，开关置灰 */
  thinking_available: boolean;
  /** 思考模式使用的推理模型名（不可用时为空串） */
  thinking_model: string;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  attachments?: AttachmentMeta[];
  thinking?: string;
  /** 本轮推理内容已输出完毕（收到 thinking_end）：前端据此把「思考过程」自动收起 */
  thinkingDone?: boolean;
  toolCalls?: ToolCallView[];
  pending?: boolean;
  error?: string;
};

export function parseToolArguments(raw: unknown): Record<string, unknown> {
  if (raw && typeof raw === "object") return raw as Record<string, unknown>;
  if (typeof raw !== "string" || !raw.trim()) return {};
  try {
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : {};
  } catch {
    return { raw };
  }
}

/** 历史消息中的工具结果只有文本，按约定前缀还原出卡片状态。 */
function statusFromContent(content: string): ToolCallStatus {
  if (!content.startsWith("错误：")) return "success";
  return content.startsWith("错误：用户拒绝") ? "rejected" : "error";
}

/** 把服务端消息历史还原为前端视图（工具结果回填到对应调用卡片）。 */
export function toChatMessages(list: ServerMessage[]): ChatMessage[] {
  const resultsById = new Map<string, ServerMessage>();
  for (const item of list) {
    if (item.role === "tool" && item.tool_call_id) resultsById.set(item.tool_call_id, item);
  }

  const messages: ChatMessage[] = [];
  for (const item of list) {
    if (item.role === "user") {
      messages.push({
        id: item.id,
        role: "user",
        content: item.content,
        attachments: item.attachments ?? [],
      });
      continue;
    }
    if (item.role !== "assistant") continue;

    const toolCalls = (item.tool_calls ?? []).map<ToolCallView>((call) => {
      const result = resultsById.get(call.id);
      return {
        id: call.id,
        name: call.function?.name ?? "tool",
        args: parseToolArguments(call.function?.arguments),
        // 工具结果只落库了文本（错误统一以「错误：」开头），据此还原卡片状态
        status: result ? statusFromContent(result.content) : "error",
        summary: result?.content,
      };
    });

    messages.push({
      id: item.id,
      role: "assistant",
      content: item.content,
      toolCalls: toolCalls.length ? toolCalls : undefined,
    });
  }
  return messages;
}

/** 知识库文档（M4）：上传后即完成解析、分块与入库。 */
export type KnowledgeDocument = {
  id: string;
  filename: string;
  file_type: string;
  size_bytes: number;
  /** ready（已入库）/ failed（解析失败，原因见 error_message） */
  status: string;
  chunk_count: number;
  char_count: number;
  error_message: string;
  created_at: string;
  /** 该文档的分块是否已生成向量（false 时可在页面上「补齐索引」） */
  embedded: boolean;
};

export type DocumentListResponse = {
  items: KnowledgeDocument[];
  total: number;
};

/** 文档分块：`has_embedding` 接入本地向量模型后为 true。 */
export type KnowledgeChunk = {
  id: string;
  document_id: string | null;
  seq: number;
  content: string;
  char_count: number;
  has_embedding: boolean;
};

export type ChunkListResponse = {
  items: KnowledgeChunk[];
  total: number;
  limit: number;
  offset: number;
};

/** 重建索引结果（POST /knowledge/reindex）。 */
export type ReindexResponse = {
  /** 本次待处理的片段总数 */
  total: number;
  /** 实际写入向量的片段数 */
  embedded: number;
  /** 是否为全量重算 */
  forced: boolean;
  /** 使用的嵌入后端名 */
  backend: string;
};

/** 一条检索命中（后端 /knowledge/search，与 knowledge_search 工具共用同一实现）。 */
export type KnowledgeSearchHit = {
  document_id: string | null;
  filename: string;
  seq: number;
  score: number;
  content: string;
};

export type KnowledgeSearchResponse = {
  query: string;
  backend: string;
  items: KnowledgeSearchHit[];
};

/** 管理后台：系统配置（API-Key 已脱敏，不含明文）。 */
export type AdminConfig = {
  /** 选用的模型厂商 id，可选值来自 GET /api/v1/admin/llm/providers */
  llm_provider: string;
  llm_api_key: string;
  llm_api_key_configured: boolean;
  /** 密钥来自旧版 deepseek_api_key（仅在 deepseek 厂商下生效） */
  llm_api_key_from_legacy: boolean;
  /** 表单字段：留空表示跟随所选厂商的预设 */
  llm_base_url: string;
  default_model: string;
  /** 最终真正生效的参数（厂商预设 + 上面的覆盖） */
  effective_provider: string;
  effective_provider_label: string;
  effective_base_url: string;
  effective_model: string;
  /** 搜索后端 id，可选值来自 GET /api/v1/admin/search/providers */
  search_provider: string;
  search_api_key: string;
  search_api_key_configured: boolean;
  /** 留空表示跟随所选后端的预设地址（自建 SearXNG 必填） */
  search_base_url: string;
  web_search_max_results: number;
  effective_search_provider: string;
  effective_search_label: string;
  effective_search_base_url: string;
  /** 实际会依次尝试的后端名（「自动」模式下多于一个） */
  effective_search_chain: string[];
  sandbox_global_enabled: boolean;
  max_steps: number;
  tool_timeout: number;
  updated_at: string | null;
};

/** 一家可选的 OpenAI 兼容厂商（预设由后端 app/llm/providers.py 下发）。 */
export type LLMProvider = {
  id: string;
  label: string;
  base_url: string;
  models: string[];
  default_model: string;
  requires_api_key: boolean;
  hint: string;
};

export type LLMProviderList = {
  total: number;
  items: LLMProvider[];
  /** 当前生效的厂商 id */
  active: string;
};

/** 一种可选的搜索后端（预设由后端 app/search/providers.py 下发）。 */
export type SearchProvider = {
  id: string;
  label: string;
  base_url: string;
  requires_api_key: boolean;
  /** 密钥形态提示，用作输入框占位符 */
  key_hint: string;
  hint: string;
  /** 是否参与「自动」模式的依次尝试 */
  auto: boolean;
};

export type SearchProviderList = {
  total: number;
  items: SearchProvider[];
  /** 当前生效的后端 id */
  active: string;
};

/** 单个搜索后端的连通性探测结果。 */
export type SearchProbeResult = {
  provider: string;
  label: string;
  ok: boolean;
  /** 成功为「返回 N 条」，失败为中文原因 */
  detail: string;
  took_ms: number;
  /** 前几条标题，便于确认结果是否对路 */
  sample: string[];
};

export type SearchTestResponse = {
  query: string;
  provider: string;
  ok: boolean;
  results: SearchProbeResult[];
  /** 配置层面就无法开始时（如未知后端）的原因 */
  message: string;
};

export type AdminUser = {
  id: string;
  username: string;
  email: string;
  role: string;
  sandbox_enabled: boolean;
  email_verified: boolean;
  status: string;
  created_at: string;
};

export type AdminUserList = {
  total: number;
  items: AdminUser[];
};
