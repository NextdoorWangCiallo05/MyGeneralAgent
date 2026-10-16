"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useAuth } from "@/hooks/useAuth";

/** 主应用布局：未登录一律跳转到登录页。 */
export default function MainLayout({ children }: { children: React.ReactNode }) {
  const { user, initializing } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!initializing && !user) router.replace("/login");
  }, [initializing, user, router]);

  if (initializing) return <div className="loading-screen">正在恢复登录状态…</div>;
  if (!user) return <div className="loading-screen">正在跳转到登录页…</div>;

  return <>{children}</>;
}
