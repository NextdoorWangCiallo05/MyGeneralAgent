"use client";

import Link from "next/link";

import ThemeToggle from "@/components/ThemeToggle";
import type { Session } from "@/lib/types";

type Props = {
  sessions: Session[];
  activeId: string | null;
  sandboxEnabled: boolean;
  /** 管理员全局总闸状态：关闭时本开关不可用。 */
  sandboxAvailable: boolean;
  userName: string;
  userRole: string;
  onSelect: (sessionId: string) => void;
  onNewChat: () => void;
  onDelete: (sessionId: string) => void;
  onToggleSandbox: (enabled: boolean) => void;
  onLogout: () => void;
};

export default function SessionSidebar({
  sessions,
  activeId,
  sandboxEnabled,
  sandboxAvailable,
  userName,
  userRole,
  onSelect,
  onNewChat,
  onDelete,
  onToggleSandbox,
  onLogout,
}: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <span className="auth-logo" style={{ width: 28, height: 28, fontSize: 15 }} aria-hidden>
          ✦
        </span>
        <span className="sidebar-title">Agent App</span>
      </div>

      <div className="sidebar-nav">
        <button type="button" className="btn btn-ghost btn-block" onClick={onNewChat}>
          ＋ 新建会话
        </button>
        <Link href="/knowledge" className="btn btn-ghost btn-block">
          知识库
        </Link>
      </div>

      <div className="sidebar-body">
        <div className="sidebar-section-label">会话</div>
        {sessions.length === 0 ? (
          <p style={{ padding: "4px 8px", fontSize: 13, color: "var(--text-faint)" }}>
            暂无会话，点击上方按钮开始。
          </p>
        ) : (
          sessions.map((session) => (
            <div
              key={session.id}
              className={`session-item ${session.id === activeId ? "active" : ""}`}
              role="button"
              tabIndex={0}
              onClick={() => onSelect(session.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter") onSelect(session.id);
              }}
            >
              <span className="session-item-title">{session.title || "新会话"}</span>
              <button
                type="button"
                className="session-delete"
                title="删除会话"
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(session.id);
                }}
              >
                ×
              </button>
            </div>
          ))
        )}
      </div>

      <div className="sidebar-footer">
        {userRole === "admin" ? (
          <Link
            href="/admin"
            className="btn btn-ghost btn-block"
            style={{ padding: "8px 10px", fontSize: 13 }}
          >
            ⚙ 管理后台
          </Link>
        ) : null}

        <div className="toggle-row">
          <span>外观</span>
          <ThemeToggle />
        </div>

        <div className="toggle-row">
          <span>沙箱执行</span>
          <button
            type="button"
            className={`switch ${sandboxEnabled ? "on" : ""}`}
            onClick={() => onToggleSandbox(!sandboxEnabled)}
            disabled={!sandboxAvailable}
            title={sandboxAvailable ? "切换沙箱执行" : "管理员未开启全局沙箱，暂不可用"}
            aria-pressed={sandboxEnabled}
            aria-label="切换沙箱执行"
          />
        </div>
        {!sandboxAvailable ? (
          <p className="toggle-hint">管理员未开启全局沙箱，文件读写与代码执行不可用。</p>
        ) : null}

        <div className="user-chip">
          <span className="avatar" aria-hidden>
            {userName.slice(0, 1).toUpperCase()}
          </span>
          <span className="user-chip-meta">
            <span className="user-chip-name">{userName}</span>
            <span className="user-chip-role">{userRole === "admin" ? "管理员" : "普通用户"}</span>
          </span>
          <button type="button" className="btn btn-ghost" style={{ padding: "6px 10px" }} onClick={onLogout}>
            退出
          </button>
        </div>
      </div>
    </aside>
  );
}
