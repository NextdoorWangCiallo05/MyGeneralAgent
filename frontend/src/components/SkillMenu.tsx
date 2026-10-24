"use client";

import { useEffect, useRef, useState } from "react";

import type { Skill } from "@/lib/types";

type Props = {
  /** 服务端下发的可用技能（内置 + 自定义） */
  skills: Skill[];
  /** 当前选中的技能 id；空串 = 不使用技能 */
  value: string;
  onChange: (skillId: string) => void;
};

/**
 * 技能选择器：对话级参数（不是会话设置），只影响本轮 system 指令与工具范围。
 * 放置在输入区操作栏，面板向上弹出，避免遮挡输入框。
 */
export default function SkillMenu({ skills, value, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

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

  if (skills.length === 0) return null;

  const current = skills.find((skill) => skill.id === value);
  const active = Boolean(current);

  function pick(skillId: string) {
    onChange(skillId);
    setOpen(false);
  }

  return (
    <div className="skill-menu" ref={rootRef}>
      <button
        type="button"
        className={`mode-toggle skill-trigger${active ? " is-on" : ""}`}
        onClick={() => setOpen((prev) => !prev)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={current ? `当前技能：${current.name}（点击切换）` : "选择技能：为这轮对话指定角色与工具范围"}
      >
        <span className="mode-dot" aria-hidden />
        <span className="skill-trigger-label">{current ? current.name : "技能"}</span>
        <span className="theme-caret" aria-hidden>
          ▾
        </span>
      </button>

      {open ? (
        <div className="skill-panel" role="menu" aria-label="选择技能">
          <div className="theme-panel-title">技能（本轮生效）</div>

          <button
            type="button"
            role="menuitemradio"
            aria-checked={!active}
            className={`theme-option${active ? "" : " is-active"}`}
            onClick={() => pick("")}
          >
            <span className="skill-option-text">
              <span className="theme-option-label">通用助手</span>
              <span className="theme-option-hint">不限定角色与工具</span>
            </span>
            <span className="theme-check" aria-hidden>
              {active ? "" : "✓"}
            </span>
          </button>

          <div className="theme-divider" />

          {skills.map((skill) => {
            const selected = skill.id === value;
            return (
              <button
                key={skill.id}
                type="button"
                role="menuitemradio"
                aria-checked={selected}
                className={`theme-option${selected ? " is-active" : ""}`}
                onClick={() => pick(skill.id)}
              >
                <span className="skill-option-text">
                  <span className="theme-option-label">
                    {skill.name}
                    {skill.source === "file" ? <span className="skill-badge">自定义</span> : null}
                  </span>
                  <span className="theme-option-hint">{skill.description || "未填写说明"}</span>
                </span>
                <span className="theme-check" aria-hidden>
                  {selected ? "✓" : ""}
                </span>
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
