"use client";

import { useEffect, useRef, useState } from "react";

import SkillMenu from "@/components/SkillMenu";
import { deleteAttachment, uploadAttachment } from "@/lib/api";
import type { AttachmentMeta, Skill } from "@/lib/types";

/** 单条消息可携带的附件上限（与后端 `attachment_max_per_message` 默认值一致）。 */
const MAX_ATTACHMENTS = 5;

/** 可选文件类型（与后端 `ATTACHMENT_ALLOWED_TYPES` 默认值一致）。 */
const ACCEPT = ".txt,.md,.markdown,.log,.csv,.json,.pdf";

type PendingAttachment = {
  /** 本地标识：上传期间还没有服务端 id */
  key: string;
  name: string;
  size: number;
  status: "uploading" | "ready" | "error";
  meta?: AttachmentMeta;
  error?: string;
};

type Props = {
  onSend: (text: string, attachments: AttachmentMeta[]) => void;
  onStop: () => void;
  streaming: boolean;
  disabled?: boolean;
  /** 思考模式：开启后本轮换用推理模型，回答前会先思考（过程可展开） */
  thinking?: boolean;
  /** 当前厂商是否提供推理模型；false 时开关置灰并说明原因 */
  thinkingAvailable?: boolean;
  thinkingModel?: string;
  onToggleThinking?: () => void;
  /** 技能：服务端下发的可选技能与当前选择（空串 = 通用助手） */
  skills?: Skill[];
  skillId?: string;
  onSelectSkill?: (skillId: string) => void;
};

export default function ChatComposer({
  onSend,
  onStop,
  streaming,
  disabled,
  thinking = false,
  thinkingAvailable = false,
  thinkingModel = "",
  onToggleThinking,
  skills = [],
  skillId = "",
  onSelectSkill,
}: Props) {
  const [value, setValue] = useState("");
  const [items, setItems] = useState<PendingAttachment[]>([]);
  const [dragging, setDragging] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const uploading = items.some((item) => item.status === "uploading");
  const ready = items.filter((item) => item.status === "ready" && item.meta);
  const full = items.length >= MAX_ATTACHMENTS;
  // 失败原因来自后端（不支持的格式 / 超限 / 解析失败…），必须显式展示，不能只藏在悬浮提示里
  const failed = items.find((item) => item.status === "error");

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }, [value]);

  /** 选择后立即上传，发送时只需要带 id，避免「发送要等上传」 */
  async function upload(file: File) {
    const key = `${file.name}-${file.size}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    setItems((prev) => [...prev, { key, name: file.name, size: file.size, status: "uploading" }]);
    try {
      const meta = await uploadAttachment(file);
      setItems((prev) =>
        prev.map((item) => (item.key === key ? { ...item, status: "ready", meta } : item)),
      );
    } catch (error) {
      setItems((prev) =>
        prev.map((item) =>
          item.key === key
            ? { ...item, status: "error", error: error instanceof Error ? error.message : "上传失败" }
            : item,
        ),
      );
    }
  }

  function addFiles(files: FileList | File[]) {
    if (disabled) return;
    const room = MAX_ATTACHMENTS - items.length;
    if (room <= 0) return;
    Array.from(files)
      .slice(0, room)
      .forEach((file) => void upload(file));
  }

  function remove(item: PendingAttachment) {
    setItems((prev) => prev.filter((entry) => entry.key !== item.key));
    if (item.meta) void deleteAttachment(item.meta.id).catch(() => undefined);
  }

  function submit() {
    const text = value.trim();
    if (!text || streaming || disabled || uploading) return;
    onSend(
      text,
      ready.map((item) => item.meta as AttachmentMeta),
    );
    setItems([]);
    setValue("");
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div className="composer">
      <div className="composer-inner">
        {items.length > 0 && (
          <div className="composer-attachments">
            {items.map((item) => (
              <span
                key={item.key}
                className={`attach-chip is-${item.status}`}
                title={item.error ?? item.name}
              >
                <span className="attach-ext">{extensionOf(item.name)}</span>
                <span className="attach-name">{item.name}</span>
                <span className="attach-size">{statusText(item)}</span>
                <button
                  type="button"
                  className="attach-remove"
                  onClick={() => remove(item)}
                  title="移除附件"
                >
                  ✕
                </button>
              </span>
            ))}
          </div>
        )}

        <div
          className={`composer-box${dragging ? " is-dragging" : ""}`}
          onDragOver={(event) => {
            if (disabled) return;
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            if (event.dataTransfer.files?.length) addFiles(event.dataTransfer.files);
          }}
        >
          <textarea
            ref={textareaRef}
            className="textarea"
            rows={1}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              items.length
                ? "说说要拿这些文件做什么，Enter 发送"
                : "输入消息，Enter 发送，Shift + Enter 换行"
            }
            disabled={disabled}
          />
          <div className="composer-actions">
            {onSelectSkill ? (
              <SkillMenu skills={skills} value={skillId} onChange={onSelectSkill} />
            ) : null}
            <button
              type="button"
              className={`mode-toggle${thinking ? " is-on" : ""}`}
              // 用 aria-disabled 而不是 disabled：原生禁用的按钮收不到指针事件，title 里的原因就看不到了
              aria-disabled={!thinkingAvailable}
              onClick={thinkingAvailable ? onToggleThinking : undefined}
              aria-pressed={thinking}
              title={
                thinkingAvailable
                  ? `思考模式${thinking ? "已开启" : "已关闭"}：用推理模型 ${thinkingModel} 先思考再回答，思考过程可展开`
                  : "当前模型厂商未配置推理模型，无法开启（可用环境变量 THINKING_MODEL 指定）"
              }
            >
              <span className="mode-dot" aria-hidden />
              思考模式
            </button>
            <input
              ref={fileRef}
              type="file"
              multiple
              hidden
              accept={ACCEPT}
              onChange={(event) => {
                if (event.target.files?.length) addFiles(event.target.files);
                event.target.value = "";
              }}
            />
            <button
              type="button"
              className="icon-btn ghost"
              onClick={() => fileRef.current?.click()}
              disabled={disabled || full}
              title={full ? `最多 ${MAX_ATTACHMENTS} 个附件` : "添加附件（txt / md / csv / json / pdf）"}
            >
              ＋
            </button>
            {streaming ? (
              <button type="button" className="icon-btn stop" onClick={onStop} title="停止生成">
                ■
              </button>
            ) : (
              <button
                type="button"
                className="icon-btn"
                onClick={submit}
                disabled={!value.trim() || disabled || uploading}
                title="发送"
              >
                ↑
              </button>
            )}
          </div>
        </div>
        <div className={`composer-tip${failed ? " is-error" : ""}`}>
          {failed
            ? `上传失败：${failed.error ?? "原因未知"}（点 ✕ 移除后可重新选择）`
            : items.length > 0
              ? "附件会在发送前完成解析，最多 5 个；助手回答时会引用文件内容"
              : "内容由 AI 生成，请自行甄别关键信息；支持拖拽文件到输入框"}
        </div>
      </div>
    </div>
  );
}

/** 取扩展名做角标，如 `报告.pdf` → `PDF`。 */
function extensionOf(name: string): string {
  const index = name.lastIndexOf(".");
  if (index <= 0 || index === name.length - 1) return "FILE";
  return name.slice(index + 1).toUpperCase().slice(0, 4);
}

function statusText(item: PendingAttachment): string {
  if (item.status === "uploading") return "解析中…";
  if (item.status === "error") return "失败";
  return formatSize(item.size);
}

function formatSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}
