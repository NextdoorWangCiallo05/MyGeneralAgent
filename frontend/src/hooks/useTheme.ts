"use client";

import { useSyncExternalStore } from "react";

import { getThemeSnapshot, subscribeTheme, type Theme } from "@/lib/theme";

/**
 * 订阅当前主题。
 * 服务端与首次水合固定按深色渲染（getServerSnapshot），水合后再切到真实值。
 */
export function useTheme(): Theme {
  return useSyncExternalStore(subscribeTheme, getThemeSnapshot, () => "dark" as Theme);
}
