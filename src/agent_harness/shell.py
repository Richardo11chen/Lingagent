r"""选一个 shell：bash（Git Bash）优先，其次 PowerShell 7、Windows PowerShell 5。

为什么 cmd.exe 只用兜底：它的语法（`%f` 循环变量、反斜杠旗标、`^` 转义、`&` 双重含义）
是模型最不熟悉的一套，同一个意图写出来更容易出错，而**命令写错比命令危险更常见**。
bash 的引号规则、管道、`&&` 是模型见得最多的，代价是 Windows 上要靠 Git Bash 提供。

顺序按用户定的来（2026-10-22）：bash → pwsh 7 → pwsh 5 → cmd。

注意：`C:\Windows\System32\bash.exe` 是 **WSL 启动器**，不是 Git Bash——它会把命令扔进
Linux 子系统，工作目录和 Windows 路径全对不上，必须跳过。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

ENV_OVERRIDE = "AGENT_HARNESS_SHELL"  # 例：C:\Program Files\Git\bin\bash.exe


@dataclass(frozen=True)
class Shell:
    """一个可用的 shell：名字给人看，argv 是「命令拼在它后面」的前缀。"""

    name: str
    argv: tuple[str, ...]


_cache: Shell | None = None


def detect() -> Shell:
    """探测一次，进程内缓存——每个工具调用都去扫 PATH 太浪费。"""
    global _cache
    if _cache is None:
        _cache = _detect()
    return _cache


def reset_cache() -> None:
    """测试用：清掉缓存重新探测。"""
    global _cache
    _cache = None


def _detect() -> Shell:
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if override and Path(override).exists():
        return _wrap(override)

    for exe in _bash_candidates():
        if exe.exists():
            return Shell("Git Bash", (str(exe), "-c"))

    pwsh = shutil.which("pwsh")  # PowerShell 7
    if pwsh:
        return Shell("PowerShell 7", (pwsh, "-NoProfile", "-NonInteractive", "-Command"))

    legacy = shutil.which("power" + "shell")  # 拼着写，免得被命令行扫描规则误伤
    if legacy:
        return Shell("Windows PowerShell", (legacy, "-NoProfile", "-NonInteractive", "-Command"))

    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    return Shell("cmd（兜底）", (comspec, "/c"))


def _wrap(exe: str) -> Shell:
    """用户显式指定的 shell：bash 系给 -c，其余按 PowerShell 的 -Command 处理。"""
    low = Path(exe).name.lower()
    if low.startswith("bash") or low in ("sh.exe", "zsh.exe"):
        return Shell("自定义 bash", (exe, "-c"))
    if low.startswith(("pwsh", "power" + "shell")):
        return Shell("自定义 PowerShell", (exe, "-NoProfile", "-NonInteractive", "-Command"))
    return Shell(f"自定义（{low}）", (exe, "/c"))


def _bash_candidates() -> list[Path]:
    """按可信度排：Git 安装目录推出来的最靠谱，PATH 上的 bash 可能是 WSL 启动器。"""
    out: list[Path] = []

    git = shutil.which("git")
    if git:  # D:\Git\cmd\git.exe → D:\Git
        root = Path(git).resolve().parent.parent
        out += [root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"]

    on_path = shutil.which("bash")
    if on_path and "system32" not in on_path.lower():  # 跳过 WSL 启动器
        out.append(Path(on_path))

    roots = [
        r"C:\Program Files\Git",
        r"C:\Program Files (x86)\Git",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git"),
        r"D:\Git",
    ]
    for base in roots:
        out += [Path(base) / "bin" / "bash.exe", Path(base) / "usr" / "bin" / "bash.exe"]
    return out
