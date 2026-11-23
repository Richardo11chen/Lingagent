"""四个搜索源的适配器。

放一个文件里是因为它们形状高度相似（一次 HTTP、一层 JSON、映射成 `Hit`），
并排看比翻四个文件快。每个类只做一件事：**把某家的响应翻译成 `list[Hit]`**。

字段名都做了容错（`results` / `items`、`summary` / `snippet` / `content`）：
这类 API 改字段名是常事，而失败方式很隐蔽——不是报错，是"搜到了但全是空字符串"。
"""

import os

import httpx

from .base import Hit, NotSupported, SearchError, raise_for

_TIMEOUT = 20.0


def _s(value) -> str:
    return "" if value is None else str(value)


class DeepSeek:
    """DeepSeek 的 Search / Fetch API（复用模型那把 key，不额外配置）。"""

    name = "deepseek"
    supports_fetch = True

    def __init__(self, base_url: str, api_key: str, timeout: float = _TIMEOUT):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout

    def _post(self, path: str, payload: dict, what: str) -> dict:
        resp = httpx.post(
            f"{self._base}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=self._timeout,
        )
        raise_for(resp, what)
        return resp.json()

    def search(self, query: str, n: int) -> list[Hit]:
        data = self._post("/v1/tools/search", {"query": query, "count": n}, "搜索")
        rows = data.get("results") or data.get("items") or []
        return [
            Hit(
                title=_s(r.get("title")),
                url=_s(r.get("url")),
                snippet=_s(r.get("summary") or r.get("snippet") or r.get("content")),
            )
            for r in rows
        ][:n]

    def fetch(self, url: str) -> str:
        data = self._post("/v1/tools/fetch", {"url": url}, "抓取")
        return _s(data.get("content") or data.get("markdown") or data.get("text"))


class Tavily:
    """Tavily：搜索质量稳定，自带 extract（能直接拿正文）。"""

    name = "tavily"
    supports_fetch = True

    def __init__(self, api_key: str, timeout: float = _TIMEOUT):
        self._key = api_key
        self._timeout = timeout
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def search(self, query: str, n: int) -> list[Hit]:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={"query": query, "max_results": n, "search_depth": "basic"},
            headers=self._headers,
            timeout=self._timeout,
        )
        raise_for(resp, "搜索")
        return [
            Hit(_s(r.get("title")), _s(r.get("url")), _s(r.get("content")))
            for r in (resp.json().get("results") or [])
        ][:n]

    def fetch(self, url: str) -> str:
        resp = httpx.post(
            "https://api.tavily.com/extract",
            json={"urls": [url]},
            headers=self._headers,
            timeout=self._timeout,
        )
        raise_for(resp, "抓取")
        rows = resp.json().get("results") or []
        if not rows:
            raise NotSupported("Tavily 没抓到正文，改用内置抓取器。")
        return _s(rows[0].get("raw_content"))


class Brave:
    """Brave Search：只给摘要，正文走内置抓取器。"""

    name = "brave"
    supports_fetch = False

    def __init__(self, api_key: str, timeout: float = _TIMEOUT):
        self._key = api_key
        self._timeout = timeout

    def search(self, query: str, n: int) -> list[Hit]:
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": n},
            headers={"X-Subscription-Token": self._key, "Accept": "application/json"},
            timeout=self._timeout,
        )
        raise_for(resp, "搜索")
        rows = (resp.json().get("web") or {}).get("results") or []
        return [
            Hit(_s(r.get("title")), _s(r.get("url")), _s(r.get("description")))
            for r in rows
        ][:n]

    def fetch(self, url: str) -> str:
        raise NotSupported("Brave 不提供抓取。")


class SearXNG:
    """自建 SearXNG：不需要 key，适合内网或不想外发查询的场景。"""

    name = "searxng"
    supports_fetch = False

    def __init__(self, base_url: str, timeout: float = _TIMEOUT):
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def search(self, query: str, n: int) -> list[Hit]:
        resp = httpx.get(
            f"{self._base}/search",
            params={"q": query, "format": "json"},
            timeout=self._timeout,
        )
        raise_for(resp, "搜索")
        rows = resp.json().get("results") or []
        return [
            Hit(_s(r.get("title")), _s(r.get("url")), _s(r.get("content")))
            for r in rows
        ][:n]

    def fetch(self, url: str) -> str:
        raise NotSupported("SearXNG 不提供抓取。")


class AnthropicServerTool:
    """借 Anthropic Messages 协议的 server tool 搜索（官方、或会说这套协议的网关都行）。

    为什么不是"调 Anthropic 的搜索接口"：**这个协议里没有那个接口**。
    `web_search_20250305` 是 `tools` 数组里的一个**类型名**——声明了它，服务端就自己执行搜索，
    再按 `server_tool_use → web_search_tool_result → text` 的顺序把结果塞回响应流。
    谁实现了这个类型名，谁就是服务端，和 host 无关。

    于是同一个 base_url，换一种协议去问，本地网关（magpie 这类中转）也能给出搜索结果，
    不用额外 key、不用直连官方。这也是这个类存在的唯一理由：**有些中转只认这个协议**。

    已知边界（实测）：`encrypted_content` 基本是空的，所以做不了"引用上一轮的结果编号"
    那种多轮校验——想要就每轮重搜。
    """

    name = "anthropic"
    supports_fetch = False  # 正文交给内置抓取器

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        # OpenAI 兼容配置里的 base_url 一般已经带 /v1，追加 /messages 正好。
        # 别再补一层 /v1——那会变成 /v1/v1/messages（这个项目在 deepseek 那个 provider 上踩过）。
        self._url = base_url.rstrip("/") + "/messages"
        self._key = api_key
        self._model = model
        self._timeout = timeout

    def search(self, query: str, n: int) -> list[Hit]:
        payload = {
            # 必须是配置里的模型名。网关只认它自己那套名字，写别的直接 404。
            "model": self._model,
            "max_tokens": 512,  # 只要结果块，正文是白花的
            "tools": [{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": max(1, n),
            }],
            "messages": [{
                "role": "user",
                "content": [{"type": "text", "text": f"Perform a web search for the query: {query}"}],
            }],
        }
        headers = {
            "anthropic-version": "2023-06-01",
            "x-api-key": self._key,
            "Authorization": f"Bearer {self._key}",  # 网关普遍只认这个，两个都带
        }
        try:
            resp = httpx.post(self._url, json=payload, headers=headers, timeout=self._timeout)
        except Exception as e:
            raise SearchError(f"搜索失败：连不上 {self._url}（{type(e).__name__}）。") from e
        if resp.status_code == 404:
            raise SearchError(
                f"搜索失败：{self._url} 不存在（HTTP 404）——"
                "这个端点大概不是 Anthropic 协议的，改用 [search] provider = \"tavily\"。"
            )
        raise_for(resp, "搜索")

        hits = _parse_server_tool(resp.json(), n)
        if hits is None:
            # 这类网关会**静默忽略**不认识的工具类型：200 但响应里什么都没有。
            # 光看配置看不出来，所以这里必须说清楚，别让它表现成"搜到了 0 条"。
            raise SearchError(
                f"{self._url} 没有执行 web_search_20250305（响应里没有结果块）。"
                "常见原因：这个端点不支持 server tool，或者 model 名不是网关认的那个。"
                '退路：把 [search] provider 换成 "tavily"（需要 TAVILY_API_KEY）。'
            )
        return hits

    def fetch(self, url: str) -> str:
        raise NotSupported("Anthropic server tool 只负责搜索，正文用内置抓取器。")


def _parse_server_tool(data: dict, n: int) -> list[Hit] | None:
    """从 Messages 响应里挑出 `web_search_tool_result`，翻成 `Hit`。

    返回 None = **没有这个块**（端点不支持）；返回 [] = 有块但真的没结果。
    这两种必须分开：合成一个就等于把"配置错了"伪装成"网上没有"。
    """
    blocks = data.get("content")
    if not isinstance(blocks, list):
        return None
    found = False
    hits: list[Hit] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "web_search_tool_result":
            continue
        found = True
        rows = block.get("content")
        if isinstance(rows, dict):  # 出错时这里是个对象，不是数组
            code = _s(rows.get("error_code") or rows.get("type"))
            raise SearchError(f"搜索被服务端拒绝：{code}")
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict):
                continue
            hits.append(Hit(_s(r.get("title")), _s(r.get("url")), _s(r.get("snippet"))))
    return hits[:n] if found else None


def pick_key(api_key_env: str, default_env: str) -> str:
    """按「显式指定的环境变量 → provider 的约定变量」取 key。"""
    env = api_key_env or default_env
    return os.environ.get(env, "")


def make(kind: str, *, base_url: str, api_key: str, searxng_url: str, api_key_env: str,
         model: str = ""):
    """按名字造 provider；缺配置就抛 SearchError（调用方决定降级还是报错）。"""
    if kind == "deepseek":
        if not api_key:
            raise SearchError("搜索没启用：缺 DeepSeek 的 API key。")
        return DeepSeek(base_url, api_key)
    if kind == "anthropic":
        # 复用当前模型那把 key 和那个模型名——server tool 不需要另配
        if not api_key:
            raise SearchError("搜索没启用：缺 API key（这个 provider 复用当前模型的 key）。")
        if not model:
            raise SearchError("搜索没启用：不知道用哪个模型名（[models.*] 的 name 是空的）。")
        return AnthropicServerTool(base_url, api_key, model)
    if kind == "tavily":
        key = pick_key(api_key_env, "TAVILY_API_KEY")
        if not key:
            raise SearchError("搜索没启用：环境变量 TAVILY_API_KEY 没设置。")
        return Tavily(key)
    if kind == "brave":
        key = pick_key(api_key_env, "BRAVE_API_KEY")
        if not key:
            raise SearchError("搜索没启用：环境变量 BRAVE_API_KEY 没设置。")
        return Brave(key)
    if kind == "searxng":
        if not searxng_url:
            raise SearchError("搜索没启用：[search] searxng_url 没写。")
        return SearXNG(searxng_url)
    raise SearchError(f"未知的搜索 provider：{kind}")
