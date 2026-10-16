"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import AuthShell from "@/components/AuthShell";
import { useAuth } from "@/hooks/useAuth";
import { ApiError } from "@/lib/api";

const MIN_PASSWORD_LENGTH = 8;

export default function RegisterPage() {
  const { user, initializing, register } = useAuth();
  const router = useRouter();

  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!initializing && user) router.replace("/");
  }, [initializing, user, router]);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);

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
      await register(username.trim(), email.trim(), password);
      router.replace("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "注册失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AuthShell
      title="创建账号"
      subtitle="注册后立即开始使用，数据按账号隔离"
      footer={
        <>
          <span>已有账号？</span>
          <Link href="/login">去登录</Link>
        </>
      }
    >
      <form className="auth-form" onSubmit={handleSubmit}>
        {error ? <div className="alert alert-error">{error}</div> : null}

        <div className="field">
          <label htmlFor="username">用户名</label>
          <input
            id="username"
            className="input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="3-64 位，字母 / 数字 / _ . -"
            minLength={3}
            maxLength={64}
            pattern="[A-Za-z0-9_.\-]+"
            autoComplete="username"
            required
          />
        </div>

        <div className="field">
          <label htmlFor="email">邮箱</label>
          <input
            id="email"
            className="input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="用于找回密码"
            autoComplete="email"
            required
          />
        </div>

        <div className="field">
          <label htmlFor="password">密码</label>
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
          <label htmlFor="confirm">确认密码</label>
          <input
            id="confirm"
            className="input"
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            placeholder="再次输入密码"
            autoComplete="new-password"
            required
          />
        </div>

        <button className="btn btn-block" type="submit" disabled={submitting}>
          {submitting ? "注册中…" : "注册并登录"}
        </button>
      </form>
    </AuthShell>
  );
}
