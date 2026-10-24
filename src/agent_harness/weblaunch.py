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
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

FRONTEND_PORT = 5173  # Vite 默认端口（web/ 未改配置）

# 与 server.PROTOCOL 必须一致（test_s6b 有一行断言守着）。加/改 WS 消息或 /health 字段时两边一起 +1。
EXPECTED_PROTOCOL = 3


@dataclass
class WebRun:
    """这次 TUI 拉起了什么。

    只记自己拉起的——端口上原本就有的进程不记，退出时也不动它：那可能是另开的 TUI 或
    用户自己在用的后端，收掉等于替别人做主。
    """

    procs: list[tuple[str, subprocess.Popen]] = field(default_factory=list)
    warning: str | None = None
    reaped: list[str] = field(default_factory=list)  # 上次 TUI 被强杀时留下的，这次开工前收了
    ui_port: int | None = None  # 网页界面的端口（开发 = vite 5173，装好的包 = 后端端口）

    @property
    def started(self) -> list[str]:
        return [name for name, _ in self.procs]


def dev_web_dir() -> Path | None:
    """仓库里的 `web/`（只有源码树里有；装成 wheel 之后没有）。

    为什么按包的位置找、不按 cwd：`cd` 到别人的前端项目里启动 TUI 时，`cwd/web/package.json`
    可能存在——那会把 `npm run dev` 拉进用户自己的项目里，纯属莫名其妙。
    """
    cand = Path(__file__).resolve().parents[2] / "web"
    return cand if (cand / "package.json").exists() else None


def bundled_web_dir() -> Path | None:
    """包内的前端构建产物（`agent_harness/web_dist`）——有它就不用 vite、单端口。"""
    cand = Path(__file__).resolve().parent / "web_dist"
    return cand if (cand / "index.html").exists() else None



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



def _health(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> dict | None:
    """问一句 /health。连不上 / 不是 JSON → None（比这个机制还老的版本）。"""
    # 本机请求别走系统代理：这台机器上 HTTP_PROXY 会把 127.0.0.1 也拦掉（回 502）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{host}:{port}/health", timeout=timeout) as r:
            data = json.load(r)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _backend_is_stale(port: int, host: str = "127.0.0.1") -> bool:
    """端口有东西在听——但它是当前版本的后端吗？

    只看端口会踩坑：TUI 拉起的后端是独立进程，TUI 退了它还活着。之后每次进 TUI 都因为
    "端口有人在听"而跳过启动，网页就一直连到旧代码（2026-10-24 卡过一轮：新加的按钮点了没反应，
    后端根本不认那条消息）。
    """
    data = _health(port, host)
    if data is None:
        return True
    return data.get("app") != "agent-harness" or data.get("protocol") != EXPECTED_PROTOCOL


def backend_busy(port: int, host: str = "127.0.0.1") -> int | None:
    """网页上还有几轮在跑。None = 问不到（没起 / 老到没有这个字段）。"""
    data = _health(port, host)
    if data is None:
        return None
    busy = data.get("busy")
    return busy if isinstance(busy, int) else 0


def wait_until_idle(
    port: int,
    timeout: float,
    on_wait: Callable[[int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    step: float = 0.5,
) -> bool:
    """等网页上正在跑的那一轮跑完（最多 timeout 秒）。True = 等空了。

    退出 TUI 要关后端时用：后端一关，网页那半轮就断了——工具可能正改文件，
    trace 也停在半截。等它自己写完再关，代价只是晚几秒退出。
    """
    busy = backend_busy(port)
    if not busy:  # 0 = 闲着；None = 那么后端都没了，没什么可等
        return True
    if on_wait:
        on_wait(busy)
    end = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < end:
        sleep(step)
        busy = backend_busy(port)
        if busy is None or busy == 0:
            return True
    return False


def _wait_port_closed(port: int, timeout: float = 5.0) -> bool:
    """等端口真的松手。taskkill 返回成功不等于监听立刻消失，紧接着起新的会撞上。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not _port_open(port):
            return True
        time.sleep(0.1)
    return False


def _state_path() -> Path:
    """跨启动的认领凭据（`~/.agent-harness/web.json`）。

    只在 TUI 被强杀（finally 跑不到）时才用得上：那种情况没有句柄可认，只能靠这份记录知道
    "8000 上那个进程是哪个死掉的 TUI 留下的"。
    """
    from .settings import CONFIG_PATH

    return CONFIG_PATH.parent / "web.json"


STATE_TTL = 7 * 24 * 3600  # 超过一周的记录不信了（端口和 pid 都可能早被别人占走）


def _write_state(run: WebRun, backend_port: int) -> None:
    data = {
        "tui": os.getpid(),
        "at": time.time(),
        "procs": [
            # 只有两种；名字就是它们在 ensure_web 里的身份
            {"name": name, "pid": proc.pid, "port": FRONTEND_PORT if name == "前端" else backend_port}
            for name, proc in run.procs
        ],
    }
    try:
        _state_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 记不下来不影响用，只是下次认不出孤儿


def _read_state() -> dict | None:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_state() -> None:
    try:
        _state_path().unlink()
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """这个 pid 还在吗。

    Windows 上不能用 `os.kill(pid, 0)` 试探——它走的是 TerminateProcess，等于把进程杀了。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def reap_orphans(backend_port: int) -> list[str]:
    """收掉上一个 TUI 被强杀时留下的后端。返回给人看的结果行。

    TUI 被 kill -9 / 任务管理器结束时 finally 跑不到，进程成了孤儿；下次启动按端口看又会把它
    当"已经起好了"复用。这里靠 web.json 认领：那个 TUI 的 pid 已经没了 → 它拉起的这些是遗物。

    三道闸，缺一不杀（宁可留个孤儿，也不能杀别人的进程）：
    ① 记下的 pid 还活着  ② 它那个端口还有人听着  ③ 后端的话，/health 得答 agent-harness
    （③ 防的是 pid 被系统复用：端口上要是换成了别的程序，就不认了。）

    注意 pid 不等于监听者：Windows 上 `.venv/Scripts/python.exe` 是个转手进程，真正 listen 的是
    它拉起的子进程——所以按记下的 pid 收整棵树（taskkill /T），不要拿 pid 去和 netstat 对。
    """
    st = _read_state()
    if not st:
        return []
    if time.time() - float(st.get("at") or 0) > STATE_TTL:
        clear_state()  # 太久远，不敢认
        return []
    if _pid_alive(int(st.get("tui") or 0)):
        return []  # 那个 TUI 还活着——网页端归它管，别插手
    lines: list[str] = []
    for item in st.get("procs") or []:
        name = str(item.get("name") or "进程")
        pid, port = int(item.get("pid") or 0), int(item.get("port") or 0)
        if pid <= 0 or port <= 0 or not _pid_alive(pid):
            continue  # 它自己退干净了（多数情况），没什么可收
        if not _port_open(port):
            continue  # 已经不在占端口了，别追着杀一个正在退出的进程
        if name == "后端" and (_health(port) or {}).get("app") != "agent-harness":
            continue  # 端口上不是我们的后端——pid 被复用了，不认
        ok = _kill_tree(pid) and _wait_port_closed(port)
        lines.append(f"{name}{'已结束' if ok else '结束失败'}（上次遗留 PID {pid}）")
    clear_state()
    return lines


def ensure_web(backend_port: int, workspace: Path) -> WebRun:
    """确保网页端在跑：缺后端起后端；开发树的 web/ 缺前端就起 vite。返回这次拉起的东西。

    两种形态：
    - 源码树（`dev_web_dir()` 有）：vite 5173 + 后端 8000，热更新改前端
    - 装好的包（`bundled_web_dir()` 有）：只有后端，静态文件由它托管 → 一个端口 8000
    """
    run = WebRun()
    run.reaped = reap_orphans(backend_port)  # 先清遗物，否则端口还被它占着、会被当成"已在跑"
    if not _port_open(backend_port):
        run.procs.append(
            ("后端", _spawn([sys.executable, "-m", "agent_harness.server"], cwd=workspace))
        )
    elif _backend_is_stale(backend_port):
        run.warning = (
            f"{backend_port} 端口上跑的后端不是当前版本——网页会连到旧代码。"
            f"先结束那个进程（或改端口）再开 TUI。"
        )
    web_dir = dev_web_dir()
    if web_dir and not _port_open(FRONTEND_PORT):
        npm = shutil.which("npm")
        if npm:
            if npm.lower().endswith((".cmd", ".bat")):  # Windows：.cmd 要走 cmd /c
                argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", npm, "run", "dev"]
            else:
                argv = [npm, "run", "dev"]
            run.procs.append(("前端", _spawn(argv, cwd=web_dir)))
    if web_dir:
        run.ui_port = FRONTEND_PORT
    elif bundled_web_dir():
        run.ui_port = backend_port  # 静态文件由后端托管：同一个端口
    if run.procs:
        _write_state(run, backend_port)  # 给"下次启动"留一份认领凭据
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
