"use client";

import { useEffect, useState, type ReactNode } from "react";

import { useTheme } from "@/hooks/useTheme";

/**
 * Mermaid 图表渲染。
 *
 * 三种状态各有取舍：
 * - 流式生成中（pending）：只展示源码。半截的图语法必然解析失败，会不停闪烁；
 * - 渲染成功：展示 SVG（mermaid 默认 securityLevel=strict，会 sanitize 标签）；
 * - 渲染失败：回退成源码 + 错误提示，回答本身不会因此丢失。
 *
 * 库体积不小，故按需 import()，不进首屏包。
 */

type Mermaid = typeof import("mermaid").default;

let modulePromise: Promise<Mermaid> | null = null;

/** 动态加载 mermaid（同一进程内只加载一次）。 */
function loadMermaid(): Promise<Mermaid> {
  if (!modulePromise) {
    modulePromise = import("mermaid").then((mod) => mod.default);
  }
  return modulePromise;
}

/**
 * mermaid 是带全局状态的单例，且 render() 内部会插入临时 DOM，
 * 并发调用会互相干扰，这里用一个 Promise 队列把渲染串行化。
 */
let renderQueue: Promise<unknown> = Promise.resolve();

function enqueue<T>(task: () => Promise<T>): Promise<T> {
  const run = renderQueue.then(task, task);
  renderQueue = run.catch(() => undefined);
  return run;
}

let seq = 0;

type Phase = "waiting" | "rendering" | "ready" | "failed";

export default function MermaidBlock({ code, pending }: { code: string; pending: boolean }) {
  const theme = useTheme();
  const source = code.trim();
  const [phase, setPhase] = useState<Phase>(pending ? "waiting" : "rendering");
  const [svg, setSvg] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (pending || !source) return;

    let cancelled = false;
    setPhase("rendering");

    const id = `mermaid-${(seq += 1)}`;
    void enqueue(async () => {
      try {
        const mermaid = await loadMermaid();
        if (cancelled) return;
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          // 与站点主题保持一致：深色用内置 dark，浅色用 default
          theme: theme === "light" ? "default" : "dark",
        });
        const result = await mermaid.render(id, source);
        if (cancelled) return;
        setSvg(result.svg);
        setError("");
        setPhase("ready");
      } catch (err) {
        if (cancelled) return;
        // 失败时 mermaid 可能在 body 里留下临时节点，清掉避免残留
        document.getElementById(id)?.remove();
        document.getElementById(`d${id}`)?.remove();
        setError(err instanceof Error ? err.message : "图表渲染失败");
        setPhase("failed");
      }
    });

    return () => {
      cancelled = true;
    };
  }, [source, theme, pending]);

  if (phase === "ready") {
    return (
      <div className="md-mermaid" data-state="ready">
        <div className="md-mermaid-canvas" dangerouslySetInnerHTML={{ __html: svg }} />
      </div>
    );
  }

  const note =
    phase === "waiting"
      ? "Mermaid 图会在回答生成完成后渲染"
      : phase === "rendering"
        ? "正在渲染图表…"
        : `图表渲染失败，已回退为源码：${firstLine(error)}`;

  return (
    <div className="md-mermaid" data-state={phase}>
      <div className={`md-mermaid-note ${phase === "failed" ? "is-error" : ""}`}>{note}</div>
      <pre className="md-mermaid-source">
        <code>{source}</code>
      </pre>
    </div>
  );
}

function firstLine(text: string): string {
  const line = (text || "").split("\n").find((item) => item.trim().length > 0) ?? "";
  return line.length > 200 ? `${line.slice(0, 200)}…` : line;
}

/** 从 <pre> 的子元素里取出 mermaid 代码块；非 mermaid 返回 null。 */
export function extractMermaidSource(children: ReactNode): string | null {
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (!child || typeof child !== "object" || !("props" in child)) continue;
    const props = (child as { props?: { className?: string; children?: ReactNode } }).props;
    const language = /language-([\w-]+)/.exec(props?.className ?? "")?.[1];
    if (language !== "mermaid") continue;
    return textFromNode(props?.children).replace(/\n$/, "");
  }
  return null;
}

function textFromNode(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textFromNode).join("");
  return "";
}
