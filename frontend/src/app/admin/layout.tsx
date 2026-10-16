"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useAuth } from "@/hooks/useAuth";

/** 管理后台布局：未登录跳登录页，非管理员踢回对话页。 */
export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const { user, initializing } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (initializing) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (user.role !== "admin") router.replace("/");
  }, [initializing, user, router]);

  if (initializing) return <div className="loading-screen">正在校验权限…</div>;
  if (!user || user.role !== "admin") {
    return <div className="loading-screen">无管理员权限，正在返回…</div>;
  }

  return <>{children}</>;
}
