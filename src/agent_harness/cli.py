"""CLI 入口：参数分派 → 装配 → 会话选择 → REPL → 收尾。

从包根搬出来（原来叫 `__init__.py`）：包根一被 import 就会拖进 prompt_toolkit / rich /
subprocess，「import agent_harness 只为拿一个 Msg」也要付整台终端的代价。
现在 `agent_harness/__init__.py` 只剩身份说明，应用主体在这里，入口点见 pyproject。
"""

import signal
import sys
import time
from pathlib import Path

from . import tui
from .approval import SessionAllow
from .assembly import WORKSPACE, build_system_prompt, load_settings_and_llm
from .runtime import SessionRuntime, choices, clean_trace, prune, trace_age_days
from .shell import detect as detect_shell
from .trace import days_since_viewed
from .turn import TurnCallbacks, describe_failure
from .weblaunch import WebRun, clear_state, ensure_web, release_lock, stop_run, wait_until_idle


def choose_session(workspace: Path, default_profile: str = "") -> str | None:
    """列历史会话让用户选（分页，每页 10 条）；返回 session_id，None = 开新会话。

    只回答问题，不问"怎么建"——新建/恢复的差异全在 `SessionRuntime` 里，
    这里多写一行，两个入口就又分叉一次。
    """
    if not tui.interactive():
        return None  # 非终端（管道喂任务）：选择没意义，直接新会话
    sessions = choices(workspace, default_profile)
    if not sessions:
        return None  # 首次：开新会话

    per_page = 10
    page = 1
    while True:
        total_pages = (len(sessions) + per_page - 1) // per_page
        page = min(page, total_pages)
        chunk = sessions[(page - 1) * per_page: page * per_page]
        rows = [
            {
                "time": time.strftime("%m-%d %H:%M", time.localtime(s.mtime)),
                "model": s.model,
                "preview": s.preview[:40],
            }
            for s in chunk
        ]
        tui.show_sessions(rows, page, total_pages, len(sessions))
        try:
            choice = input("选择> ").strip().lower()
        except EOFError:  # 终端读尽（Ctrl+Z 回车）——开新会话
            return None
        if choice in ("0", ""):
            return None  # 新会话
        if choice == "n" and page < total_pages:
            page += 1
        elif choice == "p" and page > 1:
            page -= 1
        elif choice.isdigit() and 1 <= int(choice) <= len(chunk):
            return chunk[int(choice) - 1].id
        else:
            tui.show_note("没这个选项——按提示来", "warn")

NUDGE_DAYS = 30  # 轨迹这么久没人看，就该考虑清掉或关掉

class SoftStop:
    """Ctrl+C 的软中断：第一次「跑完这一步就停」，第二次才是硬中断。

    为什么不能直接让 KeyboardInterrupt 穿出去：它会从当前字节码处炸开，把正在跑的这一步
    连同整轮 history 一起丢掉。而 `should_stop` 只在**每步开始**检查（loop.py），所以想要
    「本步跑完再停、已完成的照常落盘」，就得让第一次 Ctrl+C 只置位、不抛异常。

    限制（诚实说）：Windows 上 Ctrl+C 是控制台事件，子进程也会收到，所以正在跑的 shell
    命令仍可能被打断。软中断保证的是「不再发起新的一步，且这一轮已有的结果照常落盘」。
    """

    def __init__(self) -> None:
        self.hits = 0
        self.requested = False

    def reset(self) -> None:
        self.hits = 0
        self.requested = False

    def __call__(self, signum=None, frame=None) -> None:
        self.hits += 1
        if self.hits == 1:
            self.requested = True
            # 不用 rich/console：信号处理器里碰控制台的锁可能死锁
            sys.stderr.write("\n[中断] 跑完这一步就停（再按一次立刻中断）\n")
            sys.stderr.flush()
            return
        raise KeyboardInterrupt


def _trace_nudge(settings) -> None:
    """轨迹堆着没人看时提醒一句（只提示：删/关都由人决定）。"""
    if not settings.trace_enabled:
        return
    days = days_since_viewed()
    if days is None:  # 从没看过：拿最早的轨迹文件当基准，别对新项目唠叨
        days = trace_age_days(WORKSPACE.root)
        if days is None:
            return
    if days >= NUDGE_DAYS:
        tui.show_note(
            f"轨迹已 {int(days)} 天没打开过——清空间：uv run agent-harness --clean-trace 30；"
            f"不需要就在配置里设 [trace] enabled = false",
            "warn",
        )


def _let_web_finish(settings) -> None:
    """关后端之前，等网页上正在跑的那一轮跑完。

    不等的话那一轮会被拦腰砍断：工具可能正改文件、trace 停在半截、人在浏览器里等一个永远不来的
    回复。等是有上限的（`[web] stop_wait`），Ctrl+C 可以当场不等。
    """
    if settings.web_stop_wait <= 0:
        return

    def on_wait(busy: int) -> None:
        tui.console.print(
            f"[dim]网页上还有 {busy} 轮在跑——等它写完再关"
            f"（最多 {settings.web_stop_wait:g} 秒，Ctrl+C 直接关）[/]"
        )

    try:
        wait_until_idle(settings.server_port, settings.web_stop_wait, on_wait=on_wait)
    except KeyboardInterrupt:
        tui.console.print("[dim]不等了，直接关[/]")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        description="Lingagent —— 在任意目录启动的 coding agent",
        epilog="不带参数 = 终端界面（-tui）；lingan -web = 只起后端 + 网页界面。",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "-tui", "--tui", action="store_true",
        help="终端界面（默认，不加这个也是一样）",
    )
    mode.add_argument(
        "-web", "--web", action="store_true",
        help="只起后端 + 网页界面（不开终端）",
    )
    ap.add_argument("--model", help="模型 profile 名（配置 [models.*]；缺省用 default）")
    ap.add_argument(
        "--clean-trace", type=int, metavar="DAYS",
        help="删掉 DAYS 天前的轨迹文件（0 = 全部），腾空间；不启动会话",
    )
    ap.add_argument(
        "--stop-web", action="store_true",
        help="结束占用网页端端口（后端 + 前端 dev server）的进程；不启动会话",
    )
    ap.add_argument(
        "--init", action="store_true",
        help="首启向导：交互式生成 ~/.agent-harness/config.toml；不启动会话",
    )
    args = ap.parse_args()

    if args.init:  # 也不该要求 API key——它就是来写 key 的
        from .config_wizard import run_init

        raise SystemExit(run_init())

    if args.stop_web:  # 也不该要求 API key
        from .settings import load_settings
        from .weblaunch import FRONTEND_PORT, stop_web  # 局部导入：这两个只在 --stop-web 时才需要

        for line in stop_web([load_settings().server_port, FRONTEND_PORT]):
            print(line)
        clear_state()  # 记录里那些 pid 已经被上面收掉了，别留给下次启动去"认领"
        return

    if args.clean_trace is not None:  # 清理不该要求 API key
        n, freed = clean_trace(args.clean_trace)
        print(f"清掉 {n} 个轨迹文件，释放 {freed / 1048576:.1f} MB")
        return

    if args.web:  # lingan -web：只起后端 + 网页，不进终端界面
        from .server import run_server

        run_server(args.model)
        return

    base_prompt = build_system_prompt()
    llm, settings = load_settings_and_llm(  # 可切换门面（流式）
        base_prompt, args.model, on_notice=tui.show_notice,
    )
    def _ask(command: str, reason: str) -> str:
        """终端怎么问：按键。记忆、自动批准、判定都在 SessionAllow 里，两个入口共用。"""
        return tui.show_approval(command, reason, timeout=settings.approval_timeout)

    def _on_auto(command: str, reason: str) -> None:
        tui.show_auto(reason)
        # 自动批准也要留账：屏幕上那行会滚走，trace 才是事后能查的地方。
        # runtime 在下面才建——晚绑定是安全的：这个回调只在一轮跑起来之后才会被调。
        runtime.record_auto(command, reason)

    approver = SessionAllow(
        _ask,
        on_granted=lambda head: tui.show_note(f"本会话不再询问 {head}", "info"),
        on_reused=lambda head: tui.show_note(f"本会话已允许 {head}，直接执行"),
        on_auto=_on_auto,
    )
    # Shift+Tab 与工具栏都从这里读写——自动批准不再有第二份状态
    tui.set_auto_hooks(lambda: approver.auto, approver.toggle_auto)

    web = WebRun()
    if settings.web_autostart and tui.interactive():  # 管道/脚本模式不拉网页端
        web = ensure_web(settings.server_port, WORKSPACE.root)

    prune(WORKSPACE.root)  # 打扫：清掉「开了没聊」的空占位（不阻塞——顺手的事）
    # 会话级装配（id / 历史 / 轨迹 / 该用哪个模型）全交给 runtime：
    # choose_session 只回答"选哪个"，怎么打开是它的事
    runtime = SessionRuntime(
        llm=llm, settings=settings, approver=approver, workspace=WORKSPACE.root,
        session_id=choose_session(WORKSPACE.root, settings.default_model),  # None = 新会话
    )
    # --model 显式指定压过会话记忆，并落账到这个会话上（用户这次就是冲着它来的）
    notice = runtime.restore_model(explicit=args.model)
    if notice:
        tui.show_note(notice, "warn")

    # 底部显示上下文占比：分子是「系统提示 + 历史」的字符当量，分母是压缩阈值。
    # runtime 每轮跑完自己更新提示词那份缓存——这里不再手动喂它。
    tui.set_context_probe(runtime.usage)

    tui.show_banner(runtime.session_id, len(runtime.history), llm.current, detect_shell().name)
    if runtime.history:
        tui.show_history(runtime.history)  # 恢复的会话：把历史渲染出来
    tui.show_web_hint(web.started, web.warning, web.reaped, web.ui_port)  # 放历史之后，不用往上翻
    _trace_nudge(settings)
    try:
        while True:
            try:
                goal = tui.ask().strip()
            except EOFError:
                break  # 输入读尽（管道喂完 / Ctrl+Z 回车）——收工退出，不是错误
            if goal in ("exit", "quit"):
                break
            if goal == "/trace" or goal.startswith("/trace "):  # S8：轨迹时间线（只读投影）
                recs = runtime.trace_records()
                arg = goal[len("/trace"):].strip()
                if not arg:
                    tui.show_trace(recs, runtime.session_id)      # 总览
                elif arg.isdigit() and 1 <= int(arg) <= len(recs):
                    tui.show_trace_event(recs[int(arg) - 1], int(arg))  # 钻取单条全文
                else:
                    tui.show_note(f"没有 #{arg} 这条事件（当前共 {len(recs)} 条）", "warn")
                continue
            if goal == "/model":  # 运行时切换模型（会话级——落盘，退出不丢）
                tui.show_models(llm.profiles, llm.current_profile)
                pick = tui.ask().strip()
                if pick.isdigit() and 1 <= int(pick) <= len(llm.profiles):
                    name = llm.profiles[int(pick) - 1]
                    tui.show_note(f"已切换到 {runtime.switch_model(name)}")
                elif pick:  # 空输入 = 取消（面板上写了）；打了别的东西 = 说清楚，别静默吞
                    tui.show_note("没这个序号——按提示来", "warn")
                continue
            if not goal:
                continue
            tui.show_user(goal)
            tui.begin_stream()
            soft = SoftStop()
            # 只在跑这一轮时装软中断：输入行那边 prompt_toolkit 自己管 Ctrl+C，别抢
            prev_sigint = signal.signal(signal.SIGINT, soft)
            try:
                # 落盘也在 runtime.run 里：跑成功才存这一轮新增的消息
                runtime.run(
                    goal,
                    callbacks=TurnCallbacks(
                        on_text=tui.stream_chunk,
                        on_tool=tui.show_tool,
                        on_tool_result=tui.show_tool_result,
                        on_reasoning=tui.reasoning_chunk,
                        on_hook=tui.show_hook,
                        on_notice=tui.show_notice,
                        on_child_text=tui.child_text,
                        on_child_tool=tui.child_tool,
                        on_child_tool_result=tui.child_tool_result,
                    ),
                    # 绑默认参数：lambda 捕获的是**变量**，而 soft 每轮都会被重新赋值。
                    # 今天 run_turn 是同步调用所以撞不上，但这属于"哪天有人把 should_stop
                    # 存起来延后调用"就会静默取到最后一个 soft 的坑。
                    should_stop=lambda s=soft: s.requested,
                )
            except KeyboardInterrupt:  # 第二次 Ctrl+C：硬中断，这一轮不落盘
                tui.end_stream()
                tui.show_note("已中断本轮（这一轮的历史未落盘）", "warn")
                continue
            except Exception as e:  # 网络断了 / 模型报错 / 参数解析炸了——报一句，保住会话
                # history 是被 agent_loop 换成新列表的（history + [...]），失败时原列表没被改写
                tui.end_stream()
                tui.show_note(describe_failure(e), "error")
                continue
            finally:
                signal.signal(signal.SIGINT, prev_sigint)
            tui.end_stream()
    finally:
        # 关 TUI 就连带收掉网页端：留着它们会变成孤儿，下次启动按端口判断会复用旧代码
        # （2026-10-24 就是这么卡住的：新按钮点了没反应，8000 上还是上个世纪的进程）。
        # 只收自己拉起的——端口上原有的可能是另一个 TUI 或用户自己在用，不动。
        try:
            if web.procs and settings.web_stop_on_exit:
                try:
                    _let_web_finish(settings)  # 网页上还有一轮就先等它跑完，别拦腰砍断
                    tui.console.print(f"[dim]网页端已关闭：{'、'.join(stop_run(web))}[/]")
                    clear_state()
                except KeyboardInterrupt:
                    # 第三次 Ctrl+C 会落在这里（第二次已经触发 EOFError、进入收尾）——
                    # 再打断清理只会打一条 traceback，没有任何好处。
                    pass
        finally:
            if web.lock_held:
                release_lock()  # 不收别人的锁：release_lock 只认自己 pid
