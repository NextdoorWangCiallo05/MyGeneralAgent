"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import AuthShell from "@/components/AuthShell";
import { ApiError, request } from "@/lib/api";

const MIN_PASSWORD_LENGTH = 8;

function ResetPasswordForm() {
  const params = useSearchParams();
  const token = params.get("token") ?? "";

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);

    if (!token) {
      setError("链接缺少重置令牌，请重新发起找回密码流程");
      return;
    }
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`密码至少 ${MIN_PASSWORD_LENGTH} 位`);
      return;
    }
    if (password !== confirm) {
      setError("两次输入的密码不一致");
      return;
    }

    setSubmitting(true);
    try {
      await request<{ message: string }>("/api/v1/auth/reset-password", {
        method: "POST",
        body: { token, new_password: password },
        auth: false,
      });
      setDone(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "重置失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  }

  if (done) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <div className="alert alert-success">密码已重置成功，请使用新密码登录。</div>
        <Link className="btn btn-block" href="/login">
          前往登录
        </Link>
      </div>
    );
  }

  return (
    <form className="auth-form" onSubmit={handleSubmit}>
      {!token ? (
        <div className="alert alert-error">链接无效或缺少令牌，请重新发起「忘记密码」。</div>
      ) : null}
      {error ? <div className="alert alert-error">{error}</div> : null}

      <div className="field">
        <label htmlFor="password">新密码</label>
        <input
          id="password"
          className="input"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder={`至少 ${MIN_PASSWORD_LENGTH} 位`}
          minLength={MIN_PASSWORD_LENGTH}
          autoComplete="new-password"
          required
        />
      </div>

      <div className="field">
        <label htmlFor="confirm">确认新密码</label>
        <input
          id="confirm"
          className="input"
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          placeholder="再次输入新密码"
          autoComplete="new-password"
          required
        />
      </div>

      <button className="btn btn-block" type="submit" disabled={submitting || !token}>
        {submitting ? "提交中…" : "确认重置"}
      </button>
    </form>
  );
}

export default function ResetPasswordPage() {
  return (
    <AuthShell
      title="设置新密码"
      subtitle="重置后，之前所有登录状态将失效"
      footer={
        <>
          <span>不想重置了？</span>
          <Link href="/login">返回登录</Link>
        </>
      }
    >
      <Suspense fallback={<div className="alert alert-info">正在读取重置链接…</div>}>
        <ResetPasswordForm />
      </Suspense>
    </AuthShell>
  );
}
