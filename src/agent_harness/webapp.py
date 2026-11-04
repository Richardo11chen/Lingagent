"""HTTP 面：应用对象、探活、静态界面托管。

和 `server.py` 分开的原因：那边是 WebSocket 会话协议（一个连接 = 一个 agent 会话），
这边是普通 HTTP（探活 + 把打好的前端发出去）。混在一个文件里时，改协议的人要在一堆
静态托管代码之间找自己那段；分开后两边的读者和测试都只面对自己关心的东西。

`server.py` 会把这些名字重新导出（`server.app` 等），调用方不用改 import。
"""

import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
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
