import { defineConfig } from "@playwright/test";

const PORT = Number(process.env.PW_PORT ?? 3100);
const BASE_URL = `http://localhost:${PORT}`;

/**
 * 前端 E2E：用 Playwright 拦截 `/api/v1/**`，不依赖真实后端与 LLM。
 *
 * - 默认复用本机已安装的 Chrome（`channel: "chrome"`），免下载浏览器；
 *   没有 Chrome 的环境执行 `npx playwright install chromium` 并设 `PW_CHANNEL=chromium`。
 * - 测试端口固定 3100，避免和开发服务器（3000）撞车；已在跑则直接复用。
 */
export default defineConfig({
  testDir: "./e2e",
  // 单 worker：dev 服务器首次编译路由较慢，并发会互相拖到超时；冒烟测试量小，串行更稳
  fullyParallel: false,
  workers: Number(process.env.PW_WORKERS ?? 1),
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: BASE_URL,
    channel: process.env.PW_CHANNEL ?? "chrome",
    viewport: { width: 1280, height: 800 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    // NEXT_PUBLIC_API_BASE_URL 置空：请求走同源 /api/v1，交给 page.route 处理（也避免跨域）
    command: `npm run dev -- --port ${PORT}`,
    url: `${BASE_URL}/login`,
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
    env: {
      NEXT_PUBLIC_API_BASE_URL: "",
      // 独立产物目录：不碰开发/生产用的 .next
      NEXT_DIST_DIR: ".next-e2e",
      // 某些 IDE 会通过 NODE_OPTIONS 注入「安全删除」垫片，导致 next dev 清理缓存被拦下；
      // 这里清空，仅作用于测试服务器进程（Next 自己删自己的缓存是正常行为）
      NODE_OPTIONS: "",
    },
  },
});
