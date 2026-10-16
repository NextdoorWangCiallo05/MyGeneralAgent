"""联网搜索模块的专项测试。

分四层覆盖：
- 解析层：预设、「自动」链顺序、显式后端、非法后端、地址优先级；
- 后端层：结果页解析（Bing / DuckDuckGo）与 API 请求组装（Tavily / Serper / Brave / SearXNG）；
- 工具层：结果渲染、自动回退、全部失败时的下一步指引；
- 接口层：`/admin/search/providers`、`/admin/search/test`、搜索配置的保存与生效视图。
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.admin as admin_api
import app.search.backends.api as api_backends
import app.search.backends.bing as bing_backend
import app.search.backends.duckduckgo as ddg_backend
import app.tools.web_search as web_tool
from app.config import Settings, get_settings
from app.core.crypto import decrypt_value, is_encrypted
from app.core.runtime_config import RuntimeConfigService, reset_runtime_config
from app.main import app
from app.search import (
    PRESETS,
    SearchError,
    SearchNotConfiguredError,
    SearchResult,
    build_search_backend,
    build_search_chain,
    resolve_search_config,
)
from app.search.backends import (
    BingBackend,
    BraveBackend,
    DuckDuckGoBackend,
    SearxngBackend,
    SerperBackend,
    TavilyBackend,
)
from app.store import MemoryStore, reset_store

SECRET = "tvly-live-1234567890"
PASSWORD = "secret123"
ADMIN_EMAIL = "root@example.com"


def _settings(**overrides: Any) -> Settings:
    """构造一份只用于解析的 Settings：不读 .env，避免受开发者本机配置影响。"""
    base: dict[str, Any] = {
        "search_provider": "auto",
        "search_api_key": "",
        "search_base_url": "",
        "search_locale": "zh-CN",
        "search_timeout": 5.0,
        "web_search_max_results": 5,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeBackend:
    """按脚本返回结果的搜索后端替身。"""

    def __init__(
        self,
        backend_id: str,
        label: str,
        *,
        results: list[SearchResult] | None = None,
        error: str = "",
    ) -> None:
        self.id = backend_id
        self.label = label
        self._results = results or []
        self._error = error
        self.calls = 0

    async def search(self, query: str, *, limit: int) -> list[SearchResult]:
        self.calls += 1
        if self._error:
            raise SearchError(self._error)
        return self._results[:limit]


def _result(title: str, url: str = "https://example.com/a", snippet: str = "摘要") -> SearchResult:
    return SearchResult(title=title, url=url, snippet=snippet, source="替身后端")


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """每个用例使用全新的内存存储与配置缓存。"""
    reset_store()
    reset_runtime_config()
    monkeypatch.setattr(get_settings(), "search_api_key", "")
    monkeypatch.setattr(get_settings(), "search_provider", "auto")
    yield
    reset_store()
    reset_runtime_config()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """启动应用并借助 `ADMIN_EMAIL/ADMIN_PASSWORD` 播种初始管理员。"""
    monkeypatch.setenv("ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("ADMIN_USERNAME", "root")
    get_settings.cache_clear()
    reset_runtime_config()

    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()
    reset_runtime_config()


@pytest.fixture
def admin_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"username": ADMIN_EMAIL, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# ---------------------------------------------------------------- 解析层
def test_default_provider_is_auto_and_lists_free_backends() -> None:
    """默认「自动」：不强求密钥，链上的都是免密钥后端，且顺序确定。"""
    config = resolve_search_config(_settings())

    assert config.provider == "auto"
    assert config.requires_api_key is False
    assert [backend.id for backend in build_search_chain(config)] == ["bing", "duckduckgo"]


def test_explicit_provider_builds_single_backend() -> None:
    """指定后端时只尝试它自己，失败即失败，便于管理员定位问题。"""
    config = resolve_search_config(_settings(search_provider="serper", search_api_key=SECRET))

    chain = build_search_chain(config)
    assert [backend.id for backend in chain] == ["serper"]
    assert chain[0].requires_api_key is True


@pytest.mark.parametrize("preset", [item for item in PRESETS if item.id != "auto"])
def test_preset_alone_is_enough_to_resolve(preset: Any) -> None:
    """只选后端（不填地址/密钥）就能得到一份确定的接入参数。"""
    config = resolve_search_config(_settings(search_provider=preset.id))

    assert config.provider == preset.id
    assert config.label == preset.label
    assert config.base_url == preset.base_url.rstrip("/")
    assert config.requires_api_key is preset.requires_api_key
    # 工厂分支：预设 id 能直接构造出对应后端，且展示名与预设一致（后台 / 工具结果都靠它）
    backend = build_search_backend(config)
    assert backend.id == preset.id
    assert backend.label == preset.label


def test_runtime_base_url_overrides_preset() -> None:
    config = resolve_search_config(
        _settings(search_provider="searxng", search_base_url="http://127.0.0.1:8080/")
    )
    assert config.base_url == "http://127.0.0.1:8080"


def test_unknown_provider_is_rejected_with_options() -> None:
    with pytest.raises(SearchNotConfiguredError) as excinfo:
        resolve_search_config(_settings(search_provider="yahoo"))

    message = str(excinfo.value)
    assert "yahoo" in message
    assert "bing" in message and "tavily" in message


# ---------------------------------------------------------------- 后端层
def test_bing_parse_extracts_title_url_and_snippet() -> None:
    markup = """
    <html><body>
      <li class="b_algo" data-id iid=SERP.1>
        <h2><a href="https://fastapi.tiangolo.com/">FastAPI <strong>官方</strong></a></h2>
        <div class="b_caption"><p class="b_lineclamp2">高性能 Python Web 框架</p></div>
      </li>
      <li class="b_algo">
        <h2><a href="https://example.com/b">示例 B</a></h2>
      </li>
      <li class="b_ans">没有标题链接的块</li>
    </body></html>
    """
    results = bing_backend.parse_results(markup, limit=5)

    assert [item.title for item in results] == ["FastAPI 官方", "示例 B"]
    assert results[0].url == "https://fastapi.tiangolo.com/"
    assert results[0].snippet == "高性能 Python Web 框架"
    assert results[1].snippet == ""


def test_bing_parse_decodes_redirect_links() -> None:
    target = "https://example.com/ck-target"
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    markup = (
        '<li class="b_algo"><h2>'
        f'<a href="https://www.bing.com/ck/a?!&&p=abc&u=a1{encoded}&ntb=1">带跳转的结果</a>'
        "</h2></li>"
    )

    results = bing_backend.parse_results(markup, limit=3)

    assert len(results) == 1
    assert results[0].url == target


def test_bing_parse_respects_limit() -> None:
    markup = "".join(
        f'<li class="b_algo"><h2><a href="https://example.com/{i}">标题{i}</a></h2></li>'
        for i in range(8)
    )
    assert len(bing_backend.parse_results(markup, limit=3)) == 3


def test_bing_backend_reports_unparsable_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """页面拿不到结果时给出可读原因，而不是返回空列表。"""

    async def fake_request_text(*_args: Any, **_kwargs: Any) -> str:
        return "<html><body>验证码</body></html>"

    monkeypatch.setattr(bing_backend, "request_text", fake_request_text)

    with pytest.raises(SearchError) as excinfo:
        _run(BingBackend().search("测试", limit=3))
    assert "解析" in str(excinfo.value)


def test_duckduckgo_backend_uses_post_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """DDG Lite 需要 POST 表单，链路正确即可（解析已有独立用例）。"""
    captured: dict[str, Any] = {}

    async def fake_request_text(url: str, **kwargs: Any) -> str:
        captured["url"] = url
        captured.update(kwargs)
        return '<a class="result-link" href="https://example.com/a">示例</a>'

    monkeypatch.setattr(ddg_backend, "request_text", fake_request_text)

    results = _run(DuckDuckGoBackend().search("测试", limit=3))

    assert captured["method"] == "POST"
    assert captured["data"] == {"q": "测试"}
    assert results[0].source == "DuckDuckGo（Lite）"


def test_api_backends_require_key() -> None:
    with pytest.raises(SearchNotConfiguredError):
        _run(TavilyBackend(api_key="").search("测试", limit=3))
    with pytest.raises(SearchNotConfiguredError):
        _run(SerperBackend(api_key="").search("测试", limit=3))
    with pytest.raises(SearchNotConfiguredError):
        _run(BraveBackend(api_key="").search("测试", limit=3))


def test_searxng_requires_instance_url() -> None:
    with pytest.raises(SearchNotConfiguredError) as excinfo:
        _run(SearxngBackend(base_url="").search("测试", limit=3))
    assert "实例地址" in str(excinfo.value)


def test_tavily_request_and_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_request_json(url: str, **kwargs: Any) -> dict[str, Any]:
        captured["url"] = url
        captured.update(kwargs)
        return {
            "results": [
                {"title": "T1", "url": "https://t.example.com/1", "content": "正文 片段"},
                {"title": "T2", "url": "not-a-url", "content": "应被过滤"},
            ]
        }

    monkeypatch.setattr(api_backends, "request_json", fake_request_json)

    results = _run(TavilyBackend(api_key=SECRET).search("FastAPI", limit=5))

    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert captured["payload"]["query"] == "FastAPI"
    assert captured["payload"]["max_results"] == 5
    assert [item.title for item in results] == ["T1"]
    assert results[0].snippet == "正文 片段"
    assert results[0].source == "Tavily"


def test_serper_and_brave_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    async def fake_request_json(url: str, **kwargs: Any) -> dict[str, Any]:
        captured.append({"url": url, **kwargs})
        if "serper" in url:
            return {"organic": [{"title": "S", "link": "https://s.example.com", "snippet": "摘要"}]}
        return {
            "web": {
                "results": [
                    {"title": "B", "url": "https://b.example.com", "description": "<strong>加粗</strong> 描述"}
                ]
            }
        }

    monkeypatch.setattr(api_backends, "request_json", fake_request_json)

    serper_results = _run(SerperBackend(api_key=SECRET).search("q", limit=4))
    brave_results = _run(BraveBackend(api_key=SECRET).search("q", limit=4))

    assert captured[0]["headers"]["X-API-KEY"] == SECRET
    assert captured[0]["payload"]["num"] == 4
    assert serper_results[0].url == "https://s.example.com"
    assert captured[1]["headers"]["X-Subscription-Token"] == SECRET
    assert captured[1]["params"] == {"q": "q", "count": 4}
    assert brave_results[0].snippet == "加粗 描述"


def test_searxng_uses_json_format(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_request_json(url: str, **kwargs: Any) -> dict[str, Any]:
        captured["url"] = url
        captured.update(kwargs)
        return {"results": [{"title": "X", "url": "https://x.example.com", "content": "内容"}]}

    monkeypatch.setattr(api_backends, "request_json", fake_request_json)

    results = _run(SearxngBackend(base_url="http://searx.local").search("q", limit=3))

    assert captured["url"] == "http://searx.local/search"
    assert captured["params"]["format"] == "json"
    assert results[0].url == "https://x.example.com"


# ---------------------------------------------------------------- 工具层
def test_web_search_renders_results_with_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功时文本含编号、标题、链接、摘要与来源，data 供审计回溯。"""
    backend = FakeBackend(
        "bing", "Bing 网页搜索", results=[_result("FastAPI 文档", "https://fastapi.tiangolo.com/")]
    )
    monkeypatch.setattr(web_tool, "build_search_chain", lambda _config: [backend])

    result = _run(web_tool._web_search("FastAPI 文档"))

    assert result.status == "success"
    assert "Bing 网页搜索" in result.summary
    assert "1. FastAPI 文档" in result.content
    assert "https://fastapi.tiangolo.com/" in result.content
    assert "引用对应链接" in result.content
    assert result.data["backend"] == "bing"
    assert result.data["results"][0]["title"] == "FastAPI 文档"


def test_web_search_falls_back_to_next_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """第一个后端失败时自动改用下一个，并在结果里说明谁不可用。"""
    broken = FakeBackend("bing", "Bing 网页搜索", error="网络不可达")
    healthy = FakeBackend("duckduckgo", "DuckDuckGo（Lite）", results=[_result("备用结果")])
    monkeypatch.setattr(web_tool, "build_search_chain", lambda _config: [broken, healthy])

    result = _run(web_tool._web_search("关键词"))

    assert result.status == "success"
    assert broken.calls == 1 and healthy.calls == 1
    assert "备用结果" in result.content
    assert "已自动改用可用的后端" in result.content
    assert result.data["backend"] == "duckduckgo"


def test_web_search_all_backends_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """全部失败时报错，并给出「换关键词 / 换后端 / 直接抓链接」的下一步。"""
    broken = FakeBackend("bing", "Bing 网页搜索", error="请求超时")
    monkeypatch.setattr(web_tool, "build_search_chain", lambda _config: [broken])

    result = _run(web_tool._web_search("关键词"))

    assert result.status == "error"
    assert "请求超时" in result.content
    assert "搜索后端" in result.content and "fetch_url" in result.content


def test_web_search_reports_unconfigured_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolve(_settings: Any) -> Any:
        raise SearchNotConfiguredError("Tavily 未配置 API-Key")

    monkeypatch.setattr(web_tool, "resolve_search_config", fake_resolve)

    result = _run(web_tool._web_search("关键词"))

    assert result.status == "error"
    assert "Tavily 未配置 API-Key" in result.content
    assert "搜索后端" in result.content


def test_web_search_rejects_blank_query() -> None:
    result = _run(web_tool._web_search("   "))
    assert result.status == "error"
    assert "不能为空" in result.content


# ---------------------------------------------------------------- 接口层
def test_search_providers_endpoint_lists_presets(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.get("/api/v1/admin/search/providers", headers=admin_headers)

    assert response.status_code == 200
    payload = response.json()
    ids = [item["id"] for item in payload["items"]]
    assert payload["total"] == len(PRESETS)
    assert ids[:3] == ["auto", "bing", "duckduckgo"]
    assert payload["active"] == "auto"

    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id["bing"]["requires_api_key"] is False
    assert by_id["tavily"]["requires_api_key"] is True
    assert by_id["bing"]["auto"] is True


def test_search_providers_endpoint_requires_admin(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    assert client.get("/api/v1/admin/search/providers").status_code == 401
    assert (
        client.post("/api/v1/admin/search/test", json={"query": "x"}).status_code == 401
    )


def test_admin_can_switch_search_provider(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """切到 Tavily 并填密钥后，「当前生效」立即反映出来，密钥只回显脱敏值。"""
    response = client.put(
        "/api/v1/admin/config",
        json={"search_provider": "tavily", "search_api_key": SECRET, "web_search_max_results": 8},
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    view = client.get("/api/v1/admin/config", headers=admin_headers).json()
    assert view["search_provider"] == "tavily"
    assert view["search_api_key_configured"] is True
    assert SECRET not in json.dumps(view)
    assert "*" in view["search_api_key"]
    assert view["effective_search_provider"] == "tavily"
    assert view["effective_search_label"] == "Tavily"
    assert view["effective_search_base_url"] == "https://api.tavily.com"
    assert view["effective_search_chain"] == ["Tavily"]
    assert view["web_search_max_results"] == 8


def test_search_secret_is_encrypted_at_rest() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        service = RuntimeConfigService(store=store, settings=get_settings())

        await service.update({"search_api_key": SECRET}, updated_by="admin-1")

        item = await store.get_config("search_api_key")
        assert item is not None and item.encrypted is True
        assert SECRET not in item.value
        assert decrypt_value(item.value) == SECRET
        assert is_encrypted(item.value)

    _run(scenario())


def test_unknown_search_provider_is_rejected(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.put(
        "/api/v1/admin/config", json={"search_provider": "yahoo"}, headers=admin_headers
    )
    assert response.status_code == 422
    assert "yahoo" in response.text


def test_search_test_endpoint_probes_each_backend(
    client: TestClient, admin_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """连通性测试逐个探测后端，分别回报条数、耗时与失败原因。"""
    healthy = FakeBackend("bing", "Bing 网页搜索", results=[_result("命中标题")])
    broken = FakeBackend("duckduckgo", "DuckDuckGo（Lite）", error="网络不可达（可能被墙）")
    monkeypatch.setattr(admin_api, "build_search_chain", lambda _config: [healthy, broken])

    response = client.post(
        "/api/v1/admin/search/test", json={"query": "FastAPI"}, headers=admin_headers
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["query"] == "FastAPI"
    probes = {item["provider"]: item for item in payload["results"]}
    assert probes["bing"]["ok"] is True
    assert probes["bing"]["sample"] == ["命中标题"]
    assert probes["duckduckgo"]["ok"] is False
    assert "网络不可达" in probes["duckduckgo"]["detail"]


def test_search_test_endpoint_rejects_unknown_provider_override(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/admin/search/test", json={"provider": "yahoo"}, headers=admin_headers
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert "yahoo" in payload["message"]
