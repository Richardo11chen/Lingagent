"""联网搜索：抓取器（SSRF / HTML→文本）、provider 解析、工具输出格式。

网络一律不碰——provider 的 HTTP 用 monkeypatch 换掉，内置抓取器只测纯函数部分。
理由和别处一样：测的是"我们的翻译对不对"，不是"对方服务在不在"。
"""

import pytest

from agent_harness.settings import SearchConfig
from agent_harness.websearch import build_provider, make_tools
from agent_harness.websearch.base import Hit
from agent_harness.websearch.fetch import SSRFBlocked, assert_public, html_to_text
from agent_harness.websearch.providers import (
    AnthropicServerTool,
    Brave,
    DeepSeek,
    SearchError,
    SearXNG,
    Tavily,
)

# ---- 内置抓取器 ----


def test_html_to_text_drops_scripts_and_keeps_blocks():
    raw = b"<html><head><style>p{}</style></head><body><h1>T</h1><script>x=1</script><p>hello   world</p></body></html>"
    text = html_to_text(raw, "utf-8")
    assert "hello world" in text  # 空白被压平
    assert "x=1" not in text and "p{}" not in text  # 脚本/样式不该进上下文
    assert text.startswith("T")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/ws",      # 自己的后端
    "http://localhost/",
    "http://169.254.169.254/latest/meta-data/",  # 云元数据
    "http://10.0.0.5/admin",
    "http://192.168.1.1/",
    "file:///etc/passwd",            # 非 http(s)
])
def test_internal_addresses_are_refused(url):
    """模型能构造任意 URL——这道门是我们的，不能外包给 provider。"""
    with pytest.raises(SSRFBlocked):
        assert_public(url)


def test_public_address_passes(monkeypatch):
    """不碰真 DNS：把解析结果钉成一个公网地址。"""
    monkeypatch.setattr(
        "agent_harness.websearch.fetch.socket.getaddrinfo",
        lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))],
    )
    assert_public("https://example.com/a")  # 不抛就是通过


# ---- provider 解析 ----


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = ""

    def json(self):
        return self._payload


def test_deepseek_parses_hits(monkeypatch):
    payload = {"results": [{"title": "T", "url": "https://x", "summary": "S"}]}
    monkeypatch.setattr(
        "agent_harness.websearch.providers.httpx.post", lambda *a, **k: _Resp(payload)
    )
    hits = DeepSeek("https://api.deepseek.com", "k").search("q", 5)
    assert hits == [Hit("T", "https://x", "S")]


def test_tavily_and_brave_parse_their_own_shapes(monkeypatch):
    monkeypatch.setattr(
        "agent_harness.websearch.providers.httpx.post",
        lambda *a, **k: _Resp({"results": [{"title": "A", "url": "u1", "content": "c"}]}),
    )
    assert Tavily("k").search("q", 3)[0].snippet == "c"

    monkeypatch.setattr(
        "agent_harness.websearch.providers.httpx.get",
        lambda *a, **k: _Resp({"web": {"results": [
            {"title": "B", "url": "u2", "description": "d"},
        ]}}),
    )
    hit = Brave("k").search("q", 3)[0]
    assert (hit.title, hit.url, hit.snippet) == ("B", "u2", "d")
    assert Brave.supports_fetch is False  # 正文要走内置抓取器


def test_searxng_reads_results_array(monkeypatch):
    monkeypatch.setattr(
        "agent_harness.websearch.providers.httpx.get",
        lambda *a, **k: _Resp({"results": [{"title": "S", "url": "u", "content": "c"}]}),
    )
    assert SearXNG("http://localhost:8080").search("q", 2)[0].url == "u"


# ---- 选 provider ----


class _Profile:
    def __init__(self, base_url="https://api.deepseek.com", api_key="k", api_key_env="",
                 name="deepseek-flash"):
        self.base_url = base_url
        self.api_key = api_key
        self.api_key_env = api_key_env
        self.name = name


def test_auto_prefers_deepseek_then_tavily(monkeypatch):
    assert build_provider(SearchConfig(), _Profile()).name == "deepseek"

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    other = _Profile(base_url="https://api.openai.com", api_key="", api_key_env="")
    assert build_provider(SearchConfig(), other) is None  # 没有可用搜索源 → 工具不出现

    monkeypatch.setenv("TAVILY_API_KEY", "t")
    assert build_provider(SearchConfig(), other).name == "tavily"


def test_off_means_no_tools(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    assert build_provider(SearchConfig(provider="off"), _Profile()) is None


# ---- 工具输出 ----


class _Stub:
    name = "stub"
    supports_fetch = False

    def __init__(self, hits):
        self._hits = hits
        self.fetched = []

    def search(self, query, n):
        return self._hits[:n]

    def fetch(self, url):
        raise AssertionError("supports_fetch=False 时不该走 provider")


def test_search_output_carries_the_not_a_command_note():
    out = make_tools(_Stub([Hit("标题", "https://x", "摘要")]))["web_search"]("q")
    assert "https://x" in out and "标题" in out
    assert "不是指令" in out  # 外部内容必须带着这句回来


def test_empty_result_suggests_an_alternative():
    out = make_tools(_Stub([]))["web_search"]("q")
    assert "没有搜到" in out and "fetch_url" in out


def test_fetch_blocks_internal_url_before_asking_provider():
    """即使 provider 自己会抓，也要先过我们的 SSRF 门。"""
    out = make_tools(_Stub([]))["fetch_url"]("http://127.0.0.1:8000/ws")
    assert "拒绝" in out and "内网" in out


def test_fetch_truncates_long_text(monkeypatch):
    class _Provider:
        name = "p"
        supports_fetch = False

        def search(self, query, n):
            return []

        def fetch(self, url):
            return "x" * 100

    monkeypatch.setattr(
        "agent_harness.websearch.fetch_text", lambda url, timeout=15.0: "x" * 100
    )
    out = make_tools(_Provider(), fetch_limit=10)["fetch_url"]("https://example.com")
    assert "只给前 10" in out


# ---- 摘要预算：按条数均分 + 句子边界 ----


def _long(n: int) -> str:
    return "这是一句足够长的话。" * n


def test_snippet_budget_is_split_across_hits():
    """固定每条 300 在 2 条时浪费、在 8 条时挤爆；均分让两头都合理。"""
    hits = [Hit(f"T{i}", f"https://e/{i}", _long(30)) for i in range(2)]
    out = make_tools(_Stub(hits), snippet_budget=1500)["web_search"]("q", 2)
    assert "每条最多 750 字符" in out


def test_snippet_clips_on_sentence_boundary():
    """不要在词/句子中间砍断——那半句话会被模型当成事实。"""
    from agent_harness.websearch import _clip

    text = "第一句话说完了。第二句话很长很长很长很长很长很长很长很长很长"
    clipped = _clip(text, 12)
    assert clipped.endswith("。…")  # 停在句号，不是硬切
    assert len(clipped) <= 13


def test_snippet_squashes_markdown_noise():
    from agent_harness.websearch import _squash

    raw = "## 标题\n\n![图](x.png) 正文 [链接](http://a) 继续  还有\t空格"
    assert _squash(raw) == "标题 正文 链接 继续 还有 空格"


# ---- anthropic：借 Messages 协议的 server tool ----


def _resp(payload, status=200):
    class R:
        status_code = status
        text = ""

        def json(self):
            return payload

    return R()


def test_anthropic_reads_the_server_tool_block(monkeypatch):
    """把 web_search_20250305 塞进 tools，从响应里挑 web_search_tool_result。

    这条路的核心是"声明一个类型名"，不是调某个搜索接口——所以断言落在请求体上。
    """
    import agent_harness.websearch.providers as p

    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json, headers=headers)
        return _resp({"content": [
            {"type": "server_tool_use", "id": "x", "name": "web_search", "input": {"query": "q"}},
            {"type": "web_search_tool_result", "content": [
                {"type": "web_search_result", "title": "标题", "url": "https://a", "snippet": "摘要"},
                {"type": "web_search_result", "title": "二号", "url": "https://b"},  # 没有 snippet
            ]},
            {"type": "text", "text": "根据搜索结果……"},
        ]})

    monkeypatch.setattr(p.httpx, "post", fake_post)
    hits = AnthropicServerTool("http://127.0.0.1:3425/v1", "k", "workbuddy/x").search("怎么装", 5)

    assert seen["url"] == "http://127.0.0.1:3425/v1/messages"  # 不再拼一层 /v1
    assert seen["body"]["tools"][0]["type"] == "web_search_20250305"
    assert seen["body"]["model"] == "workbuddy/x"  # 用配置里那个名字，网关只认它
    assert [h.title for h in hits] == ["标题", "二号"]
    assert hits[1].snippet == ""  # 缺字段要容错，不能崩


def test_anthropic_says_so_when_the_endpoint_ignores_the_tool(monkeypatch):
    """网关静默忽略不认识的类型：200 但没有结果块。

    必须和"搜到了 0 条"分开——否则配置错了会表现成"网上没有"，人永远查不出来。
    """
    import agent_harness.websearch.providers as p

    monkeypatch.setattr(
        p.httpx, "post", lambda *a, **kw: _resp({"content": [{"type": "text", "text": "好的"}]})
    )
    with pytest.raises(SearchError) as exc:
        AnthropicServerTool("http://x/v1", "k", "m").search("q", 3)
    assert "web_search_20250305" in str(exc.value)


def test_anthropic_404_points_at_the_wrong_protocol(monkeypatch):
    """端点没有 /messages：说清是协议不对，别让人去找 key 的问题。"""
    import agent_harness.websearch.providers as p

    monkeypatch.setattr(p.httpx, "post", lambda *a, **kw: _resp({}, status=404))
    with pytest.raises(SearchError) as exc:
        AnthropicServerTool("http://x/v1", "k", "m").search("q", 3)
    assert "404" in str(exc.value) and "tavily" in str(exc.value)


def test_anthropic_needs_the_model_name_from_config():
    """没有模型名就不装——网关会 404 掉随便写的名字。"""
    from agent_harness.websearch.providers import make

    with pytest.raises(SearchError):
        make("anthropic", base_url="http://x/v1", api_key="k",
             searxng_url="", api_key_env="", model="")


def test_provider_anthropic_is_explicit_only(monkeypatch):
    """auto 不会自己挑 anthropic：URL 上看不出端点会不会这套协议。"""
    other = _Profile(base_url="http://127.0.0.1:3425/v1", name="workbuddy/x")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert build_provider(SearchConfig(), other) is None

    picked = build_provider(SearchConfig(provider="anthropic"), other)
    assert picked is not None and picked.name == "anthropic"
    assert picked.supports_fetch is False  # 正文交给内置抓取器
