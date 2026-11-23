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
        # base_url 在配置里既可能写成 https://api.deepseek.com，也可能按 OpenAI 习惯带 /v1。
        # 路径是死拼的 /v1/tools/search，所以先把尾部的 /v1 削掉——否则会变成 /v1/v1/tools/search。
        base = base_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        self._base = base
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
        rows = data.get("results")
        if rows is None:
            rows = data.get("items")
        if rows is None:
            # 200 但没有结果字段：这个端点没有 /v1/tools/search，或者回的完全是别的东西。
            # 不能当成"没搜到"——那会把"这条路走不通"伪装成"网上没有"。
            raise NotSupported(
                f"{self._base}/v1/tools/search 没有返回搜索结果（可能是中转站不提供这个接口）。"
            )
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


def pick_key(api_key_env: str, default_env: str) -> str:
    """按「显式指定的环境变量 → provider 的约定变量」取 key。"""
    env = api_key_env or default_env
    return os.environ.get(env, "")


def make(kind: str, *, base_url: str, api_key: str, searxng_url: str, api_key_env: str,
         model: str = ""):
    """按名字造 provider；缺配置就抛 SearchError（调用方决定降级还是报错）。

    `model` 目前没有 provider 用（留在这里是因为调用方统一传，省得改签名）。
    """
    if kind == "deepseek":
        if not api_key:
            raise SearchError("搜索没启用：缺 DeepSeek 的 API key。")
        return DeepSeek(base_url, api_key)
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
