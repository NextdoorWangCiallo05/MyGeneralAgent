/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // E2E 用独立产物目录，避免 dev 服务器清理生产构建的 .next（见 playwright.config.ts）
  distDir: process.env.NEXT_DIST_DIR || ".next",
};

export default nextConfig;
