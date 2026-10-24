"""网页端自启（体验件）：进 TUI 时确保后端 + 前端 dev server 在跑。

三条脾气：
- 幂等——先探端口，谁在跑就跳过谁（重复进 TUI 不会拉出第二份）；
- 静默——不弹终端窗口（CREATE_NO_WINDOW）、日志吞掉（DEVNULL），失败不炸 TUI；
- 独立——关掉 TUI（exit / Ctrl+C）网页端还活着。
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

FRONTEND_PORT = 5173  # Vite 默认端口（web/ 未改配置）

# 与 server.PROTOCOL 必须一致（test_s6b 有一行断言守着）。加/改 WS 消息时两边一起 +1。
EXPECTED_PROTOCOL = 2


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


def _backend_is_stale(port: int, host: str = "127.0.0.1") -> bool:
    """端口有东西在听——但它是当前版本的后端吗？

    只看端口会踩坑：TUI 拉起的后端是独立进程，TUI 退了它还活着。之后每次进 TUI 都因为
    "端口有人在听"而跳过启动，网页就一直连到旧代码（2026-10-24 卡过一轮：新加的按钮点了没反应，
    后端根本不认那条消息）。
    """
    # 本机请求别走系统代理：这台机器上 HTTP_PROXY 会把 127.0.0.1 也拦掉（回 502）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{host}:{port}/health", timeout=0.6) as r:
            data = json.load(r)
    except Exception:
        return True  # 连 /health 都没有：比这个机制还老的版本
    return data.get("app") != "agent-harness" or data.get("protocol") != EXPECTED_PROTOCOL


def ensure_web(backend_port: int, workspace: Path, web_dir: Path) -> tuple[list[str], str | None]:
    """确保网页端在跑：缺后端起后端、缺前端起前端。返回 (本次启动的项, 警告或 None)。"""
    started: list[str] = []
    warning: str | None = None
    if not _port_open(backend_port):
        _spawn([sys.executable, "-m", "agent_harness.server"], cwd=workspace)
        started.append("后端")
    elif _backend_is_stale(backend_port):
        warning = (
            f"{backend_port} 端口上跑的后端不是当前版本——网页会连到旧代码。"
            f"先结束那个进程（或改端口）再开 TUI。"
        )
    if (web_dir / "package.json").exists() and not _port_open(FRONTEND_PORT):
        npm = shutil.which("npm")
        if npm:
            if npm.lower().endswith((".cmd", ".bat")):  # Windows：.cmd 要走 cmd /c
                argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", npm, "run", "dev"]
            else:
                argv = [npm, "run", "dev"]
            _spawn(argv, cwd=web_dir)
            started.append("前端")
    return started, warning


def _parse_netstat(out: str, port: int) -> list[int]:
    """从 `netstat -ano` 的输出里挑出「LISTENING 在这个端口上」的 PID。

    单独拆出来是为了能测：真实输出里 IPv4/IPv6 各一行、还有大量非监听行。
    """
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() not in ("TCP", "TCPV6"):
            continue
        local, state, pid = parts[1], parts[3], parts[4]
        if not local.endswith(f":{port}") or state.upper() != "LISTENING":
            continue
        if pid.isdigit() and int(pid) not in pids:
            pids.append(int(pid))
    return pids


def _listening_pids(port: int) -> list[int]:
    """谁在听这个端口（Windows 用 netstat，其他平台用 lsof）。查不到就当没有。"""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=10
            ).stdout
            return _parse_netstat(out, port)
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def stop_web(ports: list[int]) -> list[str]:
    """按端口结束占用的进程（连子进程）。返回给人看的结果行。

    为什么需要：后端和前端是脱离终端的后台进程，关 TUI 不会连坐；改了后端代码就得先让它退出，
    否则下次进 TUI 会复用旧进程（网页连到旧代码，新功能静默失效）。
    """
    lines: list[str] = []
    for port in ports:
        pids = _listening_pids(port)
        if not pids:
            lines.append(f"{port}: 没有进程在听")
            continue
        for pid in pids:
            try:
                if os.name == "nt":
                    ok = subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True, text=True,
                    ).returncode == 0
                else:
                    ok = subprocess.run(["kill", "-TERM", str(pid)]).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            lines.append(f"{port}: {'已结束' if ok else '结束失败'} PID {pid}")
    return lines
