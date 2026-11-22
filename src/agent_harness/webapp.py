"""HTTP 面：应用对象、探活、静态界面托管。

和 `server.py` 分开的原因：那边是 WebSocket 会话协议（一个连接 = 一个 agent 会话），
这边是普通 HTTP（探活 + 把打好的前端发出去）。混在一个文件里时，改协议的人要在一堆
静态托管代码之间找自己那段；分开后两边的读者和测试都只面对自己关心的东西。

`server.py` 会把这些名字重新导出（`server.app` 等），调用方不用改 import。
"""

import secrets
import threading
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# 前后端消息版本。加/改 WS 消息（或 /health 里新增 TUI 依赖的字段）时 +1，weblaunch 用它判断
# 「8000 上那个进程是不是旧代码」。别指望端口探测：上一个 TUI 拉起的后端会一直活着，占着端口
# 假装自己是新的。3 = /health 多了 busy（TUI 退出前靠它决定等不等网页那轮）。
PROTOCOL = 3

# 正在跑几轮（进程级）。TUI 退出前问一句：网页上还有人在等回复就先别关，不然那半轮就断了。
_BUSY = {"n": 0}
_BUSY_LOCK = threading.Lock()


def _turn_started() -> None:
    with _BUSY_LOCK:
        _BUSY["n"] += 1


def _turn_ended() -> None:
    with _BUSY_LOCK:
        _BUSY["n"] = max(0, _BUSY["n"] - 1)  # 兜住：重复减不出现负数，免得一直「有人在跑」


@app.get("/health")
def health() -> dict:
    """给 weblaunch 的自检端点：应用名 + 协议版本 + 还有几轮在跑。"""
    with _BUSY_LOCK:
        busy = _BUSY["n"]
    return {"app": "agent-harness", "protocol": PROTOCOL, "busy": busy}


# 打包形态：前端构建产物跟着包走（src/agent_harness/web_dist），后端一个端口全包
# ——装完不用 Node、不用 vite，直接开 http://127.0.0.1:8000。
WEB_DIST = Path(__file__).parent / "web_dist"


# ---- 连接鉴权：Origin 挡浏览器，token 挡局域网里的 curl ----

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# 每次启动都换。放进 webapp 而不是 server：它属于"这个 HTTP 面"，而且 token 端点必须
# 注册在 SPA 通配路由**之前**——通配路由一匹配，后面的端点就轮不上了。
SESSION_TOKEN = secrets.token_urlsafe(32)


def origin_allowed(origin: str | None, extra: tuple[str, ...] = ()) -> bool:
    """浏览器发起的 WebSocket 一定带 `Origin`，而且**页面无法伪造它**（由浏览器按实际来源填）。

    所以"只接受本机来源"就足以挡住任意网页连 `ws://127.0.0.1:8000/ws`——
    那些页面的 Origin 是 `https://evil.com`，连不上。这也是 WS 唯一的守门手段：
    它不受同源策略和 CORS 约束，服务端不验 Origin 就等于对全网开放。

    三种放行/拒绝的边界：
    - **没有 Origin**（curl / 脚本 / 测试客户端）→ 放行。既然它已经能在本机执行代码，
      多这道门没有意义；挡住它只会让调试变难。**但 WS 还要 token**，那条才是门闩。
    - **`Origin: null`** → 拒绝。sandbox iframe、`file://` 页面的 Origin 就是字符串 "null"，
      那正是最需要挡的来源。
    - **端口不校验**：前端 dev server 是 5173、生产同端口是 8000，都可能；而端口不提供
      额外安全性——攻击者换不了 Origin 里的 hostname。
    """
    if not origin:
        return True
    if origin == "null":
        return False
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if (parsed.hostname or "").lower() in _LOCAL_HOSTS:
        return True
    return any(origin.rstrip("/") == e.rstrip("/") for e in extra)


@app.get("/api/token")
async def api_token(request: Request):
    """把本次启动的 token 交给**同源页面**。

    没有 Origin（curl / 脚本）一律 403：否则局域网里一条 curl 就把钥匙拿走了，
    token 也就白设了。跨站页面读不到响应体（没有 ACAO 头时浏览器拦住）；
    dev 模式下前端在 5173、后端在 8000，是跨端口，所以对白名单来源回显 ACAO。
    """
    origin = request.headers.get("origin")
    if not origin or not origin_allowed(origin):
        raise HTTPException(status_code=403, detail="origin not allowed")
    resp = JSONResponse({"token": SESSION_TOKEN})
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _web_dist_response(path: Path):
    if not path.is_file():
        raise HTTPException(status_code=404, detail="网页界面没打包（只跑 API）")
    return FileResponse(path)


def _mount_web_ui() -> None:
    """挂前端路由。构建产物在就托管，不在就是纯 API（开发时前端在 5173，由 vite 顶着）。

    路由无条件注册、响应时才看文件在不在：否则测试和线上会因为"dist 有没有构建过"走出
    两套不同的 URL 表，藏 bug。
    """
    if (WEB_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def _ui_index():
        return _web_dist_response(WEB_DIST / "index.html")

    @app.get("/{path:path}", include_in_schema=False)
    def _ui_file(path: str):
        """dist 里的真文件发文件，其余一律回 index.html（前端自己按路径渲染）。"""
        target = (WEB_DIST / path).resolve()
        try:
            target.relative_to(WEB_DIST.resolve())  # 挡住 ../ 穿越
        except ValueError:
            target = WEB_DIST / "index.html"
        return _web_dist_response(target if target.is_file() else WEB_DIST / "index.html")


_mount_web_ui()
