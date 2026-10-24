import type { Page, Route } from "@playwright/test";

/**
 * E2E 用的假后端：拦截 `/api/v1/**`，返回固定数据。
 * 好处是测试不依赖后端进程 / 数据库 / 真实 LLM，跑得快且可重复；
 * 真正的后端契约由 `backend/tests` 覆盖。
 */

export const TOKEN_KEY = "agent_app_token";

export const USER = {
  id: "u-e2e",
  username: "e2e",
  email: "e2e@example.com",
  role: "user",
  sandbox_enabled: false,
  email_verified: true,
  status: "active",
  created_at: "2026-01-01T00:00:00Z",
};

export const SKILLS = [
  {
    id: "research",
    name: "深度调研",
    description: "先查资料再下结论",
    tools: ["web_search", "fetch_url", "calculator"],
    source: "builtin",
  },
  {
    id: "writing",
    name: "文案写作",
    description: "结构化写作",
    tools: ["web_search"],
    source: "builtin",
  },
];

/** 默认一轮回复：先思考、再正文，用于验证思考过程自动收起与流式渲染。 */
function buildSse(startModel: string): string {
  const events: Array<[string, unknown]> = [
    ["start", { message_id: "m-e2e", session_id: "s-e2e", model: startModel }],
    ["thinking", { delta: "先比较整数部分，再比小数位。" }],
    ["thinking_end", {}],
    ["text", { delta: "9.9 更大。" }],
    ["usage", { prompt_tokens: 12, completion_tokens: 6 }],
    ["done", { finish_reason: "stop" }],
  ];
  return events.map(([type, data]) => `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`).join("");
}

export const DEFAULT_SSE = buildSse("deepseek-chat");

export type CapturedChat = {
  message?: string;
  thinking?: boolean;
  skill_id?: string;
  session_id?: string | null;
};

type MockOptions = {
  /** 覆盖默认 SSE；需要多轮脚本时传自定义事件 */
  sse?: string;
  /** 会话列表（默认空） */
  sessions?: unknown[];
  /** 思考模式能力（默认可用） */
  thinkingAvailable?: boolean;
  /** START 事件回执的模型名（默认 deepseek-chat；测思考模式时传推理模型） */
  startModel?: string;
};

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
    body: JSON.stringify(body),
  });
}

/** 安装假后端，返回本轮 `/chat` 收到的请求体，供断言。 */
export async function mockBackend(page: Page, options: MockOptions = {}) {
  const chats: CapturedChat[] = [];

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const { pathname } = new URL(request.url());
    const method = request.method();

    if (pathname === "/api/v1/auth/login" && method === "POST") {
      return json(route, {
        access_token: "e2e-token",
        token_type: "bearer",
        expires_in: 7200,
        user: USER,
      });
    }
    if (pathname === "/api/v1/auth/me") return json(route, USER);

    if (pathname === "/api/v1/config") {
      return json(route, {
        sandbox_global_enabled: false,
        sandbox_tools: [],
        knowledge_enabled: false,
        knowledge_backend: "none",
        knowledge_tool: null,
        thinking_available: options.thinkingAvailable ?? true,
        thinking_model: (options.thinkingAvailable ?? true) ? "deepseek-reasoner" : "",
      });
    }

    if (pathname === "/api/v1/skills") {
      return json(route, { items: SKILLS, total: SKILLS.length });
    }

    if (pathname === "/api/v1/sessions" && method === "GET") {
      return json(route, options.sessions ?? []);
    }

    // 会话附件清单（开始生成后前端会拉一次）
    if (/^\/api\/v1\/sessions\/[^/]+\/attachments$/.test(pathname)) {
      return json(route, []);
    }

    if (pathname === "/api/v1/chat" && method === "POST") {
      chats.push(request.postDataJSON() as CapturedChat);
      return route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: options.sse ?? buildSse(options.startModel ?? "deepseek-chat"),
      });
    }

    // 未预期的请求直接暴露出来，避免「测试悄悄通过了但界面在报错」
    return json(route, { code: "NOT_MOCKED", message: `${method} ${pathname}` }, 404);
  });

  return { chats };
}

/** 预置登录态（跳过登录表单）。 */
export async function signIn(page: Page) {
  await page.addInitScript(
    ({ key }) => window.localStorage.setItem(key, "e2e-token"),
    { key: TOKEN_KEY },
  );
}

/** 预置登录态并打开对话页。 */
export async function openApp(page: Page) {
  await signIn(page);
  await page.goto("/");
  await page.getByPlaceholder(/输入消息/).waitFor();
}
