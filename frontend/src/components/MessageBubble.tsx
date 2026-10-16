"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import MermaidBlock, { extractMermaidSource } from "@/components/MermaidBlock";
import ToolCallCard from "@/components/ToolCallCard";
import { fetchAttachment } from "@/lib/api";
import type { AttachmentDetail, AttachmentMeta, ChatMessage } from "@/lib/types";

type Props = {
  message: ChatMessage;
  /** 确认/拒绝待执行的副作用工具（由会话页透传）。 */
  onConfirm?: (callId: string, approved: boolean) => Promise<void> | void;
  /** 重新生成本条回答（仅最后一条回答由会话页透传）。 */
  onRegenerate?: (messageId: string) => Promise<void> | void;
  /**
   * 已被用户从会话中删除的附件 id。
   *
   * 消息里的附件只是发送当时的快照（历史记录不该被改写），所以「删没删」由会话页
   * 用当前附件清单算出来传进来；清单还没就绪时传 undefined，避免把刚发的文件误标成已删除。
   */
  deletedAttachmentIds?: Set<string>;
};

/**
 * 思考过程：推理模型边想边吐时展开跟随，`thinking_end`（或本轮结束）后自动收起；
 * 用户点标题可随时展开/收起——手动开合过之后就不再自动跟随，免得打断阅读。
 */
function ThinkingBlock({ text, streaming }: { text: string; streaming: boolean }) {
  const [open, setOpen] = useState(streaming);
  const [touched, setTouched] = useState(false);

  useEffect(() => {
    if (!touched) setOpen(streaming);
  }, [streaming, touched]);

  return (
    <div className={`thinking${open ? " is-open" : ""}`}>
      <button
        type="button"
        className="thinking-head"
        onClick={() => {
          setTouched(true);
          setOpen((prev) => !prev);
        }}
        aria-expanded={open}
      >
        <span className="thinking-label">思考过程</span>
        <span className="thinking-state">
          {streaming ? "思考中…" : `已完成 · ${text.length} 字`}
        </span>
        <span className="thinking-toggle" aria-hidden>
          {open ? "收起" : "展开"}
        </span>
      </button>
      {open ? <div className="thinking-body">{text}</div> : null}
    </div>
  );
}

/** 消息里的附件：点击展开解析后的原文（正文按需向后端取，不随历史消息下发）。 */
function AttachmentCard({ item, deleted = false }: { item: AttachmentMeta; deleted?: boolean }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<AttachmentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function toggle() {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (detail || loading) return;

    setLoading(true);
    setError(null);
    try {
      setDetail(await fetchAttachment(item.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "原文加载失败");
    } finally {
      setLoading(false);
    }
  }

  if (deleted) {
    // 已被用户从会话中删除：留一行不可展开的记录（历史快照不篡改，原文也确实取不到了）
    return (
      <div className="attach-card is-deleted" title="该文件已从会话中删除，正文不再进入上下文">
        <div className="attach-card-head is-static">
          <span className="attach-ext">{attachmentExt(item.filename)}</span>
          <span className="attach-name">{item.filename}</span>
          <span className="attach-size">已删除</span>
        </div>
      </div>
    );
  }

  return (
    <div className={`attach-card${open ? " is-open" : ""}`}>
      <button type="button" className="attach-card-head" onClick={() => void toggle()}>
        <span className="attach-ext">{attachmentExt(item.filename)}</span>
        <span className="attach-name">{item.filename}</span>
        <span className="attach-size">{attachmentSize(item.size_bytes)}</span>
        <span className="attach-toggle" aria-hidden>
          {open ? "收起" : "原文"}
        </span>
      </button>
      {open ? (
        <div className="attach-card-body">
          {loading ? (
            <div className="attach-hint">正在读取附件原文…</div>
          ) : error ? (
            <div className="attach-hint is-error">{error}</div>
          ) : detail ? (
            <>
              <pre className="attach-preview">{detail.text}</pre>
              {detail.truncated ? (
                <div className="attach-hint">原文过长，此处仅展示前 10 万字</div>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/** 取扩展名做角标，如 `报告.pdf` → `PDF`。 */
function attachmentExt(name: string): string {
  const index = name.lastIndexOf(".");
  if (index <= 0 || index === name.length - 1) return "FILE";
  return name.slice(index + 1).toUpperCase().slice(0, 4);
}

function attachmentSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

export default function MessageBubble({
  message,
  onConfirm,
  onRegenerate,
  deletedAttachmentIds,
}: Props) {
  const isUser = message.role === "user";
  // 生成中不出现操作条；出错的回答仍可复制已有内容或重新生成
  const showActions = !isUser && !message.pending && Boolean(message.content || message.error);
  const pending = Boolean(message.pending);

  // 引用保持稳定，否则每次增量渲染都会重建元素类型，导致 Mermaid 图表被反复重挂载
  const components = useMemo<Components>(
    () => ({
      // 宽表格改为容器内横向滚动，避免撑破消息区
      table: ({ node: _node, ...props }) => (
        <div className="md-table-wrap">
          <table {...props} />
        </div>
      ),
      // ```mermaid 代码块交给 MermaidBlock；其余代码块保持默认渲染
      pre: ({ node: _node, children, ...props }) => {
        const source = extractMermaidSource(children);
        if (source !== null) return <MermaidBlock code={source} pending={pending} />;
        return <pre {...props}>{children}</pre>;
      },
    }),
    [pending],
  );

  return (
    <div className={`msg ${isUser ? "msg-user" : "msg-assistant"}`}>
      <div className="msg-avatar" aria-hidden>
        {isUser ? "我" : "AI"}
      </div>
      <div className="msg-body">
        <div className="msg-role">{isUser ? "你" : "助手"}</div>

        {message.thinking ? (
          <ThinkingBlock text={message.thinking} streaming={pending && !message.thinkingDone} />
        ) : null}

        {message.toolCalls?.length ? (
          <div className="tool-cards">
            {message.toolCalls.map((call) => (
              <ToolCallCard key={call.id} call={call} onConfirm={onConfirm} />
            ))}
          </div>
        ) : null}

        {isUser ? (
          <>
            <div className="msg-content">{message.content}</div>
            {message.attachments?.length ? (
              <div className="msg-attachments">
                {message.attachments.map((item) => (
                  <AttachmentCard
                    key={item.id}
                    item={item}
                    deleted={deletedAttachmentIds?.has(item.id) ?? false}
                  />
                ))}
              </div>
            ) : null}
          </>
        ) : message.content ? (
          <div className={`msg-content ${message.pending ? "cursor-blink" : ""}`}>
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
              {message.content}
            </ReactMarkdown>
          </div>
        ) : message.pending ? (
          <div className="msg-content cursor-blink" />
        ) : null}

        {message.error ? (
          <div className="alert alert-error" style={{ marginTop: 10 }}>
            {message.error}
          </div>
        ) : null}

        {showActions ? (
          <div className="msg-actions">
            {message.content ? <CopyButton text={message.content} /> : null}
            {onRegenerate ? (
              <button
                type="button"
                className="msg-action"
                onClick={() => void onRegenerate(message.id)}
              >
                <span className="msg-action-icon" aria-hidden>
                  ↻
                </span>
                重新生成
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

type CopyState = "idle" | "copied" | "failed";

function CopyButton({ text }: { text: string }) {
  const [state, setState] = useState<CopyState>("idle");
  const timer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  const handleClick = async () => {
    const ok = await copyToClipboard(text);
    setState(ok ? "copied" : "failed");
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setState("idle"), 1600);
  };

  return (
    <button
      type="button"
      className={`msg-action ${state === "copied" ? "is-copied" : ""} ${
        state === "failed" ? "is-failed" : ""
      }`}
      onClick={() => void handleClick()}
      aria-live="polite"
    >
      <span className="msg-action-icon" aria-hidden>
        {state === "copied" ? "✓" : "⧉"}
      </span>
      {state === "copied" ? "已复制" : state === "failed" ? "复制失败" : "复制"}
    </button>
  );
}

/** 优先用异步剪贴板 API；非安全上下文（如局域网 http）退回旧接口。 */
async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // 授权被拒或非安全上下文：继续走降级分支
  }

  try {
    const holder = document.createElement("textarea");
    holder.value = text;
    holder.setAttribute("readonly", "");
    holder.style.position = "fixed";
    holder.style.top = "-9999px";
    document.body.appendChild(holder);
    holder.select();
    const ok = document.execCommand("copy");
    holder.remove();
    return ok;
  } catch {
    return false;
  }
}
