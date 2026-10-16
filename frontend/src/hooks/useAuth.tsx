"use client";

/**
 * 全局鉴权上下文：持有当前用户与令牌，提供 login / register / logout。
 * 令牌存放在 localStorage（M1 简化方案），后续可切换为 httpOnly Cookie。
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { clearToken, getToken, request, setToken } from "@/lib/api";

export type AuthUser = {
  id: string;
  username: string;
  email: string;
  role: string;
  sandbox_enabled: boolean;
  email_verified: boolean;
  status: string;
  created_at: string;
};

type TokenResponse = {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: AuthUser;
};

type AuthContextValue = {
  user: AuthUser | null;
  initializing: boolean;
  login: (username: string, password: string) => Promise<void>;
  register: (username: string, email: string, password: string) => Promise<void>;
  logout: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [initializing, setInitializing] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function restore() {
      if (!getToken()) {
        setInitializing(false);
        return;
      }
      try {
        const me = await request<AuthUser>("/api/v1/auth/me");
        if (!cancelled) setUser(me);
      } catch {
        clearToken();
      } finally {
        if (!cancelled) setInitializing(false);
      }
    }

    void restore();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const data = await request<TokenResponse>("/api/v1/auth/login", {
      method: "POST",
      body: { username, password },
      auth: false,
    });
    setToken(data.access_token);
    setUser(data.user);
  }, []);

  const register = useCallback(async (username: string, email: string, password: string) => {
    const data = await request<TokenResponse>("/api/v1/auth/register", {
      method: "POST",
      body: { username, email, password },
      auth: false,
    });
    setToken(data.access_token);
    setUser(data.user);
  }, []);

  const logout = useCallback(() => {
    void request("/api/v1/auth/logout", { method: "POST" }).catch(() => undefined);
    clearToken();
    setUser(null);
  }, []);

  const value = useMemo(
    () => ({ user, initializing, login, register, logout }),
    [user, initializing, login, register, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth 必须在 AuthProvider 内部使用");
  return context;
}
