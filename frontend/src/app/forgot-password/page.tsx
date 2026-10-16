"use client";

import Link from "next/link";
import { useState } from "react";

import AuthShell from "@/components/AuthShell";
import { ApiError, request } from "@/lib/api";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await request<{ message: string }>("/api/v1/auth/forgot-password", {
        method: "POST",
        body: { email: email.trim() },
        auth: false,
      });
      setSent(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "发送失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AuthShell
      title="找回密码"
      subtitle="输入注册邮箱，我们会发送一封包含重置链接的邮件"
      footer={
        <>
          <span>想起密码了？</span>
          <Link href="/login">返回登录</Link>
        </>
      }
    >
      {sent ? (
        <div className="alert alert-success">
          若该邮箱已注册，我们已发送重置邮件，请查收（有效期 30 分钟，仅可使用一次）。
        </div>
      ) : (
        <form className="auth-form" onSubmit={handleSubmit}>
          {error ? <div className="alert alert-error">{error}</div> : null}

          <div className="field">
            <label htmlFor="email">注册邮箱</label>
            <input
              id="email"
              className="input"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              autoComplete="email"
              required
            />
          </div>

          <button className="btn btn-block" type="submit" disabled={submitting}>
            {submitting ? "发送中…" : "发送重置邮件"}
          </button>
        </form>
      )}
    </AuthShell>
  );
}
