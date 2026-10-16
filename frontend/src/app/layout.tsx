import type { Metadata, Viewport } from "next";

import { AuthProvider } from "@/hooks/useAuth";
import { THEME_INIT_SCRIPT } from "@/lib/theme";

import "./globals.css";

export const metadata: Metadata = {
  title: "Agent App · 智能助手",
  description: "支持多用户、工具调用与流式输出的智能助手应用",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f4f6fb" },
    { media: "(prefers-color-scheme: dark)", color: "#0b0f19" },
  ],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // data-theme 由下面的内联脚本在首屏前写入，水合时属性已与初始 HTML 不同，
    // 故显式关闭 <html> 的属性水合告警（Next.js 官方推荐写法）
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body>
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
