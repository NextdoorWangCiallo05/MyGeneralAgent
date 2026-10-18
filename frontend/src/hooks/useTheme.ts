"use client";

import { useSyncExternalStore } from "react";

import { getThemeSnapshot, subscribeTheme, themeMode, type ThemeId, type ThemeMode } from "@/lib/theme";

/**
 * 订阅当前配色。
 * 服务端与首次水合固定按默认深色渲染（getServerSnapshot），水合后再切到真实值。
 */
export function useTheme(): ThemeId {
  return useSyncExternalStore(subscribeTheme, getThemeSnapshot, () => "dark" as ThemeId);
}

/** 只关心明暗时用这个（Mermaid 等第三方主题只分明暗）。 */
export function useThemeMode(): ThemeMode {
  return themeMode(useTheme());
}
