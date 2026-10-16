"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import AuthShell from "@/components/AuthShell";
import { useAuth } from "@/hooks/useAuth";
import { ApiError } from "@/lib/api";

export default function LoginPage() {
  const { user, initializing, login } = useAuth();
  const router = useRouter();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!initializing && user) router.replace("/");
  }, [initializing, user, router]);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(username.trim(), password);
      router.replace("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "登录失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AuthShell
      title="欢迎回来"
      subtitle="登录后即可开始与智能助手对话"
      footer={
        <>
          <span>还没有账号？</span>
          <Link href="/register">立即注册</Link>
        </>
      }
    >
      <form className="auth-form" onSubmit={handleSubmit}>
        {error ? <div className="alert alert-error">{error}</div> : null}

        <div className="field">
          <label htmlFor="username">用户名或邮箱</label>
          <input
            id="username"
            className="input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="alice 或 alice@example.com"
            autoComplete="username"
            required
          />
        </div>

        <div className="field">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <label htmlFor="password">密码</label>
            <Link href="/forgot-password" style={{ fontSize: 12.5 }}>
              忘记密码？
            </Link>
          </div>
          <input
            id="password"
            className="input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="请输入密码"
            autoComplete="current-password"
            required
          />
        </div>

        <button className="btn btn-block" type="submit" disabled={submitting}>
          {submitting ? "登录中…" : "登录"}
        </button>
      </form>
    </AuthShell>
  );
}
