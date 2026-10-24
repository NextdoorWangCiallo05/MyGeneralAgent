/**
 * 后端 API 客户端：统一处理鉴权头、错误码与令牌存储。
 */

import type { AttachmentDetail, AttachmentMeta, Skill } from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

const TOKEN_KEY = "agent_app_token";

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(message: string, code = "UNKNOWN_ERROR", status = 0) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(TOKEN_KEY);
}

type RequestOptions = {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  auth?: boolean;
};

/** 判断是否为 FormData：multipart 上传时浏览器需自行设置边界，不能手动写 Content-Type。 */
function isFormData(value: unknown): value is FormData {
  return typeof FormData !== "undefined" && value instanceof FormData;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, signal, auth = true } = options;
  const headers: Record<string, string> = {};
  const formData = isFormData(body);

  if (body !== undefined && !formData) headers["Content-Type"] = "application/json";
  if (auth) {
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : formData ? body : JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(
      "无法连接后端服务：后端未启动、正在重启或响应被中断，请稍后重试",
      "NETWORK_ERROR",
    );
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const data = text ? safeParse(text) : null;

  if (!response.ok) {
    const code = (data as { code?: string } | null)?.code ?? "HTTP_ERROR";
    const message =
      (data as { message?: string } | null)?.message ?? `请求失败（HTTP ${response.status}）`;
    if (response.status === 401) clearToken();
    throw new ApiError(message, code, response.status);
  }

  return data as T;
}

function safeParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** 以 multipart/form-data 上传单个文件（知识库文档上传）。 */
export function uploadFile<T>(path: string, file: File, field = "file"): Promise<T> {
  const form = new FormData();
  form.append(field, file);
  return request<T>(path, { method: "POST", body: form });
}

/** 上传一个对话附件，返回元数据（解析出的正文留在服务端，发送时按 id 引用）。 */
export function uploadAttachment(file: File): Promise<AttachmentMeta> {
  return uploadFile<AttachmentMeta>("/api/v1/attachments", file);
}

/** 读取附件解析后的正文，用于历史消息里的「查看原文」。 */
export function fetchAttachment(id: string): Promise<AttachmentDetail> {
  return request<AttachmentDetail>(`/api/v1/attachments/${id}`);
}

/** 删除附件（用户在发送前移除，或从会话里移除不再需要的文件）。 */
export function deleteAttachment(id: string): Promise<{ message: string }> {
  return request<{ message: string }>(`/api/v1/attachments/${id}`, { method: "DELETE" });
}

/**
 * 会话当前的附件清单（会话级资源，按上传时间正序）。
 *
 * 历史消息里的附件只是发送当时的快照，用户删除文件后它不会变，
 * 所以「本会话文件」栏与「已删除」标记都以这个接口为准。
 */
export function listSessionAttachments(sessionId: string): Promise<AttachmentMeta[]> {
  return request<AttachmentMeta[]>(`/api/v1/sessions/${sessionId}/attachments`);
}

/** 当前可用的技能（内置 + 服务端 `SKILLS_DIR` 里的自定义技能）。 */
export async function listSkills(): Promise<Skill[]> {
  const data = await request<{ items: Skill[]; total: number }>("/api/v1/skills");
  return data.items;
}
