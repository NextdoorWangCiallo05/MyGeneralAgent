import { expect, test } from "@playwright/test";

import { mockBackend, openApp } from "./support/mock";

/**
 * 关键路径冒烟：登录、流式对话、思考模式、技能、配色。
 * 后端一律由 `mockBackend` 拦截，测的是前端行为与请求契约。
 */

test.describe("登录", () => {
  test("未登录访问对话页会跳转到登录页", async ({ page }) => {
    await mockBackend(page);

    await page.goto("/");

    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByText("欢迎回来")).toBeVisible();
    // 未登录不应渲染输入框
    await expect(page.getByPlaceholder(/输入消息/)).toHaveCount(0);
  });

  test("登录成功后进入对话界面", async ({ page }) => {
    await mockBackend(page);
    await page.goto("/login");

    await page.getByLabel("用户名或邮箱").fill("e2e");
    await page.getByLabel("密码").fill("secret123");
    await page.getByRole("button", { name: "登录", exact: true }).click();

    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByPlaceholder(/输入消息/)).toBeVisible();
  });
});

test.describe("对话", () => {
  test("发送消息后流式渲染回复，思考过程自动收起", async ({ page }) => {
    const { chats } = await mockBackend(page);
    await openApp(page);

    await page.getByPlaceholder(/输入消息/).fill("9.11 和 9.9 哪个大？");
    await page.getByTitle("发送").click();

    await expect(page.getByText("9.9 更大。")).toBeVisible();
    await expect(page.getByText("9.11 和 9.9 哪个大？")).toBeVisible();

    // thinking_end 之后默认收起，只保留「思考过程 / 已完成」标题
    const head = page.locator(".thinking-head");
    await expect(head).toHaveAttribute("aria-expanded", "false");
    await head.click();
    await expect(page.locator(".thinking-body")).toContainText("先比较整数部分");

    expect(chats).toHaveLength(1);
    expect(chats[0].message).toBe("9.11 和 9.9 哪个大？");
    // 能力可用时显式下发 false（= 回落默认模型），而不是省略
    expect(chats[0].thinking).toBe(false);
  });

  test("开启思考模式后请求带 thinking，并用 START 回执校准开关", async ({ page }) => {
    const { chats } = await mockBackend(page, { startModel: "deepseek-reasoner" });
    await openApp(page);

    const toggle = page.getByRole("button", { name: /思考模式/ });
    await expect(toggle).toHaveAttribute("aria-pressed", "false");

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-pressed", "true");

    await page.getByPlaceholder(/输入消息/).fill("算一下");
    await page.getByTitle("发送").click();
    await expect(page.getByText("9.9 更大。")).toBeVisible();

    expect(chats[0].thinking).toBe(true);
    // 回执 model 等于推理模型，开关保持开启
    await expect(toggle).toHaveAttribute("aria-pressed", "true");
  });

  test("厂商不支持推理模型时开关置灰且不会下发 thinking", async ({ page }) => {
    const { chats } = await mockBackend(page, { thinkingAvailable: false });
    await openApp(page);

    const toggle = page.getByRole("button", { name: /思考模式/ });
    await expect(toggle).toHaveAttribute("aria-disabled", "true");

    await toggle.click({ force: true });
    await expect(toggle).toHaveAttribute("aria-pressed", "false");

    await page.getByPlaceholder(/输入消息/).fill("你好");
    await page.getByTitle("发送").click();
    await expect(page.getByText("9.9 更大。")).toBeVisible();

    expect(chats[0].thinking).toBeUndefined();
  });

  test("选择技能后请求带 skill_id，输入区回显技能名", async ({ page }) => {
    const { chats } = await mockBackend(page);
    await openApp(page);

    await page.getByRole("button", { name: "技能", exact: true }).click();
    await page.getByRole("menuitemradio", { name: /深度调研/ }).click();
    await expect(page.getByRole("button", { name: /深度调研/ })).toBeVisible();

    await page.getByPlaceholder(/输入消息/).fill("帮我查一下");
    await page.getByTitle("发送").click();
    await expect(page.getByText("9.9 更大。")).toBeVisible();

    expect(chats[0].skill_id).toBe("research");
  });
});

test.describe("配色", () => {
  test("切换配色后立即生效，刷新仍保持", async ({ page }) => {
    await mockBackend(page);
    await openApp(page);

    await page.locator(".theme-trigger").click();
    await page.getByRole("menuitemradio", { name: /极夜青/ }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "midnight");

    await page.reload();
    await page.getByPlaceholder(/输入消息/).waitFor();

    // 首绘前由内联脚本写回 data-theme，因此刷新后不会回到默认配色
    await expect(page.locator("html")).toHaveAttribute("data-theme", "midnight");
    expect(await page.evaluate(() => localStorage.getItem("agent-theme"))).toBe("midnight");
  });
});
