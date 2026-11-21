"""内置抓取器：provider 自己不提供 fetch 时用它（Brave、SearXNG 都走这条）。

两条硬要求，缺一条这个模块就不该存在：

1. **挡 SSRF**。模型能构造任意 URL，`http://127.0.0.1:8000/ws` 就是它自己连着的后端，
   `http://169.254.169.254/` 是云元数据。抓取前解析域名拿 IP，私网/回环/link-local
   一律拒；每一跳重定向都要重新验（重定向是最常见的绕过方式）。
2. **只交纯文本**。整页 HTML 灌进上下文等于把 token 倒进海里——`<script>` 尤其。

DNS 重绑定这类高级绕过没处理：解析和连接之间理论上能换 IP。要彻底防得住得自己连
解析出来的那个 IP，那是另一个量级的实现。这里的定位是"挡住模型随手构造的内网地址"。
"""

import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from .base import SearchError, raise_for

MAX_BYTES = 2_000_000  # 正文只取前 2MB：再长的页面模型也读不完，只会挤爆上下文
MAX_REDIRECTS = 4
_UA = "lingagent (+search)"  # 带个身份，别装成浏览器
_BLOCKED_NAMES = {"localhost", "localhost.localdomain", "metadata.google.internal"}


class SSRFBlocked(SearchError):
    """目标地址不该被访问（内网 / 回环 / 元数据）。"""


def assert_public(url: str) -> None:
    """拒绝非 http(s)、拒绝解析到内网的地址。"""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise SSRFBlocked(f"只抓 http/https，收到的是 {u.scheme or '（空）'}：{url}")
    host = (u.hostname or "").lower()
    if not host:
        raise SSRFBlocked(f"URL 里没有主机名：{url}")
    if host in _BLOCKED_NAMES:
        raise SSRFBlocked(f"拒绝访问本机/元数据地址：{host}")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise SSRFBlocked(f"域名解析失败：{host}（{e}）") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise SSRFBlocked(f"拒绝访问内网地址：{host} → {ip}")


class _Text(HTMLParser):
    """HTML → 纯文本。跳过 script/style，块级标签换行。"""

    _SKIP = {"script", "style", "noscript", "template", "svg", "iframe"}
    _BLOCK = {
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "pre", "blockquote", "ul", "ol", "table",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: bytes, encoding: str = "utf-8") -> str:
    parser = _Text()
    parser.feed(raw.decode(encoding or "utf-8", errors="replace"))
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def fetch_text(url: str, timeout: float = 15.0) -> str:
    """抓一个页面，返回纯文本。"""
    for _ in range(MAX_REDIRECTS + 1):
        assert_public(url)
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            resp = client.get(url, headers={"User-Agent": _UA})
        if resp.is_redirect and resp.next_request is not None:
            url = str(resp.next_request.url)  # 每一跳都回到循环开头重新验
            continue
        raise_for(resp, "抓取")
        return html_to_text(resp.content[:MAX_BYTES], resp.encoding or "utf-8")
    raise SearchError("抓取失败：重定向次数太多。")
