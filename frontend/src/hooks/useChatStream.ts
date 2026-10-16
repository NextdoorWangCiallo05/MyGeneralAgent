"use client";

/**
 * SSE 流式对话 Hook：解析后端 `text/event-stream`，把事件回调给页面。
 * 支持中途停止（AbortController）。
 */

import { useCallback, useRef, useState } from "react";

import { API_BASE, getToken } from "@/lib/api";

export type ChatEvent =
  | { type: "start"; message_id: string; session_id: string }
  | { type: "thinking"; delta: string }
  /** 本轮推理内容输出完毕（整轮只吐正文时不出现）：前端据此收起「思考过程」 */
  | { type: "thinking_end" }
  | { type: "text"; delta: string }
  | { type: "tool_call"; id: string; name: string; args: Record<string, unknown> }
  | {
      type: "tool_confirm";
      id: string;
      name: string;
      args: Record<string, unknown>;
      expires_in: number;
    }
  | { type: "tool_result"; id: string; status: string; summary: string }
  | { type: "usage"; prompt_tokens: number; completion_tokens: number }
  | { type: "done"; finish_reason: string }
  | { type: "error"; code: string; message: string };

export type ChatPayload = {
  message: string;
  session_id?: string | null;
  model?: string | null;
  sandbox_enabled?: boolean;
  /** 重新生成：`message` 传该轮原提问，后端会摘掉最后一轮再重跑 */
  regenerate?: boolean;
  /** 随本条消息发送的附件 id（由 `POST /api/v1/attachments` 上传后拿到） */
  attachment_ids?: string[];
};

type SendOptions = {
  onEvent: (event: ChatEvent) => void;
};

export function useChatStream() {
  const abortRef = useRef<AbortController | null>(null);
  const [streaming, setStreaming] = useState(false);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
  }, []);

  const send = useCallback(async (payload: ChatPayload, { onEvent }: SendOptions) => {
    const controller = new AbortController();
    abortRef.current = controller;
    setStreaming(true);

    try {
      const token = getToken();
      const response = await fetch(`${API_BASE}/api/v1/chat`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });

      if (!response.ok) {
        const text = await response.text();
        let code = `HTTP_${response.status}`;
        let message = `请求失败（HTTP ${response.status}）`;
        try {
          const parsed = JSON.parse(text) as { code?: string; message?: string };
          code = parsed.code ?? code;
          message = parsed.message ?? message;
        } catch {
          /* 保留默认文案 */
        }
        onEvent({ type: "error", code, message });
        return;
      }

      if (!response.body) {
        onEvent({ type: "error", code: "EMPTY_BODY", message: "服务端未返回数据流" });
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let boundary = buffer.indexOf("\n\n");
        while (boundary !== -1) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const event = parseFrame(frame);
          if (event) onEvent(event);
          boundary = buffer.indexOf("\n\n");
        }
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        return; // 用户主动停止
      }
      onEvent({
        type: "error",
        code: "NETWORK_ERROR",
        message:
          error instanceof Error ? `连接中断：${error.message}` : "连接中断，请稍后重试",
      });
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setStreaming(false);
    }
  }, []);

  return { send, stop, streaming };
}

function parseFrame(frame: string): ChatEvent | null {
  let name = "";
  let data = "";

  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }

  if (!name || !data) return null;

  try {
    const payload = JSON.parse(data) as Record<string, unknown>;
    return { type: name, ...payload } as ChatEvent;
  } catch {
    return null;
  }
}
