"use client";

import { useTheme } from "@/hooks/useTheme";
import { toggleTheme } from "@/lib/theme";

type Props = {
  /** 额外类名，便于在不同容器里定位 */
  className?: string;
};

/** 深色 / 浅色切换按钮：图标显示「将切换到的模式」。 */
export default function ThemeToggle({ className }: Props) {
  const theme = useTheme();
  const next = theme === "light" ? "dark" : "light";
  const label = next === "light" ? "切换到浅色模式" : "切换到深色模式";

  return (
    <button
      type="button"
      className={className ? `theme-toggle ${className}` : "theme-toggle"}
      onClick={toggleTheme}
      title={label}
      aria-label={label}
    >
      <span aria-hidden>{next === "light" ? "☀" : "☾"}</span>
    </button>
  );
}
