/**
 * 配色方案：用户可选多套色板，选择写入 localStorage，未选择过则跟随系统。
 *
 * 真正的取值写在 <html data-theme="..."> 上，CSS 只认这个属性，
 * 所以样式切换与 React 渲染彼此独立，不会出现「状态对了但样式没跟上」。
 *
 * 加新配色只需两步：在 THEME_OPTIONS 里登记（含菜单里的三色预览），
 * 再到 globals.css 里补一段同名 `[data-theme="..."]` 的令牌覆盖。
 */

export type ThemeId = "dark" | "light" | "midnight" | "ember" | "paper";

/** 只区分明暗：给 `color-scheme`、Mermaid 主题这类只认明暗的第三方用 */
export type ThemeMode = "dark" | "light";

export type ThemeOption = {
  id: ThemeId;
  label: string;
  /** 一句话描述，菜单里做副标题 */
  hint: string;
  mode: ThemeMode;
  /** 菜单预览用的三个色值：[底色, 强调色, 前景色] */
  swatch: [string, string, string];
};

export const THEME_OPTIONS: readonly ThemeOption[] = [
  {
    id: "dark",
    label: "深空蓝",
    hint: "默认 · 靛蓝强调",
    mode: "dark",
    swatch: ["#0b0f19", "#6366f1", "#e8edf7"],
  },
  {
    id: "light",
    label: "晴空白",
    hint: "明亮 · 清爽通透",
    mode: "light",
    swatch: ["#f4f6fb", "#6366f1", "#0f172a"],
  },
  {
    id: "midnight",
    label: "极夜青",
    hint: "深色 · 青绿强调",
    mode: "dark",
    swatch: ["#071316", "#14b8a6", "#e2f4f1"],
  },
  {
    id: "ember",
    label: "落日橙",
    hint: "深色 · 暖橙强调",
    mode: "dark",
    swatch: ["#140d0a", "#f97316", "#f7e9e1"],
  },
  {
    id: "paper",
    label: "纸墨米",
    hint: "浅色 · 护眼米白",
    mode: "light",
    swatch: ["#f6f1e7", "#b45309", "#2b2418"],
  },
];

export const THEME_STORAGE_KEY = "agent-theme";

const THEME_IDS = THEME_OPTIONS.map((item) => item.id);

export function isThemeId(value: unknown): value is ThemeId {
  return typeof value === "string" && THEME_IDS.includes(value as ThemeId);
}

export function themeMode(id: ThemeId): ThemeMode {
  return THEME_OPTIONS.find((item) => item.id === id)?.mode ?? "dark";
}

export function themeLabel(id: ThemeId): string {
  return THEME_OPTIONS.find((item) => item.id === id)?.label ?? "深空蓝";
}

/**
 * 供根布局 <head> 内联执行：首次绘制前就把 data-theme 写好，避免配色闪烁。
 * 这段字符串会原样塞进 <script>，不要引用外部变量。
 */
export const THEME_INIT_SCRIPT = `(function(){try{var ids=${JSON.stringify(
  THEME_IDS,
)};var s=localStorage.getItem(${JSON.stringify(
  THEME_STORAGE_KEY,
)});var m=window.matchMedia('(prefers-color-scheme: light)').matches;document.documentElement.dataset.theme=(ids.indexOf(s)>-1)?s:(m?'light':'dark');}catch(e){document.documentElement.dataset.theme='dark';}})();`;

const listeners = new Set<() => void>();

/** getSnapshot 必须返回稳定值，否则 React 会无限重渲染 */
let cached: ThemeId | null = null;
let systemBound = false;

function systemTheme(): ThemeId {
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/** 用户选过的配色；没选过（或存的是「跟随系统」）返回 null */
function storedTheme(): ThemeId | null {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isThemeId(value) ? value : null;
  } catch {
    // 隐私模式等场景下 localStorage 可能不可用
    return null;
  }
}

/** 当前是不是「跟随系统」（没存过任何配色） */
export function isSystemTheme(): boolean {
  if (typeof window === "undefined") return true;
  return storedTheme() === null;
}

/** 把浏览器的地址栏 / 状态栏颜色也换掉，否则换了配色像只换了一半 */
function syncThemeColor(): void {
  if (typeof document === "undefined") return;
  const color = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
  if (!color) return;
  let meta = document.querySelector('meta[name="theme-color"]:not([media])');
  if (!meta) {
    meta = document.createElement("meta");
    meta.setAttribute("name", "theme-color");
    document.head.appendChild(meta);
  }
  meta.setAttribute("content", color);
}

/** 以 <html data-theme> 为准（内联脚本已写好），属性缺失时兜底重算。 */
export function getThemeSnapshot(): ThemeId {
  if (typeof document === "undefined") return "dark";
  if (cached) return cached;

  const attr = document.documentElement.dataset.theme;
  if (isThemeId(attr)) {
    cached = attr;
  } else {
    cached = storedTheme() ?? systemTheme();
    document.documentElement.dataset.theme = cached;
  }
  return cached;
}

function apply(theme: ThemeId): void {
  cached = theme;
  document.documentElement.dataset.theme = theme;
  syncThemeColor();
  listeners.forEach((listener) => listener());
}

/** 选定配色；传 "system" 表示跟随系统（清掉本地选择）。 */
export function setTheme(theme: ThemeId | "system"): void {
  try {
    if (theme === "system") {
      window.localStorage.removeItem(THEME_STORAGE_KEY);
    } else {
      window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    }
  } catch {
    // 存不下也不影响本次会话
  }
  apply(theme === "system" ? systemTheme() : theme);
}

export function subscribeTheme(listener: () => void): () => void {
  listeners.add(listener);
  bindSystemListener();
  return () => {
    listeners.delete(listener);
  };
}

/** 用户没有明确选择过时，跟随系统外观变化。 */
function bindSystemListener(): void {
  if (systemBound || typeof window === "undefined") return;
  systemBound = true;
  window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
    if (!storedTheme()) apply(systemTheme());
  });
}
