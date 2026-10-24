"""网页端自启（体验件）：进 TUI 时确保后端 + 前端 dev server 在跑。

三条脾气：
- 幂等——先探端口，谁在跑就跳过谁（重复进 TUI 不会拉出第二份）；
- 静默——不弹终端窗口（CREATE_NO_WINDOW）、日志吞掉（DEVNULL），失败不炸 TUI；
- 同生共死——TUI 退出时收掉**这次自己拉起的**进程，端口上原有的不动（别人可能正用着）。
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

FRONTEND_PORT = 5173  # Vite 默认端口（web/ 未改配置）

# 与 server.PROTOCOL 必须一致（test_s6b 有一行断言守着）。加/改 WS 消息时两边一起 +1。
EXPECTED_PROTOCOL = 2


@dataclass
class WebRun:
    """这次 TUI 拉起了什么。

    只记自己拉起的——端口上原本就有的进程不记，退出时也不动它：那可能是另开的 TUI 或
    用户自己在用的后端，收掉等于替别人做主。
    """

    procs: list[tuple[str, subprocess.Popen]] = field(default_factory=list)
    warning: str | None = None

    @property
    def started(self) -> list[str]:
        return [name for name, _ in self.procs]



def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    """探测端口有没有人在听——0.3 秒超时，连不上就是没有。"""
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _spawn(argv: list[str], cwd: Path) -> subprocess.Popen:
    """起一个后台进程（不弹窗、不吃 Ctrl+C），返回值留着退出时收尸。"""
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
        kwargs["start_new_session"] = True  # 自成进程组，收的时候能整组收
    return subprocess.Popen(argv, **kwargs)


def _kill_tree(pid: int) -> bool:
    """结束一个进程连同它的子进程。cmd /c npm run dev 会再岔出 node、esbuild，只杀爹会留孤儿。"""
    try:
        if os.name == "nt":
            return subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, text=True,
            ).returncode == 0
        os.killpg(os.getpgid(pid), signal.SIGTERM)  # _spawn 用了 start_new_session，组号 = pid
        return True
    except (OSError, subprocess.SubprocessError, AttributeError):
        return False



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


def ensure_web(backend_port: int, workspace: Path, web_dir: Path) -> WebRun:
    """确保网页端在跑：缺后端起后端、缺前端起前端。返回这次拉起的东西 + 警告。"""
    run = WebRun()
    if not _port_open(backend_port):
        run.procs.append(
            ("后端", _spawn([sys.executable, "-m", "agent_harness.server"], cwd=workspace))
        )
    elif _backend_is_stale(backend_port):
        run.warning = (
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
            run.procs.append(("前端", _spawn(argv, cwd=web_dir)))
    return run


def stop_run(run: WebRun) -> list[str]:
    """收起这次 TUI 拉起的进程。返回给人看的结果行（已经自己退了的也照实说）。"""
    lines: list[str] = []
    for name, proc in run.procs:
        pid = getattr(proc, "pid", None)
        if pid is None or proc.poll() is not None:
            lines.append(f"{name}已自行退出")
            continue
        ok = _kill_tree(pid)
        lines.append(f"{name}{'已结束' if ok else '结束失败'}（PID {pid}）")
    return lines


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

    兜底手段：正常退出 TUI 时 stop_run 已经收过自己拉起的进程；这里给的是「另一个终端留下的
    孤儿」和「上次被 Ctrl+Z / 关窗口绕过的残留」——那些没有句柄可认，只能按端口找。
    """
    lines: list[str] = []
    for port in ports:
        pids = _listening_pids(port)
        if not pids:
            lines.append(f"{port}: 没有进程在听")
            continue
        for pid in pids:
            ok = _kill_tree(pid)
            lines.append(f"{port}: {'已结束' if ok else '结束失败'} PID {pid}")
    return lines
