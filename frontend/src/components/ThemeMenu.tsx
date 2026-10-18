"use client";

import { useEffect, useRef, useState } from "react";

import { useTheme } from "@/hooks/useTheme";
import { isSystemTheme, setTheme, THEME_OPTIONS, themeLabel } from "@/lib/theme";

type Props = {
  /** 额外类名，便于在不同容器里定位 */
  className?: string;
  /** 面板弹出方向：侧边栏在底部用 up，页头用 down */
  placement?: "up" | "down";
};

/** 配色选择器：一个色板小按钮 + 弹层，5 套配色 + 跟随系统。 */
export default function ThemeMenu({ className, placement = "up" }: Props) {
  const theme = useTheme();
  const [open, setOpen] = useState(false);
  const [followingSystem, setFollowingSystem] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  // 读 localStorage 放到 effect 里，避免 SSR 与水合结果不一致。
  // 依赖 open：每次展开都重读一次，防止外部（别的标签页 / 设置页）改过偏好后显示过时。
  useEffect(() => {
    setFollowingSystem(isSystemTheme());
  }, [theme, open]);

  useEffect(() => {
    if (!open) return;

    function onPointerDown(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }

    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const current = THEME_OPTIONS.find((item) => item.id === theme) ?? THEME_OPTIONS[0];

  function pick(next: Parameters<typeof setTheme>[0]) {
    setTheme(next);
    // 显式写一次：若系统配色恰好等于当前配色，theme 快照不变、effect 不会重跑，
    // 勾选状态就会停在上一个选项上。
    setFollowingSystem(next === "system");
    setOpen(false);
  }

  return (
    <div className={`theme-menu${className ? ` ${className}` : ""}`} ref={rootRef}>
      <button
        type="button"
        className="theme-trigger"
        onClick={() => setOpen((prev) => !prev)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={`配色：${themeLabel(theme)}（点击切换）`}
      >
        <span className="theme-dots" aria-hidden>
          {current.swatch.map((color) => (
            <span key={color} className="theme-dot" style={{ background: color }} />
          ))}
        </span>
        <span className="theme-trigger-label">{current.label}</span>
        <span className="theme-caret" aria-hidden>
          ▾
        </span>
      </button>

      {open ? (
        <div
          className={`theme-panel${placement === "down" ? " is-down" : ""}`}
          role="menu"
          aria-label="选择配色"
        >
          <div className="theme-panel-title">配色</div>
          {THEME_OPTIONS.map((option) => {
            // 跟随系统时，具体配色只是「当前解析结果」，不算用户选中项，
            // 否则会和下面的「跟随系统」同时打勾。
            const active = option.id === theme && !followingSystem;
            return (
              <button
                key={option.id}
                type="button"
                role="menuitemradio"
                aria-checked={active}
                className={`theme-option${active ? " is-active" : ""}`}
                onClick={() => pick(option.id)}
              >
                <span className="theme-dots" aria-hidden>
                  {option.swatch.map((color) => (
                    <span key={color} className="theme-dot" style={{ background: color }} />
                  ))}
                </span>
                <span className="theme-option-text">
                  <span className="theme-option-label">{option.label}</span>
                  <span className="theme-option-hint">{option.hint}</span>
                </span>
                <span className="theme-check" aria-hidden>
                  {active ? "✓" : ""}
                </span>
              </button>
            );
          })}

          <div className="theme-divider" />

          <button
            type="button"
            role="menuitemradio"
            aria-checked={followingSystem}
            className={`theme-option${followingSystem ? " is-active" : ""}`}
            onClick={() => pick("system")}
          >
            <span className="theme-auto-icon" aria-hidden>
              ◐
            </span>
            <span className="theme-option-text">
              <span className="theme-option-label">跟随系统</span>
              <span className="theme-option-hint">随系统外观自动切换明暗</span>
            </span>
            <span className="theme-check" aria-hidden>
              {followingSystem ? "✓" : ""}
            </span>
          </button>
        </div>
      ) : null}
    </div>
  );
}
