"""联网搜索：抓取器（SSRF / HTML→文本）、provider 解析、工具输出格式。

网络一律不碰——provider 的 HTTP 用 monkeypatch 换掉，内置抓取器只测纯函数部分。
理由和别处一样：测的是"我们的翻译对不对"，不是"对方服务在不在"。
"""

import pytest

from agent_harness.settings import SearchConfig
from agent_harness.websearch import build_provider, make_tools
from agent_harness.websearch.base import Hit
from agent_harness.websearch.fetch import SSRFBlocked, assert_public, html_to_text
from agent_harness.websearch.providers import Brave, DeepSeek, SearXNG, Tavily

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


def _offline(monkeypatch):
    """把 HTTP 换成"一律 404"，并清掉自动探测的缓存。

    `auto` 现在是**真发请求**去问端点会什么，测试里必须挡掉——否则每跑一次测试
    就在打 api.openai.com（每条 3 秒超时，还会失败）。
    """
    import agent_harness.websearch as w
    import agent_harness.websearch.providers as p

    w._AUTO_CACHE.clear()
    monkeypatch.setattr(p.httpx, "post", lambda *a, **kw: _resp({}, status=404))
    monkeypatch.setattr(p.httpx, "get", lambda *a, **kw: _resp({}, status=404))


def test_auto_trusts_the_official_deepseek_domain_without_probing(monkeypatch):
    """官方域名直接认，不发探测请求——省一次，也别往真金白银的接口上打试探。"""
    import agent_harness.websearch as w
    import agent_harness.websearch.providers as p

    w._AUTO_CACHE.clear()
    calls = []
    monkeypatch.setattr(p.httpx, "post", lambda *a, **kw: calls.append(1) or _resp({}, 404))

    assert build_provider(SearchConfig(), _Profile()).name == "deepseek"
    assert calls == []


def test_auto_probes_the_endpoint_and_gives_up(monkeypatch):
    """两条路都不通 → 当作没有搜索源（工具不出现），而不是装一个必然失败的。"""
    _offline(monkeypatch)
    other = _Profile(base_url="https://api.openai.com", api_key="", api_key_env="")
    assert build_provider(SearchConfig(), other) is None


def test_auto_only_tries_the_rest_search_endpoint(monkeypatch):
    """`auto` 只试真·搜索接口，不借任何一家的 server tool。

    "借协议"那条路试过：一个"能通"的网关，三种措辞、中英文换来换去，返回结果和
    查询毫无关系（问 CPython 回来知乎气囊/虎扑）。工具层判断不了相关性，而这类
    server tool 的"搜什么"由端点那边的模型决定——不是我们能修的。所以干脆不做。
    """
    import agent_harness.websearch as w
    import agent_harness.websearch.providers as p

    w._AUTO_CACHE.clear()

    def fake_post(url, **kw):
        if url.endswith("/tools/search"):
            return _resp({}, status=404)
        return _resp({"content": [{"type": "web_search_tool_result", "content": [
            {"type": "web_search_result", "title": "t", "url": "u"}]}]})

    monkeypatch.setattr(p.httpx, "post", fake_post)
    prof = _Profile(base_url="http://127.0.0.1:3425/v1", name="workbuddy/x")
    assert build_provider(SearchConfig(), prof) is None


def test_auto_probe_is_cached(monkeypatch):
    """装配每轮都会调 build_provider——不缓存就是每轮两次空请求。"""
    import agent_harness.websearch as w
    import agent_harness.websearch.providers as p

    w._AUTO_CACHE.clear()
    n = []
    monkeypatch.setattr(p.httpx, "post", lambda *a, **kw: n.append(1) or _resp({}, 404))

    prof = _Profile(base_url="https://api.openai.com", api_key="k")
    build_provider(SearchConfig(), prof)
    first = len(n)
    build_provider(SearchConfig(), prof)
    assert first >= 1 and len(n) == first  # 第二次没再发请求


def test_tavily_key_wins_over_probing(monkeypatch):
    """有 tavily key 就直接用：用户既然配了，就该按它算。"""
    _offline(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    other = _Profile(base_url="https://api.openai.com", api_key="")
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


# ---- 小工具 ----

def _resp(payload, status=200):
    class R:
        status_code = status
        text = ""

        def json(self):
            return payload

    return R()


def test_deepseek_tolerates_a_base_url_ending_with_v1(monkeypatch):
    """配置里写不写 /v1 都该落到同一个路径。

    OpenAI 的习惯是把 /v1 写进 base_url，而路径是死拼的 /v1/tools/search——
    不削掉就会变成 /v1/v1/tools/search，对着网关就是 404。
    """
    import agent_harness.websearch.providers as p

    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        return _resp({"results": []})

    monkeypatch.setattr(p.httpx, "post", fake_post)
    for base in ("https://api.deepseek.com", "https://api.deepseek.com/",
                 "https://api.deepseek.com/v1", "https://api.deepseek.com/v1/"):
        seen.clear()
        DeepSeek(base, "k").search("q", 3)
        assert seen["url"] == "https://api.deepseek.com/v1/tools/search", base
