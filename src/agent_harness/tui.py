"""TUI 层（S6a）：prompt_toolkit 输入（底部 ❯）+ rich 流式 Markdown 渲染。

分工：prompt_toolkit 管交互（输入行）；rich 管渲染（消息面板、流式 Markdown、工具提示）。
"""

import shutil
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.styles import Style
from wcwidth import wcswidth
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text
from rich.panel import Panel
from rich.table import Table

console = Console()

_session: PromptSession | None = None
_live: Live | None = None
_blocks: list = []  # 本轮已封块的 renderables（快照）
_reasoning_buf = ""
_text_buf = ""
_in_reasoning = False


def interactive() -> bool:
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


def show_banner(session_id: str, n_history: int, model_name: str = "") -> None:
    tag = f" · [magenta]{model_name}[/]" if model_name else ""
    console.print(
        Panel(
            f"会话 [cyan]{session_id}[/] · {n_history} 条历史{tag} · [dim]exit 退出[/]",
            title="Agent Harness",
            border_style="blue",
        )
    )


def ask() -> str:
    """底部输入：上线 + ❯ 输入行 + 下线（bottom_toolbar）。非终端环境降级。"""
    console.print() # 输出空行
    if not interactive():
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
    """本轮开始：开 Live（唯一输出通道——思考/工具/正文都在里面组合渲染）。"""
    global _live, _blocks, _reasoning_buf, _text_buf, _in_reasoning
    _blocks, _reasoning_buf, _text_buf, _in_reasoning = [], "", "", False
    _live = Live(console=console, refresh_per_second=8, vertical_overflow="visible")
    _live.start()


def _seal_current() -> None:
    """把当前未收尾的块封成快照（后续只重建 current，开销小）。"""
    global _reasoning_buf, _text_buf, _in_reasoning
    if _reasoning_buf:
        _blocks.append(Text("💭 " + _reasoning_buf, style="dim italic"))
        _reasoning_buf = ""
    if _text_buf:
        _blocks.append(Markdown("● " + _text_buf))
        _text_buf = ""
    _in_reasoning = False


def _refresh() -> None:
    if not _live:
        return
    current = []
    if _reasoning_buf:
        current.append(Text("💭 " + _reasoning_buf, style="dim italic"))
    if _text_buf:
        current.append(Markdown("● " + _text_buf))
    _live.update(Group(*_blocks, *current))


def reasoning_chunk(chunk: str) -> None:
    """思考增量：💭 暗色斜体（与正文区分）——「看它想什么」。"""
    global _reasoning_buf, _in_reasoning
    if not _in_reasoning:
        _seal_current()
        _in_reasoning = True
    _reasoning_buf += chunk
    _refresh()


def stream_chunk(chunk: str) -> None:
    """正文增量：● 前缀 + Markdown 流式重绘（走 Live——与思考/工具同通道）。"""
    global _text_buf, _in_reasoning
    if _in_reasoning:
        _seal_current()
    _text_buf += chunk
    _refresh()


def end_stream() -> None:
    global _live
    _seal_current()
    if _live:
        _live.stop()  # 停 Live——内容留在屏幕
        _live = None


def show_models(profiles: list[str], current: str) -> None:
    """列可选模型（/model 时显示——● 标当前）。"""
    lines = [f"  {i}. {n}{'  ●' if n == current else ''}" for i, n in enumerate(profiles, 1)]
    console.print(Panel("\n".join(lines), title="选择模型（输入序号，回车取消）", border_style="magenta"))


def show_note(text: str) -> None:
    console.print(f"[magenta]{text}[/]")


def _fmt_ms(v) -> str:
    if not isinstance(v, (int, float)):
        return ""
    return f"{round(v)}ms" if v < 1000 else f"{v / 1000:.1f}s"


def show_trace(recs: list[dict], session_id: str) -> None:
    """轨迹时间线（/trace）——只读投影（S8 决策 4：只看，不重演）。"""
    if not recs:
        console.print("[dim]这个会话还没有轨迹。[/]")
        return
    table = Table(title=f"轨迹 · {session_id}", title_justify="left", box=box.SIMPLE, pad_edge=False)
    table.add_column("时间", style="dim")
    table.add_column("事件")
    table.add_column("详情")
    table.add_column("耗时", justify="right", style="dim")
    for r in recs:
        ts = str(r.get("ts", ""))[11:23]  # 只留 HH:MM:SS.mmm
        kind = r.get("kind")
        if kind == "model":
            table.add_row(ts, "● 模型", f"step {r.get('step')} · 要了 {r.get('n_actions', '?')} 个动作", _fmt_ms(r.get("duration_ms")))
        elif kind == "tool":
            status = {"ok": "成功", "denied": "驳回", "blocked": "拦截", "error": "错误"}.get(r.get("status"), str(r.get("status")))
            args = r.get("args") or {}
            detail = str(args.get("command") or args.get("path") or "")
            if r.get("error"):
                detail = f"{detail} {r['error']}".strip()
            table.add_row(ts, f"⚙  {r.get('tool')}", f"{status} · {detail[:50]}", _fmt_ms(r.get("duration_ms")))
        elif kind == "approval":
            table.add_row(ts, "⚠  审批", f"{r.get('decision')} · {str(r.get('command', ''))[:40]}", _fmt_ms(r.get("duration_ms")))
    console.print(table)


def show_tool(name: str, args: dict | None = None) -> None:
    detail = str((args or {}).get("command") or (args or {}).get("path") or "")
    if len(detail) > 50:
        detail = detail[:50] + "…"
    line = f"⚙  {name}{': ' + detail if detail else ''}"
    if _live:
        _seal_current()
        _blocks.append(Text(line, style="dim"))
        _refresh()
    else:
        console.print(f"[dim]{line}[/]")  # Live 外（如历史渲染）直接打印


def show_approval(command: str) -> str:
    prompt_text = f"⚠️ 允许执行「{command}」吗？(y/n) "
    global _live, _blocks
    restart = False
    if _live is not None:
        # 流式中途审批：prompt_toolkit 要独占终端，而 Live 还在每秒 8 次重画——
        # 让它先收口让位（stop 把最后一帧留在屏幕上），答完再起新 Live 接着画
        _seal_current()
        _live.stop()
        _live = None
        _blocks = []  # 旧块已印在屏幕上——新 Live 不重画，避免同一份内容印两遍
        restart = True
    try:
        if not interactive():
            return input(prompt_text)
        return _session_get().prompt(message=[("fg:ansiyellow bold", prompt_text + "❯ ")])
    except EOFError:
        return "n"  # 输入读尽（管道喂完 / Ctrl+Z）——无声即驳回（安全默认）
    finally:
        if restart:
            _live = Live(console=console, refresh_per_second=8, vertical_overflow="visible")
            _live.start()
