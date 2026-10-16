/**
 * 主题状态：默认深色；用户选择写入 localStorage，未选择过则跟随系统。
 *
 * 真正的取值写在 <html data-theme="..."> 上，CSS 只认这个属性，
 * 所以样式切换与 React 渲染彼此独立，不会出现「状态对了但样式没跟上」。
 */

export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "agent-theme";

/**
 * 供根布局 <head> 内联执行：首次绘制前就把 data-theme 写好，避免深/浅色闪烁。
 * 这段字符串会原样塞进 <script>，不要引用外部变量。
 */
export const THEME_INIT_SCRIPT = `(function(){try{var s=localStorage.getItem(${JSON.stringify(
  THEME_STORAGE_KEY,
)});var m=window.matchMedia('(prefers-color-scheme: light)').matches;document.documentElement.dataset.theme=(s==='light'||s==='dark')?s:(m?'light':'dark');}catch(e){document.documentElement.dataset.theme='dark';}})();`;

const listeners = new Set<() => void>();

/** getSnapshot 必须返回稳定值，否则 React 会无限重渲染 */
let cached: Theme | null = null;
let systemBound = false;

function systemTheme(): Theme {
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function storedTheme(): Theme | null {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch {
    // 隐私模式等场景下 localStorage 可能不可用
    return null;
  }
}

/** 以 <html data-theme> 为准（内联脚本已写好），属性缺失时兜底重算。 */
export function getThemeSnapshot(): Theme {
  if (typeof document === "undefined") return "dark";
  if (cached) return cached;

  const attr = document.documentElement.dataset.theme;
  if (attr === "light" || attr === "dark") {
    cached = attr;
  } else {
    cached = storedTheme() ?? systemTheme();
    document.documentElement.dataset.theme = cached;
  }
  return cached;
}

function apply(theme: Theme): void {
  cached = theme;
  document.documentElement.dataset.theme = theme;
  listeners.forEach((listener) => listener());
}

export function setTheme(theme: Theme): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // 存不下也不影响本次会话
  }
  apply(theme);
}

export function toggleTheme(): void {
  setTheme(getThemeSnapshot() === "light" ? "dark" : "light");
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
