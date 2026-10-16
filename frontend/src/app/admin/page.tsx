"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import ThemeToggle from "@/components/ThemeToggle";
import { useAuth } from "@/hooks/useAuth";
import { ApiError, request } from "@/lib/api";
import type {
  AdminConfig,
  AdminUserList,
  LLMProviderList,
  SearchProvider,
  SearchProviderList,
  SearchTestResponse,
} from "@/lib/types";

type Notice = { kind: "success" | "error"; text: string };

/** 服务地址提示：只有自建 SearXNG 必填，其余后端填了就是覆盖预设地址。 */
function searchBaseUrlHint(preset: SearchProvider | null): string {
  if (!preset) return "留空跟随环境变量与预设";
  if (preset.id === "searxng") {
    return "必填：自建实例地址，需在实例设置中开启 json 输出格式";
  }
  if (preset.base_url) {
    return `留空跟随环境变量或预设：${preset.base_url}`;
  }
  return "一般无需填写；留空跟随环境变量与预设";
}

export default function AdminPage() {
  const { user, logout } = useAuth();

  const [config, setConfig] = useState<AdminConfig | null>(null);
  const [users, setUsers] = useState<AdminUserList>({ total: 0, items: [] });
  const [catalog, setCatalog] = useState<LLMProviderList>({ total: 0, items: [], active: "" });
  const [searchCatalog, setSearchCatalog] = useState<SearchProviderList>({
    total: 0,
    items: [],
    active: "",
  });
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  // 表单状态：apiKey 留空表示「不修改」，避免误覆盖已配置的密钥；
  // 服务地址与模型留空表示「跟随所选厂商的预设」
  const [provider, setProvider] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [maxSteps, setMaxSteps] = useState("");
  const [toolTimeout, setToolTimeout] = useState("");

  // 联网搜索：密钥留空表示「不修改」，地址留空表示跟随所选后端的预设
  const [searchProvider, setSearchProvider] = useState("");
  const [searchApiKey, setSearchApiKey] = useState("");
  const [searchBaseUrl, setSearchBaseUrl] = useState("");
  const [searchMaxResults, setSearchMaxResults] = useState("");
  const [searchProbe, setSearchProbe] = useState<SearchTestResponse | null>(null);
  const [testing, setTesting] = useState(false);

  const applyConfig = useCallback((next: AdminConfig) => {
    setConfig(next);
    setApiKey("");
    setProvider(next.llm_provider);
    setBaseUrl(next.llm_base_url);
    setModel(next.default_model);
    setMaxSteps(String(next.max_steps));
    setToolTimeout(String(next.tool_timeout));
    setSearchApiKey("");
    setSearchProvider(next.search_provider);
    setSearchBaseUrl(next.search_base_url);
    setSearchMaxResults(String(next.web_search_max_results));
  }, []);

  /** 当前选中厂商的预设：用于占位符、模型建议与提示文案。 */
  const preset = useMemo(
    () => catalog.items.find((item) => item.id === provider) ?? null,
    [catalog.items, provider],
  );

  /** 当前选中搜索后端的预设：用于密钥占位符、地址提示与「测试搜索」的默认目标。 */
  const searchPreset = useMemo(
    () => searchCatalog.items.find((item) => item.id === searchProvider) ?? null,
    [searchCatalog.items, searchProvider],
  );

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const [cfg, list, providers, searchProviders] = await Promise.all([
          request<AdminConfig>("/api/v1/admin/config"),
          request<AdminUserList>("/api/v1/admin/users"),
          request<LLMProviderList>("/api/v1/admin/llm/providers"),
          request<SearchProviderList>("/api/v1/admin/search/providers"),
        ]);
        if (cancelled) return;
        applyConfig(cfg);
        setUsers(list);
        setCatalog(providers);
        setSearchCatalog(searchProviders);
      } catch (error) {
        if (cancelled) return;
        setNotice({
          kind: "error",
          text: error instanceof ApiError ? error.message : "加载系统配置失败",
        });
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [applyConfig]);

  /** 更新配置并回填最新视图。 */
  const saveConfig = useCallback(
    async (body: Record<string, unknown>, okText: string) => {
      setBusy(true);
      setNotice(null);
      try {
        applyConfig(
          await request<AdminConfig>("/api/v1/admin/config", { method: "PUT", body }),
        );
        setNotice({ kind: "success", text: okText });
      } catch (error) {
        setNotice({ kind: "error", text: error instanceof ApiError ? error.message : "保存失败" });
      } finally {
        setBusy(false);
      }
    },
    [applyConfig],
  );

  /** 换厂商时地址与模型几乎必然要跟着换：清空输入框表示跟随新厂商预设。 */
  function handleProviderChange(next: string) {
    setProvider(next);
    setBaseUrl("");
    setModel("");
  }

  async function handleSave(event: React.FormEvent) {
    event.preventDefault();
    if (!config) return;

    // 只提交真正发生变化的字段
    const payload: Record<string, unknown> = {};
    const providerChanged = provider !== config.llm_provider;

    if (providerChanged) {
      payload.llm_provider = provider;
      // 上一家的地址/模型名对新厂商基本不适用，留空即清掉库中旧值、回落到新厂商预设
      payload.llm_base_url = baseUrl.trim();
      payload.default_model = model.trim();
    } else {
      if (baseUrl.trim() !== config.llm_base_url) payload.llm_base_url = baseUrl.trim();
      if (model.trim() !== config.default_model) payload.default_model = model.trim();
    }

    if (apiKey.trim()) payload.llm_api_key = apiKey.trim();

    const steps = Number(maxSteps);
    if (Number.isFinite(steps) && steps !== config.max_steps) payload.max_steps = steps;

    const timeout = Number(toolTimeout);
    if (Number.isFinite(timeout) && timeout !== config.tool_timeout) {
      payload.tool_timeout = timeout;
    }

    if (Object.keys(payload).length === 0) {
      setNotice({ kind: "success", text: "没有需要保存的改动" });
      return;
    }
    await saveConfig(payload, "配置已保存，下一轮对话立即生效");
  }

  /** 换搜索后端时地址跟着换：清空输入框表示跟随新后端的预设地址。 */
  function handleSearchProviderChange(next: string) {
    setSearchProvider(next);
    setSearchBaseUrl("");
    setSearchProbe(null);
  }

  /** 保存搜索配置：同样是「只提交真正变化的字段」，密钥留空表示不修改。 */
  async function handleSaveSearch(event: React.FormEvent) {
    event.preventDefault();
    if (!config) return;

    const payload: Record<string, unknown> = {};
    if (searchProvider !== config.search_provider) {
      payload.search_provider = searchProvider;
      payload.search_base_url = searchBaseUrl.trim();
    } else if (searchBaseUrl.trim() !== config.search_base_url) {
      payload.search_base_url = searchBaseUrl.trim();
    }
    if (searchApiKey.trim()) payload.search_api_key = searchApiKey.trim();

    const limit = Number(searchMaxResults);
    if (Number.isFinite(limit) && limit !== config.web_search_max_results) {
      payload.web_search_max_results = limit;
    }

    if (Object.keys(payload).length === 0) {
      setNotice({ kind: "success", text: "没有需要保存的改动" });
      return;
    }
    await saveConfig(payload, "搜索配置已保存，下一次检索立即生效");
  }

  /** 连通性测试：只做一次真实检索，不落库、不影响会话。 */
  async function handleTestSearch() {
    setTesting(true);
    setSearchProbe(null);
    setNotice(null);
    try {
      setSearchProbe(
        await request<SearchTestResponse>("/api/v1/admin/search/test", {
          method: "POST",
          body: { provider: searchProvider },
        }),
      );
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof ApiError ? error.message : "测试失败" });
    } finally {
      setTesting(false);
    }
  }

  async function handleToggleSandbox(enabled: boolean) {
    setBusy(true);
    setNotice(null);
    try {
      applyConfig(
        await request<AdminConfig>("/api/v1/admin/sandbox", {
          method: "PUT",
          body: { enabled },
        }),
      );
      setNotice({ kind: "success", text: enabled ? "沙箱已全局开启" : "沙箱已全局关闭" });
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof ApiError ? error.message : "切换失败" });
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <div className="loading-screen">正在加载系统配置…</div>;

  if (!config) {
    return <div className="loading-screen">{notice?.text ?? "系统配置加载失败"}</div>;
  }

  return (
    <div className="admin-page">
      <header className="admin-header">
        <span className="auth-logo" style={{ width: 28, height: 28, fontSize: 15 }} aria-hidden>
          ✦
        </span>
        <span className="admin-header-title">系统管理后台</span>
        <span className="admin-tag">{user?.username ?? "admin"} · 管理员</span>
        <div className="admin-header-actions">
          <ThemeToggle />
          <Link href="/" className="btn btn-ghost" style={{ padding: "8px 14px" }}>
            返回对话
          </Link>
          <button
            type="button"
            className="btn btn-ghost"
            style={{ padding: "8px 14px" }}
            onClick={logout}
          >
            退出
          </button>
        </div>
      </header>

      <div className="admin-body">
        {notice ? <div className={`alert alert-${notice.kind}`}>{notice.text}</div> : null}

        <form className="admin-card" onSubmit={handleSave}>
          <h2>模型接入</h2>
          <p className="admin-card-desc">
            支持任意 OpenAI 兼容服务（DeepSeek / OpenAI / Kimi / 通义千问 / 智谱 / 本地 Ollama
            等）。服务地址与模型留空表示交还给环境变量、再不成才用厂商预设；API-Key
            加密后入库、页面只回显脱敏值，留空表示保持原值不变。
          </p>

          <div className="admin-grid">
            <div className="field">
              <label htmlFor="provider">模型厂商</label>
              <select
                id="provider"
                className="input"
                value={provider}
                onChange={(e) => handleProviderChange(e.target.value)}
              >
                {catalog.items.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}
                    {item.requires_api_key ? "" : "（无需密钥）"}
                  </option>
                ))}
              </select>
              <span className="field-hint">
                {preset?.hint || "切换厂商后地址与模型会自动回落到新厂商的预设。"}
              </span>
            </div>

            <div className="field">
              <label htmlFor="apiKey">{preset?.label ?? "厂商"} API-Key</label>
              <input
                id="apiKey"
                className="input"
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={
                  config.llm_api_key_configured
                    ? `已配置：${config.llm_api_key}（留空不修改）`
                    : preset && !preset.requires_api_key
                      ? "该厂商无需密钥，可留空"
                      : "尚未配置，请填入对应平台的密钥"
                }
                autoComplete="off"
              />
              {config.llm_api_key_from_legacy ? (
                <span className="field-hint">
                  当前密钥来自旧版 deepseek_api_key 配置，建议在这里重新填写以统一管理。
                </span>
              ) : null}
            </div>

            <div className="field">
              <label htmlFor="model">默认模型</label>
              <input
                id="model"
                className="input"
                list="model-options"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder={preset?.default_model || "模型名"}
              />
              <datalist id="model-options">
                {(preset?.models ?? []).map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
              <span className="field-hint">
                留空跟随环境变量或预设：{preset?.default_model || "需自行填写模型名"}
              </span>
            </div>

            <div className="field">
              <label htmlFor="baseUrl">服务地址</label>
              <input
                id="baseUrl"
                className="input"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder={preset?.base_url || "https://example.com/v1"}
              />
              <span className="field-hint">
                留空跟随环境变量或预设：{preset?.base_url || "自建网关请填完整地址"}
              </span>
            </div>

            <div className="field">
              <label htmlFor="maxSteps">最大推理轮次</label>
              <input
                id="maxSteps"
                className="input"
                type="number"
                min={1}
                max={50}
                value={maxSteps}
                onChange={(e) => setMaxSteps(e.target.value)}
              />
            </div>

            <div className="field">
              <label htmlFor="toolTimeout">工具超时（秒）</label>
              <input
                id="toolTimeout"
                className="input"
                type="number"
                min={1}
                max={300}
                step="0.5"
                value={toolTimeout}
                onChange={(e) => setToolTimeout(e.target.value)}
              />
            </div>
          </div>

          <div className="admin-actions">
            <span className="admin-actions-left">
              {config.effective_provider_label
                ? `当前生效：${config.effective_provider_label} · ${config.effective_model} · ${config.effective_base_url}`
                : "尚未完成接入配置（自定义厂商需填写服务地址与模型）"}
              <br />
              {config.updated_at
                ? `最近更新：${new Date(config.updated_at).toLocaleString()}`
                : "尚未在后台修改过，当前使用环境变量默认值"}
            </span>
            {config.llm_api_key_configured ? (
              <button
                type="button"
                className="btn btn-ghost"
                disabled={busy}
                onClick={() =>
                  void saveConfig({ llm_api_key: "" }, "已清除密钥，将回落到环境变量与预设")
                }
              >
                清除密钥
              </button>
            ) : null}
            <button className="btn" type="submit" disabled={busy}>
              {busy ? "保存中…" : "保存配置"}
            </button>
          </div>
        </form>

        <form className="admin-card" onSubmit={handleSaveSearch}>
          <h2>联网搜索</h2>
          <p className="admin-card-desc">
            web_search 工具的搜索源。默认「自动」会依次尝试免密钥后端（Bing → DuckDuckGo），
            任一成功即返回；也可接入 Tavily / Serper / Brave（需 API-Key）或自建 SearXNG。
            密钥加密后入库、页面只回显脱敏值，留空表示保持原值不变。
          </p>

          <div className="admin-grid">
            <div className="field">
              <label htmlFor="searchProvider">搜索后端</label>
              <select
                id="searchProvider"
                className="input"
                value={searchProvider}
                onChange={(e) => handleSearchProviderChange(e.target.value)}
              >
                {searchCatalog.items.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}
                    {item.requires_api_key ? "（需密钥）" : ""}
                  </option>
                ))}
              </select>
              <span className="field-hint">{searchPreset?.hint || "选择搜索服务来源"}</span>
            </div>

            <div className="field">
              <label htmlFor="searchApiKey">API-Key</label>
              <input
                id="searchApiKey"
                className="input"
                type="password"
                autoComplete="off"
                value={searchApiKey}
                onChange={(e) => setSearchApiKey(e.target.value)}
                placeholder={
                  searchPreset?.requires_api_key
                    ? searchPreset.key_hint || "填写密钥"
                    : "该后端无需密钥"
                }
              />
              <span className="field-hint">
                {config.search_api_key_configured
                  ? `已配置：${config.search_api_key}（留空表示不修改）`
                  : "尚未配置"}
              </span>
            </div>

            <div className="field">
              <label htmlFor="searchBaseUrl">服务地址</label>
              <input
                id="searchBaseUrl"
                className="input"
                value={searchBaseUrl}
                onChange={(e) => setSearchBaseUrl(e.target.value)}
                placeholder={searchPreset?.base_url || "留空即可"}
              />
              <span className="field-hint">{searchBaseUrlHint(searchPreset)}</span>
            </div>

            <div className="field">
              <label htmlFor="searchMaxResults">每次检索条数</label>
              <input
                id="searchMaxResults"
                className="input"
                type="number"
                min={1}
                max={20}
                value={searchMaxResults}
                onChange={(e) => setSearchMaxResults(e.target.value)}
              />
              <span className="field-hint">默认 5；条数越多越占上下文预算</span>
            </div>
          </div>

          <div className="admin-actions">
            <span className="admin-actions-left">
              {config.effective_search_chain.length
                ? `当前生效：${config.effective_search_chain.join(" → ")}`
                : "尚未就绪：请选择搜索后端，或补全密钥 / 实例地址"}
            </span>
            {config.search_api_key_configured ? (
              <button
                type="button"
                className="btn btn-ghost"
                disabled={busy}
                onClick={() =>
                  void saveConfig({ search_api_key: "" }, "已清除搜索密钥，将回落到环境变量与预设")
                }
              >
                清除密钥
              </button>
            ) : null}
            <button
              type="button"
              className="btn btn-ghost"
              disabled={testing || busy || searchCatalog.items.length === 0}
              onClick={() => void handleTestSearch()}
            >
              {testing ? "测试中…" : "测试搜索"}
            </button>
            <button className="btn" type="submit" disabled={busy}>
              {busy ? "保存中…" : "保存配置"}
            </button>
          </div>

          {searchProbe ? (
            <div className="probe-list">
              <span className="probe-title">
                测试「{searchProbe.query}」：
                {searchProbe.ok
                  ? "至少一个后端可用"
                  : searchProbe.message || "全部后端不可用，请换后端或检查网络"}
              </span>
              {searchProbe.results.map((item) => (
                <div
                  key={item.provider}
                  className={`probe-item ${item.ok ? "probe-ok" : "probe-fail"}`}
                >
                  <span className="probe-name">
                    {item.ok ? "✓" : "✕"} {item.label}
                  </span>
                  <span className="probe-detail">
                    {item.detail} · {item.took_ms}ms
                    {item.sample.length ? ` · ${item.sample.join(" / ")}` : ""}
                  </span>
                </div>
              ))}
            </div>
          ) : null}
        </form>

        <section className="admin-card">
          <h2>沙箱全局开关</h2>
          <p className="admin-card-desc">
            全局关闭后，所有会话的代码执行能力都会被强制停用，优先级高于用户侧开关。
          </p>
          <div className="admin-switch-row">
            <span className="admin-switch-meta">
              <span>允许执行代码</span>
              <span>{config.sandbox_global_enabled ? "当前：全局开启" : "当前：全局关闭"}</span>
            </span>
            <button
              type="button"
              className={`switch ${config.sandbox_global_enabled ? "on" : ""}`}
              onClick={() => void handleToggleSandbox(!config.sandbox_global_enabled)}
              disabled={busy}
              aria-pressed={config.sandbox_global_enabled}
              aria-label="切换沙箱全局开关"
            />
          </div>
        </section>

        <section className="admin-card">
          <h2>用户列表</h2>
          <p className="admin-card-desc">平台共 {users.total} 位用户。</p>
          <table className="admin-table">
            <thead>
              <tr>
                <th>用户名</th>
                <th>邮箱</th>
                <th>角色</th>
                <th>状态</th>
                <th>注册时间</th>
              </tr>
            </thead>
            <tbody>
              {users.items.map((item) => (
                <tr key={item.id}>
                  <td>{item.username}</td>
                  <td className="admin-mono">{item.email}</td>
                  <td>
                    <span className={`admin-badge ${item.role === "admin" ? "is-admin" : ""}`}>
                      {item.role === "admin" ? "管理员" : "普通用户"}
                    </span>
                  </td>
                  <td>
                    <span className={`admin-badge ${item.status === "active" ? "is-active" : ""}`}>
                      {item.status}
                    </span>
                  </td>
                  <td className="admin-mono">{new Date(item.created_at).toLocaleDateString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  );
}
