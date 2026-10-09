"""TUI 层（S6a）：prompt_toolkit 输入（底部 ❯）+ rich 流式 Markdown 渲染。

分工：prompt_toolkit 管交互（输入行）；rich 管渲染（消息面板、流式 Markdown、工具提示）。
"""

import shutil
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.styles import Style
from wcwidth import wcswidth
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel

console = Console()

_session: PromptSession | None = None
_live: Live | None = None
_buf: list[str] = []


def _interactive() -> bool:
    """——管道/重定向时降级为普通 input（prompt_toolkit 要真 console）。"""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _width() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def _session_get() -> PromptSession:
    global _session
    if _session is None:
        _session = PromptSession(
            message=[("fg:ansicyan bold", "❯ ")],
            bottom_toolbar=lambda: FormattedText([("fg:ansibrightblack", "─" * _width())]),
            # toolbar 默认样式是反色（Windows 上呈灰底）——显式 noreverse + 默认背景
            style=Style.from_dict({"bottom-toolbar": "noreverse bg:ansidefault"}),
        )
    return _session


def show_banner(session_id: str, n_history: int) -> None:
    console.print(
        Panel(
            f"会话 [cyan]{session_id}[/] · {n_history} 条历史 · [dim]exit 退出[/]",
            title="Agent Harness",
            border_style="blue",
        )
    )


def ask() -> str:
    """底部输入：上线 + ❯ 输入行 + 下线（bottom_toolbar）。非终端环境降级。"""
    console.print() # 输出空行
    if not _interactive():
        return input("❯ ")
    text = _session_get().prompt()
    _erase_input_echo(text)  # 擦掉输入回显——避免与「用户」面板重复
    return text


def _erase_input_echo(text: str) -> None:
    """把 ❯ 输入行从屏幕上清掉——按显示宽度算折行数（中文 2 列）。"""
    shown = wcswidth("❯ " + text)
    if shown < 0:  # 含不可打印字符——退回字符数估算
        shown = len("❯ " + text)
    lines = (shown // max(_width(), 1)) + 1
    sys.stdout.write("\x1b[1A\x1b[2K" * lines)
    sys.stdout.flush()


def show_user(text: str) -> None:
    console.print() # 输出空行
    console.print(Panel(text, title="用户", title_align="left", border_style="cyan"))
    console.print() # 输出空行


def show_history(messages) -> None:
    """恢复会话时把历史渲染出来（形态与实时一致：用户框 / ● 回复 / ⚙ 工具）。"""
    for m in messages:
        if m.role == "user":
            show_user(m.content)
        elif m.role == "assistant" and (m.content or "").strip():
            console.print(Markdown("● " + m.content))
        elif m.role == "tool":
            show_tool(m.tool_name or "工具结果")


def begin_stream() -> None:
    global _live, _buf
    _buf = []
    _live = Live(console=console, refresh_per_second=10, vertical_overflow="visible")
    _live.start()


def stream_chunk(chunk: str) -> None:
    _buf.append(chunk)
    if _live:
        _live.update(Markdown("● " + "".join(_buf)))


def end_stream() -> None:
    global _live
    if _live:
        _live.update(Markdown("● " + "".join(_buf)))
        _live.stop()
        _live = None


def show_tool(name: str, args: dict | None = None) -> None:
    detail = str((args or {}).get("command") or (args or {}).get("path") or "")
    if len(detail) > 50:
        detail = detail[:50] + "…"
    console.print(f"[dim]⚙ {name}{': ' + detail if detail else ''}[/]")


def show_approval(command: str) -> str:
    prompt_text = f"⚠️ 允许执行「{command}」吗？(y/n) "
    if not _interactive():
        return input(prompt_text)
    return _session_get().prompt(message=[("fg:ansiyellow bold", prompt_text + "❯ ")])
