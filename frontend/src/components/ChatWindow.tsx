"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import ChatComposer from "@/components/ChatComposer";
import MessageBubble from "@/components/MessageBubble";
import SessionSidebar from "@/components/SessionSidebar";
import { useAuth } from "@/hooks/useAuth";
import { useChatStream } from "@/hooks/useChatStream";
import { ApiError, request } from "@/lib/api";
import {
  toChatMessages,
  type AttachmentMeta,
  type ChatMessage,
  type RuntimeConfig,
  type ServerMessage,
  type Session,
  type ToolCallView,
} from "@/lib/types";

const HINTS = [
  "帮我算一下 128 * 47 + sqrt(2) 等于多少",
  "简单介绍一下 ReAct 智能体的工作流程",
  "用表格对比 REST 与 SSE 的适用场景",
  "画一张图说明 HTTPS 建立连接的过程",
];

export default function ChatWindow() {
  const { user, logout } = useAuth();
  const { send, stop, streaming } = useChatStream();

  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sandboxEnabled, setSandboxEnabled] = useState(false);
  const [runtime, setRuntime] = useState<RuntimeConfig | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [usage, setUsage] = useState<{ prompt: number; completion: number } | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  const activeIdRef = useRef<string | null>(null);
  activeIdRef.current = activeId;

  // ---------------------------------------------------------------- 数据加载
  const loadSessions = useCallback(async () => {
    try {
      const list = await request<Session[]>("/api/v1/sessions");
      setSessions(list);
      return list;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return [];
      setBanner("会话列表加载失败");
      return [];
    }
  }, []);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  // 沙箱需要「管理员全局总闸 + 会话开关」同时打开，前端必须先拿到全局状态
  useEffect(() => {
    void (async () => {
      try {
        setRuntime(await request<RuntimeConfig>("/api/v1/config"));
      } catch {
        setRuntime(null); // 读取失败时按「不可用」处理，保持 fail-closed
      }
    })();
  }, []);

  const sandboxAvailable = runtime?.sandbox_global_enabled ?? false;

  useEffect(() => {
    if (!sandboxAvailable) setSandboxEnabled(false);
  }, [sandboxAvailable]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  // ---------------------------------------------------------------- 交互
  const handleSelectSession = useCallback(async (sessionId: string) => {
    setActiveId(sessionId);
    setBanner(null);
    try {
      const history = await request<ServerMessage[]>(`/api/v1/sessions/${sessionId}/messages`);
      setMessages(toChatMessages(history));
    } catch {
      setMessages([]);
      setBanner("历史消息加载失败");
    }
  }, []);

  const handleNewChat = useCallback(() => {
    setActiveId(null);
    setMessages([]);
    setBanner(null);
    setUsage(null);
  }, []);

  const handleDeleteSession = useCallback(
    async (sessionId: string) => {
      try {
        await request(`/api/v1/sessions/${sessionId}`, { method: "DELETE" });
      } catch {
        setBanner("删除会话失败");
      }
      if (activeIdRef.current === sessionId) handleNewChat();
      void loadSessions();
    },
    [handleNewChat, loadSessions],
  );

  const patchAssistant = useCallback((id: string, updater: (m: ChatMessage) => ChatMessage) => {
    setMessages((prev) => prev.map((m) => (m.id === id ? updater(m) : m)));
  }, []);

  const patchToolCall = useCallback((callId: string, patch: Partial<ToolCallView>) => {
    setMessages((prev) =>
      prev.map((m) =>
        m.toolCalls?.some((call) => call.id === callId)
          ? { ...m, toolCalls: m.toolCalls.map((call) => (call.id === callId ? { ...call, ...patch } : call)) }
          : m,
      ),
    );
  }, []);

  /** 回应后端挂起的 `tool_confirm`：唤醒对应的 SSE 流继续执行。 */
  const handleConfirm = useCallback(
    async (callId: string, approved: boolean) => {
      const sessionId = activeIdRef.current;
      if (!sessionId) throw new Error("会话尚未建立，无法确认");

      try {
        await request("/api/v1/chat/confirm", {
          method: "POST",
          body: { session_id: sessionId, tool_call_id: callId, approved },
        });
      } catch (error) {
        setBanner(error instanceof Error ? error.message : "确认失败，请重试");
        throw error instanceof Error ? error : new Error("确认失败，请重试");
      }

      // 先给出即时反馈；随后的 tool_result 事件会覆盖为真实结果
      setBanner(null);
      patchToolCall(callId, {
        status: approved ? "running" : "rejected",
        summary: approved ? "已授权，执行中…" : "已拒绝执行",
        expiresIn: undefined,
      });
    },
    [patchToolCall],
  );

  /** 发起一轮对话，并把 SSE 事件落到指定的助手气泡上。 */
  const streamTurn = useCallback(
    async ({
      text,
      assistantId,
      regenerate,
      attachmentIds,
    }: {
      text: string;
      assistantId: string;
      regenerate?: boolean;
      attachmentIds?: string[];
    }) => {
      await send(
        {
          message: text,
          session_id: activeIdRef.current,
          sandbox_enabled: sandboxAvailable && sandboxEnabled,
          regenerate,
          attachment_ids: attachmentIds,
        },
        {
          onEvent: (event) => {
            switch (event.type) {
              case "start": {
                if (event.session_id !== activeIdRef.current) {
                  activeIdRef.current = event.session_id;
                  setActiveId(event.session_id);
                  void loadSessions();
                }
                break;
              }
              case "text":
                patchAssistant(assistantId, (m) => ({ ...m, content: m.content + event.delta }));
                break;
              case "thinking":
                patchAssistant(assistantId, (m) => ({ ...m, thinking: `${m.thinking ?? ""}${event.delta}` }));
                break;
              case "tool_call":
                patchAssistant(assistantId, (m) => ({
                  ...m,
                  toolCalls: [
                    ...(m.toolCalls ?? []),
                    { id: event.id, name: event.name, args: event.args, status: "running" } satisfies ToolCallView,
                  ],
                }));
                break;
              case "tool_confirm":
                // Agent 会挂起等待 /chat/confirm；此处把卡片切换为「待确认」
                patchAssistant(assistantId, (m) => {
                  const calls = m.toolCalls ?? [];
                  const pending: ToolCallView = {
                    id: event.id,
                    name: event.name,
                    args: event.args,
                    status: "needs_confirmation",
                    expiresIn: event.expires_in,
                  };
                  return {
                    ...m,
                    toolCalls: calls.some((call) => call.id === event.id)
                      ? calls.map((call) => (call.id === event.id ? { ...call, ...pending } : call))
                      : [...calls, pending],
                  };
                });
                break;
              case "tool_result":
                patchAssistant(assistantId, (m) => ({
                  ...m,
                  toolCalls: (m.toolCalls ?? []).map((call) =>
                    call.id === event.id
                      ? {
                          ...call,
                          status: event.status as ToolCallView["status"],
                          summary: event.summary,
                          expiresIn: undefined,
                        }
                      : call,
                  ),
                }));
                break;
              case "usage":
                setUsage({ prompt: event.prompt_tokens, completion: event.completion_tokens });
                break;
              case "error":
                patchAssistant(assistantId, (m) => ({ ...m, error: `${event.message}（${event.code}）` }));
                break;
              case "done":
                patchAssistant(assistantId, (m) => ({ ...m, pending: false }));
                break;
              default:
                break;
            }
          },
        },
      );

      patchAssistant(assistantId, (m) => ({ ...m, pending: false }));
      void loadSessions();
    },
    [loadSessions, patchAssistant, sandboxAvailable, sandboxEnabled, send],
  );

  const handleSend = useCallback(
    async (text: string, attachments: AttachmentMeta[] = []) => {
      if (streaming) return;
      setBanner(null);

      const stamp = Date.now();
      const userId = `local-user-${stamp}`;
      const assistantId = `local-assistant-${stamp}`;

      setMessages((prev) => [
        ...prev,
        { id: userId, role: "user", content: text, attachments },
        { id: assistantId, role: "assistant", content: "", pending: true },
      ]);

      await streamTurn({
        text,
        assistantId,
        attachmentIds: attachments.map((item) => item.id),
      });
    },
    [streamTurn, streaming],
  );

  /** 重新生成：沿用该条回答对应的提问原地重跑（后端会摘掉这一轮再生成）。 */
  const handleRegenerate = useCallback(
    async (assistantId: string) => {
      if (streaming) return;

      // 只有最后一条回答能重跑：截断点之后的内容会被一并丢弃
      const index = messages.findIndex((m) => m.id === assistantId);
      if (index === -1 || index !== messages.length - 1) return;

      const question = [...messages.slice(0, index)].reverse().find((m) => m.role === "user");
      if (!question) {
        setBanner("找不到对应的提问，无法重新生成");
        return;
      }

      setBanner(null);
      // 服务端会替换掉旧回答，本地先清空并回到「生成中」
      patchAssistant(assistantId, () => ({
        id: assistantId,
        role: "assistant",
        content: "",
        pending: true,
      }));
      await streamTurn({ text: question.content, assistantId, regenerate: true });
    },
    [messages, patchAssistant, streamTurn, streaming],
  );

  const activeSession = sessions.find((s) => s.id === activeId);

  // 会话内上传过的文件（按 id 去重、保持上传顺序）。附件是会话级资源：
  // 后端每一轮都会把它们重新注入上下文，所以这里常驻显示，让用户知道
  // "那份文件还在"，可以直接追问，不必重新上传。
  const sessionFiles = useMemo(() => {
    const seen = new Map<string, AttachmentMeta>();
    messages.forEach((message) => {
      message.attachments?.forEach((file) => {
        if (!seen.has(file.id)) seen.set(file.id, file);
      });
    });
    return [...seen.values()];
  }, [messages]);

  return (
    <div className="app-shell">
      <SessionSidebar
        sessions={sessions}
        activeId={activeId}
        sandboxEnabled={sandboxEnabled}
        sandboxAvailable={sandboxAvailable}
        userName={user?.username ?? "用户"}
        userRole={user?.role ?? "user"}
        onSelect={handleSelectSession}
        onNewChat={handleNewChat}
        onDelete={handleDeleteSession}
        onToggleSandbox={setSandboxEnabled}
        onLogout={logout}
      />

      <main className="chat-main">
        <header className="chat-header">
          <div>
            <h2>{activeSession?.title ?? "新会话"}</h2>
            <div className="chat-header-meta">
              {usage
                ? `本会话累计 tokens：输入 ${usage.prompt} / 输出 ${usage.completion}`
                : "支持工具调用与流式输出"}
              {sandboxAvailable && sandboxEnabled ? " · 沙箱已开启" : ""}
              {runtime && !sandboxAvailable ? " · 沙箱已被管理员禁用" : ""}
            </div>
          </div>
        </header>

        <div className="chat-scroll" ref={scrollRef}>
          <div className="chat-inner">
            {banner ? <div className="alert alert-error">{banner}</div> : null}

            {messages.length === 0 ? (
              <div className="empty-state">
                <h2>今天想聊点什么？</h2>
                <p>这是一个支持多用户、工具调用与流式输出的智能助手。</p>
                <div className="empty-hints">
                  {HINTS.map((hint) => (
                    <button
                      key={hint}
                      type="button"
                      className="hint-card"
                      onClick={() => void handleSend(hint)}
                      disabled={streaming}
                    >
                      {hint}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              messages.map((message, index) => (
                <MessageBubble
                  key={message.id}
                  message={message}
                  onConfirm={handleConfirm}
                  onRegenerate={
                    index === messages.length - 1 && message.role === "assistant"
                      ? handleRegenerate
                      : undefined
                  }
                />
              ))
            )}
          </div>
        </div>

        {sessionFiles.length > 0 ? (
          <div className="session-files">
            <span
              className="session-files-label"
              title="本会话上传过的文件在整个对话里都可用，直接追问内容即可（无需重新上传）"
            >
              本会话文件
            </span>
            {sessionFiles.map((file) => (
              <span
                key={file.id}
                className="attach-chip"
                title={`${file.filename}：${file.char_count} 字，已在本会话中，可随时追问`}
              >
                <span className="attach-ext">{file.file_type || "file"}</span>
                <span className="attach-name">{file.filename}</span>
                <span className="attach-size">{file.char_count} 字</span>
              </span>
            ))}
          </div>
        ) : null}

        <ChatComposer
          onSend={(text, attachments) => void handleSend(text, attachments)}
          onStop={stop}
          streaming={streaming}
        />
      </main>
    </div>
  );
}
