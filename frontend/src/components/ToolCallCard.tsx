"use client";

import { useEffect, useState } from "react";

import type { ToolCallStatus, ToolCallView } from "@/lib/types";

const STATUS_LABEL: Record<ToolCallStatus, string> = {
  running: "执行中",
  success: "成功",
  error: "失败",
  timeout: "超时",
  needs_confirmation: "待确认",
  rejected: "已拒绝",
};

/** 会改动环境或执行代码的工具，确认时给出更明确的提示。 */
const RISKY_TOOLS = new Set(["write_file", "python_exec"]);

type Props = {
  call: ToolCallView;
  onConfirm?: (callId: string, approved: boolean) => Promise<void> | void;
};

export default function ToolCallCard({ call, onConfirm }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [remain, setRemain] = useState(0);

  const awaiting = call.status === "needs_confirmation";
  const argsText = JSON.stringify(call.args ?? {}, null, 2);
  // 待确认时强制展开参数：用户必须在看清「将要执行什么」之后才能授权
  const open = expanded || awaiting;

  // 超时由后端自动拒绝，前端只做倒计时提示
  useEffect(() => {
    setRemain(call.expiresIn ?? 0);
    if (!awaiting || !call.expiresIn) return;
    const timer = window.setInterval(() => {
      setRemain((value) => (value > 0 ? value - 1 : 0));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [awaiting, call.expiresIn]);

  const submit = async (approved: boolean) => {
    if (busy || !onConfirm) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm(call.id, approved);
    } catch (err) {
      setError(err instanceof Error ? err.message : "确认失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  const summary =
    call.summary ||
    (call.status === "running" ? "调用中…" : awaiting ? "等待你确认后执行" : "");

  return (
    <div className={`tool-card${awaiting ? " tool-card-awaiting" : ""}`}>
      <div
        className="tool-card-head"
        role="button"
        tabIndex={0}
        onClick={() => setExpanded((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setExpanded((v) => !v);
          }
        }}
      >
        <span className="tool-icon" aria-hidden>
          ⚙
        </span>
        <span className="tool-name">{call.name}</span>
        <span className="tool-summary">{summary}</span>
        <span className={`tool-status status-${call.status}`}>
          {STATUS_LABEL[call.status] ?? call.status}
        </span>
      </div>

      {open ? <pre className="tool-args">{argsText}</pre> : null}

      {awaiting && onConfirm ? (
        <div className="tool-confirm">
          <span className="tool-confirm-hint">
            {RISKY_TOOLS.has(call.name)
              ? "该工具会写入文件或执行代码，请确认内容后再继续。"
              : "该工具需要你确认后才会执行。"}
            {remain > 0 ? `（${remain} 秒内未操作将自动拒绝）` : "（等待已超时，请重新发送）"}
          </span>
          <div className="tool-confirm-actions">
            <button
              type="button"
              className="confirm-allow"
              onClick={() => void submit(true)}
              disabled={busy}
            >
              {busy ? "提交中…" : "允许执行"}
            </button>
            <button
              type="button"
              className="confirm-deny"
              onClick={() => void submit(false)}
              disabled={busy}
            >
              拒绝
            </button>
          </div>
          {error ? <span className="tool-confirm-error">{error}</span> : null}
        </div>
      ) : null}
    </div>
  );
}
