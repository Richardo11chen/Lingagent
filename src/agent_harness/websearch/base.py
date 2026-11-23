"""搜索 provider 的公共契约。

只定三件事：一条结果长什么样、一个 provider 要提供什么、失败怎么表达。
各家的 JSON 形状差异全部关在 `providers.py` 里——上层只认 `Hit`。
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Hit:
    """一条搜索结果。摘要可能为空（有的 provider 只给标题和链接）。"""

    title: str
    url: str
    snippet: str = ""


class SearchError(Exception):
    """给人看的失败。消息本身就是最终文案——工具层直接回灌给模型，不再加工。"""


class NotSupported(SearchError):
    """该端点不提供这项能力（目前只有 fetch 会出现）。

    继承 `SearchError` 是有意的：工具层只需要一个 except 分支，而它的消息本身
    就已经是给人看的话。**但探测要能区分它和"网络炸了"**——见 `__init__._works`。
    """


class SearchProvider(Protocol):
    """一个搜索源。

    `supports_fetch = False` 的 provider 会落到内置抓取器（见 `fetch.py`）——
    Brave 和 SearXNG 就属于这种：它们只给摘要，正文得自己取。
    """

    name: str
    supports_fetch: bool

    def search(self, query: str, n: int) -> list[Hit]: ...

    def fetch(self, url: str) -> str: ...


def raise_for(resp, what: str) -> None:
    """把 HTTP 失败翻成人话。

    只翻最常见的几种：余额、限流、鉴权、404。其余原样带状态码和一小段响应体——
    比"搜索失败"四个字有用得多，也不会把整页 HTML 灌进上下文。
    """
    code = resp.status_code
    if code < 400:
        return
    if code == 402:
        raise SearchError(f"{what}失败：账户余额不足（HTTP 402）。")
    if code == 429:
        raise SearchError(f"{what}失败：被限流了（HTTP 429），等一会再试。")
    if code in (401, 403):
        raise SearchError(f"{what}失败：key 无效或没有权限（HTTP {code}）。")
    if code == 404:
        raise SearchError(f"{what}失败：对方说没有这个资源（HTTP 404）——抓页面时常见于站点禁止抓取。")
    raise SearchError(f"{what}失败：HTTP {code} {resp.text[:200]}")
