"""TUI 层（S6a）：prompt_toolkit 输入（底部 ❯）+ rich 流式 Markdown 渲染。

分工：prompt_toolkit 管交互（输入行）；rich 管渲染（消息面板、流式 Markdown、工具提示）。
"""

import asyncio
import shutil
import sys
import time

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from wcwidth import wcswidth
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text
from rich.panel import Panel
from rich.table import Table

from .tools import tool_detail
from .trace import TRACE_FIELD_CHARS, ellipsis_note

console = Console()

_session: PromptSession | None = None
_approval: PromptSession | None = None  # 审批专用输入会话（erase_when_done、无 toolbar）
_live: Live | None = None
_blocks: list = []  # 本轮已封块的 renderables（快照）
_reasoning_buf = ""
_text_buf = ""
_in_reasoning = False
_waiting = False  # 已发出请求、还没等到第一个字——先给人一个「它在动」的信号
_ctrl_c_at = 0.0  # 上次按 Ctrl+C 的时刻（两次之内 = 退出）
CTRL_C_WINDOW = 2.0  # 秒


class _CommandCompleter(Completer):
    """输入 / 就自动列出命令——不用记，也不用先查文档。"""

    COMMANDS = (
        ("/model", "切换模型"),
        ("/trace", "查看本轮轨迹（/trace N 看单条）"),
    )

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        if any(text == cmd for cmd, _ in self.COMMANDS):
            return  # 已经打全了：收起菜单，别让 Enter 去「接受补全」而不是发送
        for cmd, desc in self.COMMANDS:
            if cmd.startswith(text):
                yield Completion(cmd, start_position=-len(text), display=cmd, display_meta=desc)


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
            completer=_CommandCompleter(),
            complete_while_typing=True,
        )
    return _session


def show_banner(session_id: str, n_history: int, model_name: str = "", shell_name: str = "") -> None:
    tag = f" · [magenta]{model_name}[/]" if model_name else ""
    sh = f" · [dim]{shell_name}[/]" if shell_name else ""
    console.print(
        Panel(
            f"会话 [cyan]{session_id}[/] · {n_history} 条历史{tag}{sh} · [dim]Ctrl+C 两次 退出[/]",
            title="Agent Harness",
            border_style="blue",
        )
    )


def show_sessions(rows: list[dict], page: int, total_pages: int, total: int) -> None:
    """历史会话选择器（分页）——rows: {time, model, preview}，每页十条。"""
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False, show_edge=False)
    table.add_column("#", style="dim", justify="right")
    table.add_column("时间", style="dim")
    table.add_column("模型", style="magenta")
    table.add_column("会话", overflow="ellipsis")
    for i, r in enumerate(rows, 1):
        table.add_row(str(i), r["time"], r["model"], r["preview"])
    console.print(Panel(
        table,
        title=f"历史会话 · 第 {page}/{total_pages} 页 · 共 {total} 个",
        title_align="left", border_style="blue",
    ))
    keys = "输入序号选择 · n 下一页 · p 上一页 · 0 新会话" if total_pages > 1 else "输入序号选择 · 0 新会话"
    console.print(f"[dim]{keys}[/]")


def show_hook(by: str, reason: str) -> None:
    """策略拦截提示（实时流里冒一行）——内容走 Text，不走 markup 解析。"""
    global _waiting
    _waiting = False
    line = f"⚡ 策略拦截：{by}" + (f" · {reason[:50]}" if reason else "")
    if _live:
        _seal_current()
        _blocks.append(Text(line, style="yellow"))
        _refresh()
    else:
        console.print(Text(line, style="yellow"))


def show_web_hint(started: list[str] | None = None) -> None:
    """网页端链接提示——由调用方放在历史渲染之后（长历史时不用往上翻找）。"""
    tail = f" · {'、'.join(started)}启动中，几秒后就绪" if started else ""
    console.print(f"[dim]网页端: http://localhost:5173{tail}[/]")


def ask() -> str:
    """底部输入。Ctrl+D 退出；Ctrl+C 按一次清空、连按两次退出（都不打 traceback）。"""
    global _ctrl_c_at
    console.print() # 输出空行
    if not interactive():
        return input("❯ ")
    try:
        text = _session_get().prompt()
    except KeyboardInterrupt:
        now = time.monotonic()
        if now - _ctrl_c_at <= CTRL_C_WINDOW:
            raise EOFError  # 第二次：走 main 的收工路径——干净退出，不是错误
        _ctrl_c_at = now
        console.print("[dim]再按一次 Ctrl+C 退出（或 Ctrl+D）[/]")
        return ""  # 第一次：当作清空当前输入，回到提示行
    _ctrl_c_at = 0.0
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
    global _blocks, _reasoning_buf, _text_buf, _in_reasoning, _waiting
    _blocks, _reasoning_buf, _text_buf, _in_reasoning = [], "", "", False
    _waiting = True  # 请求已发出、还没回音——先挂一个「等」的提示，别让屏幕像死了
    _live_open()
    _refresh()  # 立刻画一帧，否则「等」要等到第一个 chunk 才出现（等于没写）


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
    if _waiting and not _blocks and not _reasoning_buf and not _text_buf:
        current.append(Text("⋯ 等待模型响应", style="dim"))  # 第一个字到之前，给个活着的信号
    if _reasoning_buf:
        current.append(Text("💭 " + _reasoning_buf, style="dim italic"))
    if _text_buf:
        current.append(Markdown("● " + _text_buf))
    _live.update(Group(*_blocks, *current))


def reasoning_chunk(chunk: str) -> None:
    """思考增量：💭 暗色斜体（与正文区分）——「看它想什么」。"""
    global _reasoning_buf, _in_reasoning, _waiting
    _waiting = False
    if not _in_reasoning:
        _seal_current()
        _in_reasoning = True
    _reasoning_buf += chunk
    _refresh()


def stream_chunk(chunk: str) -> None:
    """正文增量：● 前缀 + Markdown 流式重绘（走 Live——与思考/工具同通道）。"""
    global _text_buf, _in_reasoning, _waiting
    _waiting = False
    if _in_reasoning:
        _seal_current()
    _text_buf += chunk
    _refresh()


def end_stream() -> None:
    global _live, _waiting
    _waiting = False
    _seal_current()
    if _live:
        _live.stop()  # 停 Live——内容留在屏幕
        _live = None


def show_models(profiles: list[str], current: str) -> None:
    """列可选模型（/model 时显示——● 标当前）。"""
    lines = [f"  {i}. {n}{'  ●' if n == current else ''}" for i, n in enumerate(profiles, 1)]
    console.print(Panel("\n".join(lines), title="选择模型（输入序号，回车取消）", border_style="magenta"))


# 提示的三档：别让「操作成功」和「这一轮失败了」长成同一个样子
_NOTE_STYLE = {"info": ("magenta", ""), "warn": ("yellow", "! "), "error": ("red", "✗ ")}


def show_note(text: str, kind: str = "info") -> None:
    """一行提示。info = 陈述；warn = 你没按提示来 / 有东西被回退；error = 这一轮废了。"""
    color, prefix = _NOTE_STYLE.get(kind, _NOTE_STYLE["info"])
    console.print(f"[{color}]{prefix}{text}[/]")


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
            detail = tool_detail(args)
            if r.get("error"):
                detail = f"{detail} {r['error']}".strip()
            table.add_row(str(i), ts, f"⚙  {r.get('tool')}", f"{status} · {detail[:50]}", _fmt_ms(r.get("duration_ms")))
        elif kind == "approval":
            decision = {"approved": "已批准", "denied": "已驳回"}.get(r.get("decision"), str(r.get("decision")))
            table.add_row(str(i), ts, "⚠  审批", f"{decision} · {str(r.get('command', ''))[:40]}", _fmt_ms(r.get("duration_ms")))
        elif kind == "hook":
            verdict = "拦下" if r.get("verdict") == "deny" else "放行"
            reason = f" · {r.get('reason', '')}" if r.get("reason") else ""
            table.add_row(str(i), ts, "⚡ 策略", f"{verdict}（{r.get('by', '')}）{reason}"[:60], "")
        elif kind == "compact":
            # 有结构化数字就显示「前 → 后」，否则截一句——表格里别塞整句话
            if "before" in r and "after" in r:
                detail = f"{r['before']} → {r['after']} 字符"
            else:
                detail = str(r.get("text", ""))[:36]
            table.add_row(str(i), ts, "⟳  压缩", detail, "")
        elif kind == "oversized":
            table.add_row(str(i), ts, "⚠  超大", f"{r.get('bytes', '?')} 字节，超出读取上限（全文在文件里）", "")
    console.print(table)
    console.print("[dim]看某条全文：/trace N[/]")


TRACE_DETAIL_CHARS = TRACE_FIELD_CHARS  # 与网页同一个数（trace.py 定义）


def show_trace_event(rec: dict, idx: int) -> None:
    """单条事件的完整内容（/trace N）——总览表里钻进去看全文。

    内容走 Text（不走 markup 解析）——工具结果里带 [exit=0] 这类方括号是常态。
    超长字段截断（trace 文件本身仍是全文）：看全文请直接开存档目录里的 .trace.jsonl。
    """
    body = Text()
    body.append(f"#{idx} · {rec.get('kind', '')}\n", style="bold")
    for k, v in rec.items():
        text_v = v if isinstance(v, str) else str(v)
        body.append(f"{k}：", style="dim")
        if len(text_v) > TRACE_DETAIL_CHARS:
            body.append(f"{text_v[:TRACE_DETAIL_CHARS]}\n")
            body.append(ellipsis_note(len(text_v)) + "\n", style="yellow")
        else:
            body.append(f"{text_v}\n")
    console.print(Panel(body, title="轨迹明细", title_align="left", border_style="dim"))


def show_tool(name: str, args: dict | None = None) -> None:
    global _waiting
    _waiting = False
    detail = tool_detail(args)
    if len(detail) > 50:
        detail = detail[:50] + "…"
    line = f"⚙  {name}{': ' + detail if detail else ''}"
    if _live:
        _seal_current()
        _blocks.append(Text(line, style="dim"))
        _refresh()
    else:
        console.print(f"[dim]{line}[/]")  # Live 外（如历史渲染）直接打印


def show_notice(text: str) -> None:
    """模型层的提示（上下文被压缩、413 重试之类）——插进流里一行，不打断阅读。"""
    global _waiting
    _waiting = False
    line = Text("⟳ " + text, style="yellow")
    if _live:
        _seal_current()
        _blocks.append(line)
        _refresh()
    else:
        console.print(line)


def _approval_session() -> PromptSession:
    """审批专用输入会话：erase_when_done——答完自动擦提示行（不擦会留屏，像「又弹了一次」）。"""
    global _approval
    if _approval is None:
        _approval = PromptSession(erase_when_done=True, key_bindings=_approval_key_bindings())
    return _approval


ESC_SENTINEL = "\x1b"  # Esc 的返回值（prompt_toolkit 的 app.exit 原样带回）


def _approval_key_bindings():
    """Esc = 驳回。Enter 是 prompt_toolkit 默认的"提交"键，不用绑。"""
    kb = KeyBindings()

    @kb.add("escape", eager=True)
    def _esc(event):
        event.app.exit(result=ESC_SENTINEL)

    return kb


def _to_decision(raw: str | None) -> str:
    """输入 → 决定。空回车 = 批准（Enter 是"同意"的自然动作）；
    其余任何东西（n / Esc / 打错字）= 驳回——打错字不该变成放行。
    """
    if raw is None:
        return "deny"
    val = raw.strip().lower()
    if val == "a":
        return "always"
    if val in ("", "y"):
        return "approve"
    return "deny"


def show_approval(command: str, reason: str = "", timeout: float = 300.0) -> str:
    """人工审批：先说「为什么问你」，再给完整命令，然后等一个键。

    返回 "approve" / "deny" / "always"（always = 本会话不再问这个程序）。

    只给命令不够——`find /c /v "" README.md` 看着像只读统计，在 GNU find 语义下
    却会遍历整盘。判定原因必须跟着命令一起递到人眼前，否则审批只是走过场。

    timeout：没人答复就按驳回处理（0 = 一直等）。人走开了不该把整台机器卡死。
    """
    hint = "Enter 执行 · Esc 驳回 · a 记住(本会话) "
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

    console.print(Text(f"⚠  {reason or '需要确认'}", style="yellow"))
    console.print(Text("  " + command, style="dim"))  # 全文——审批不该藏东西

    timed_out = False
    timers: list = []  # 人先答了就取消——否则这个 timer 会留到下一次 prompt
    sess = _approval_session() if interactive() else None

    def _arm_timeout(*_args) -> None:
        """到点自动收掉提示行（timeout <= 0 = 不超时）。

        签名写成 `*_args` 是踩出来的：不同版本的 prompt_toolkit 调 pre_run 时
        传不传 Application 并不一致（真机上是零参，直接 TypeError 把整轮打死了）。
        所以不依赖参数，从 session 上取 app。
        """
        if timeout <= 0 or sess is None:
            return
        app = getattr(sess, "app", None) or (_args[0] if _args else None)
        if app is None:
            return

        def _fire() -> None:
            nonlocal timed_out
            timed_out = True
            app.exit(None)  # 空决定 → 驳回

        try:
            timers.append(asyncio.get_running_loop().call_later(timeout, _fire))
        except Exception:  # 定时都挂不上就算了——超时是贴心，不是必需
            pass

    raw: str | None = "n"  # 默认驳回——异常路径也有一张安全票
    try:
        if sess is None:
            raw = input(hint + "❯ ")
        else:
            raw = sess.prompt(
                message=[("fg:ansiyellow bold", hint + "❯ ")], pre_run=_arm_timeout,
            )
    except EOFError:
        raw = None  # 输入读尽（管道喂完 / Ctrl+Z）——按驳回
    except Exception as e:  # 审批界面自己坏了：按驳回处理，别把这一轮打死
        console.print(Text(f"⚠  审批界面出错（{type(e).__name__}: {e}），按驳回处理", style="red"))
        raw = None
    except BaseException:  # Ctrl+C 等：Live 先归位再抛
        if restart:
            _live_open()
        raise
    finally:
        for t in timers:
            t.cancel()

    decision = _to_decision(raw)
    if timed_out:
        console.print(Text(f"⚠  审批超时（{timeout:g} 秒没人答复），按驳回处理", style="yellow"))
        decision = "deny"

    # 此时 Live 仍停着——这一行永久落屏（审批的屏幕侧留痕；文件侧在 trace）
    # 不重复命令：上面两行已经印着原因和全文，这行只记「按了哪个键」
    label = {"approve": "已批准", "always": "已批准（本会话不再问）", "deny": "已驳回"}[decision]
    tail = f" · {reason}" if reason else ""
    console.print(f"[yellow]⚠  审批：{label}{tail}[/]")
    if restart:
        _live_open()
    return decision
