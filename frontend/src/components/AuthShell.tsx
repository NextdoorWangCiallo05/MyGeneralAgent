import Link from "next/link";
import type { ReactNode } from "react";

import ThemeMenu from "@/components/ThemeMenu";

type Props = {
  title: string;
  subtitle: string;
  children: ReactNode;
  footer?: ReactNode;
};

/** 认证页统一外壳（品牌区 + 卡片 + 底部链接）。 */
export default function AuthShell({ title, subtitle, children, footer }: Props) {
  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="auth-brand">
          <span className="auth-logo" aria-hidden>
            ✦
          </span>
          <Link href="/" className="sidebar-title" style={{ color: "inherit" }}>
            Agent App
          </Link>
          <ThemeMenu placement="down" />
        </div>

        <h1>{title}</h1>
        <p className="auth-subtitle">{subtitle}</p>

        {children}

        {footer ? <div className="auth-footer">{footer}</div> : null}
      </div>
    </div>
  );
}
