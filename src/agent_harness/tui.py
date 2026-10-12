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
_approval: PromptSession | None = None  # 审批专用输入会话（erase_when_done、无 toolbar）
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


def _live_open() -> None:
    """开一个新 Live 接着画（审批让位后归位用——旧块已印屏，不重画）。"""
    global _live
    _live = Live(console=console, refresh_per_second=8, vertical_overflow="visible")
    _live.start()


def begin_stream() -> None:
    """本轮开始：开 Live（唯一输出通道——思考/工具/正文都在里面组合渲染）。"""
    global _blocks, _reasoning_buf, _text_buf, _in_reasoning
    _blocks, _reasoning_buf, _text_buf, _in_reasoning = [], "", "", False
    _live_open()


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
    table.add_column("#", style="dim", justify="right")
    table.add_column("时间", style="dim")
    table.add_column("事件")
    table.add_column("详情")
    table.add_column("耗时", justify="right", style="dim")
    for i, r in enumerate(recs, 1):
        ts = str(r.get("ts", ""))[11:23]  # 只留 HH:MM:SS.mmm
        kind = r.get("kind")
        if kind == "model":
            if r.get("error"):
                detail = f"失败 · {r['error']}"
            else:
                detail = f"工具调用 ×{r.get('n_actions', '?')}"
            table.add_row(str(i), ts, "● 模型", f"step {r.get('step')} · {detail}", _fmt_ms(r.get("duration_ms")))
        elif kind == "tool":
            status = {"ok": "成功", "denied": "驳回", "blocked": "拦截", "error": "错误"}.get(r.get("status"), str(r.get("status")))
            args = r.get("args") or {}
            detail = str(args.get("command") or args.get("path") or "")
            if r.get("error"):
                detail = f"{detail} {r['error']}".strip()
            table.add_row(str(i), ts, f"⚙  {r.get('tool')}", f"{status} · {detail[:50]}", _fmt_ms(r.get("duration_ms")))
        elif kind == "approval":
            decision = {"approved": "已批准", "denied": "已驳回"}.get(r.get("decision"), str(r.get("decision")))
            table.add_row(str(i), ts, "⚠  审批", f"{decision} · {str(r.get('command', ''))[:40]}", _fmt_ms(r.get("duration_ms")))
    console.print(table)
    console.print("[dim]看某条全文：/trace N[/]")


def show_trace_event(rec: dict, idx: int) -> None:
    """单条事件的完整内容（/trace N）——总览表里钻进去看全文。

    内容走 Text（不走 markup 解析）——工具结果里带 [exit=0] 这类方括号是常态。
    """
    body = Text()
    body.append(f"#{idx} · {rec.get('kind', '')}\n", style="bold")
    for k, v in rec.items():
        body.append(f"{k}：", style="dim")
        body.append(f"{v}\n")
    console.print(Panel(body, title="轨迹明细", title_align="left", border_style="dim"))


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


def _approval_session() -> PromptSession:
    """审批专用输入会话：erase_when_done——答完自动擦提示行（不擦会留屏，像「又弹了一次」）。"""
    global _approval
    if _approval is None:
        _approval = PromptSession(erase_when_done=True)
    return _approval


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

    ans = "n"  # 默认驳回——异常路径也有一张安全票
    try:
        if not interactive():
            ans = input(prompt_text)
        else:
            ans = _approval_session().prompt(message=[("fg:ansiyellow bold", prompt_text + "❯ ")])
    except EOFError:
        pass  # 输入读尽（管道喂完 / Ctrl+Z）——维持默认驳回
    except BaseException:  # Ctrl+C 等：Live 先归位再抛
        if restart:
            _live_open()
        raise

    # 此时 Live 仍停着——这一行永久落屏（审批的屏幕侧留痕；文件侧在 trace）
    console.print(f"[yellow]⚠  审批：{'已批准' if ans.strip().lower() == 'y' else '已驳回'}[/] [dim]· {command[:60]}[/]")
    if restart:
        _live_open()
    return ans
