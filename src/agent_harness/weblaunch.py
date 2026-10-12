"""网页端自启（体验件）：进 TUI 时确保后端 + 前端 dev server 在跑。

三条脾气：
- 幂等——先探端口，谁在跑就跳过谁（重复进 TUI 不会拉出第二份）；
- 静默——不弹终端窗口（CREATE_NO_WINDOW）、日志吞掉（DEVNULL），失败不炸 TUI；
- 独立——关掉 TUI（exit / Ctrl+C）网页端还活着。
"""

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

FRONTEND_PORT = 5173  # Vite 默认端口（web/ 未改配置）


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    """探测端口有没有人在听——0.3 秒超时，连不上就是没有。"""
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _spawn(argv: list[str], cwd: Path) -> None:
    """起一个脱离当前终端的后台进程（关 TUI 不倒）。"""
    kwargs: dict = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        # CREATE_NO_WINDOW：不弹终端窗口（实测 DETACHED_PROCESS 下 cmd/python 会自拉一个窗口）；
        # CREATE_NEW_PROCESS_GROUP：Ctrl+C 不连坐（新进程组默认屏蔽）。
        # 代价：与控制台共生命周期——关掉终端窗口会连坐，exit/Ctrl+C 退 TUI 不受影响。
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)


def ensure_web(backend_port: int, workspace: Path, web_dir: Path) -> list[str]:
    """确保网页端在跑：缺后端起后端、缺前端起前端。返回本次启动的项（幂等）。"""
    started: list[str] = []
    if not _port_open(backend_port):
        _spawn([sys.executable, "-m", "agent_harness.server"], cwd=workspace)
        started.append("后端")
    if (web_dir / "package.json").exists() and not _port_open(FRONTEND_PORT):
        npm = shutil.which("npm")
        if npm:
            if npm.lower().endswith((".cmd", ".bat")):  # Windows：.cmd 要走 cmd /c
                argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", npm, "run", "dev"]
            else:
                argv = [npm, "run", "dev"]
            _spawn(argv, cwd=web_dir)
            started.append("前端")
    return started
