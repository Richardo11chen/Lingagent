"""Agent Harness — 用代码造一台会自己干活的 agent 机器。"""

import time
from pathlib import Path

from . import tui
from .assembly import (
    POST_HOOKS,
    PRE_HOOKS,
    WORKSPACE,
    build_session_tools,
    build_system_prompt,
    list_visible_sessions,
    load_settings_and_llm,
)
from .guardrail import command_head
from .session import (
    list_sessions,
    list_trace_files,
    load_session,
    load_session_model,
    new_session_id,
    prune_empty_sessions,
    save_messages,
    save_session_model,
    trace_path,
)
from .loop import agent_loop
from .llm import Msg, DeepSeekLLM
from .trace import Tracer, days_since_viewed, load_trace, mark_viewed
from .weblaunch import ensure_web

def choose_session(workspace: Path) -> tuple[str, list[Msg]]:
    """列历史会话让用户选（分页，每页 10 条）；返回 (session_id, history)。"""
    if not tui.interactive():
        return new_session_id(workspace), []          # 非终端（管道喂任务）：选择没意义，直接新会话
    sessions = list_visible_sessions()
    if not sessions:
        return new_session_id(workspace), []          # 首次：开新会话

    per_page = 10
    page = 1
    while True:
        total_pages = (len(sessions) + per_page - 1) // per_page
        page = min(page, total_pages)
        chunk = sessions[(page - 1) * per_page: page * per_page]
        rows = [
            {
                "time": time.strftime("%m-%d %H:%M", time.localtime(s["mtime"])),
                "model": load_session_model(workspace, s["id"]) or "-",
                "preview": (s["first_user_msg"] or "(空会话)").replace("\n", " ")[:40],
            }
            for s in chunk
        ]
        tui.show_sessions(rows, page, total_pages, len(sessions))
        try:
            choice = input("选择> ").strip().lower()
        except EOFError:                               # 终端读尽（Ctrl+Z 回车）——开新会话
            return new_session_id(workspace), []
        if choice in ("0", ""):
            return new_session_id(workspace), []       # 新会话
        if choice == "n" and page < total_pages:
            page += 1
        elif choice == "p" and page > 1:
            page -= 1
        elif choice.isdigit() and 1 <= int(choice) <= len(chunk):
            s = chunk[int(choice) - 1]
            return s["id"], load_session(workspace, s["id"])
        else:
            tui.show_note("没这个选项——按提示来", "warn")

NUDGE_DAYS = 30  # 轨迹这么久没人看，就该考虑清掉或关掉


def _trace_nudge(settings) -> None:
    """轨迹堆着没人看时提醒一句（只提示：删/关都由人决定）。"""
    if not settings.trace_enabled:
        return
    days = days_since_viewed()
    if days is None:  # 从没看过：拿最早的轨迹文件当基准，别对新项目唠叨
        files = list_trace_files(WORKSPACE)
        if not files:
            return
        try:
            days = (time.time() - min(f.stat().st_mtime for f in files)) / 86400
        except OSError:
            return
    if days >= NUDGE_DAYS:
        tui.show_note(
            f"轨迹已 {int(days)} 天没打开过——清空间：uv run agent-harness --clean-trace 30；"
            f"不需要就在配置里设 [trace] enabled = false",
            "warn",
        )


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Agent Harness CLI")
    ap.add_argument("--model", help="模型 profile 名（配置 [models.*]；缺省用 default）")
    ap.add_argument(
        "--clean-trace", type=int, metavar="DAYS",
        help="删掉 DAYS 天前的轨迹文件（0 = 全部），腾空间；不启动会话",
    )
    args = ap.parse_args()

    if args.clean_trace is not None:  # 清理不该要求 API key
        from .session import SESSION_ROOT
        from .trace import clean_traces

        n, freed = clean_traces(SESSION_ROOT, older_than_days=args.clean_trace)
        print(f"清掉 {n} 个轨迹文件，释放 {freed / 1048576:.1f} MB")
        return

    base_prompt = build_system_prompt()
    llm, settings = load_settings_and_llm(  # 可切换门面（流式）
        base_prompt, args.model, on_notice=tui.show_notice,
    )
    # 本会话「不再问」的程序名（只在内存里，重启即忘——安全边界清楚）
    session_allowed: set[str] = set()

    def approver(cmd: str, reason: str) -> bool:
        head = command_head(cmd)
        if head and head in session_allowed:
            tui.show_note(f"本会话已允许 {head}，直接执行")  # 留痕：不是静默放行
            return True
        decision = tui.show_approval(cmd, reason, timeout=settings.approval_timeout)
        if decision == "always" and head:
            session_allowed.add(head)
            tui.show_note(f"本会话不再询问 {head}", "info")
        return decision != "deny"

    web_started: list[str] = []
    if settings.web_autostart and tui.interactive():  # 管道/脚本模式不拉网页端
        web_started = ensure_web(settings.server_port, WORKSPACE, WORKSPACE / "web")

    prune_empty_sessions(WORKSPACE)  # 打扫：清掉「开了没聊」的空占位（不阻塞——顺手的事）
    session_id, history = choose_session(WORKSPACE)
    # 会话级模型（用户定）：打开哪个会话用它上次用的模型；--model 显式指定优先
    saved = load_session_model(WORKSPACE, session_id)
    if saved and saved not in settings.models:
        tui.show_note(f"会话原用模型 {saved} 已不在配置里，回退默认", "warn")
    target = args.model or (saved if saved in settings.models else None)
    if target and target != llm.current_profile:
        llm.switch(target)
    if args.model:  # 显式指定 → 落账（这个会话从此认它）
        save_session_model(WORKSPACE, session_id, llm.current_profile)
    # S8：轨迹与 session 同目录同 id；关掉 [trace] enabled 就给空对象（不写盘）
    tracer = Tracer(trace_path(WORKSPACE, session_id)) if settings.trace_enabled else Tracer()

    def _notice(text: str, **fields) -> None:
        """压缩这类提示：给人看一眼（流里一行），同时记进 trace（翻账时能看到）。"""
        tui.show_notice(text)
        tracer.emit("compact", text=text, **fields)

    llm.set_notice(_notice)
    session_tools = build_session_tools(  # S10：本会话工具表（带 spawn_subagent）
        llm, base_prompt, approver=approver, tracer=tracer,
        max_steps=settings.max_steps, on_hook=tui.show_hook,
        allow_commands=settings.guardrail_allow,
    )
    tui.show_banner(session_id, len(history), llm.current)
    if history:
        tui.show_history(history)  # 恢复的会话：把历史渲染出来
    tui.show_web_hint(web_started)  # 链接放历史之后（长历史时不用往上翻）
    _trace_nudge(settings)
    while True:
        try:
            goal = tui.ask().strip()
        except EOFError:
            break  # 输入读尽（管道喂完 / Ctrl+Z 回车）——收工退出，不是错误
        if goal in ("exit", "quit"):
            break
        if goal == "/trace" or goal.startswith("/trace "):  # S8：轨迹时间线（只读投影）
            recs = load_trace(trace_path(WORKSPACE, session_id))
            mark_viewed()  # 体温计：这个功能还有人在用吗
            arg = goal[len("/trace"):].strip()
            if not arg:
                tui.show_trace(recs, session_id)      # 总览
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
                tui.show_note(f"已切换到 {llm.switch(name)}")
                save_session_model(WORKSPACE, session_id, name)  # 记在会话上，下次打开还认
            elif pick:  # 空输入 = 取消（面板上写了）；打了别的东西 = 说清楚，别静默吞
                tui.show_note("没这个序号——按提示来", "warn")
            continue
        if not goal:
            continue
        tui.show_user(goal)
        tui.begin_stream()
        old_len = len(history)
        try:
            reply, history = agent_loop(
                goal, llm, session_tools, base_prompt,
                approver=approver, history=history,
                on_text=tui.stream_chunk, on_tool=tui.show_tool,
                on_reasoning=tui.reasoning_chunk,
                tracer=tracer,
                pre_hooks=PRE_HOOKS, post_hooks=POST_HOOKS,
                on_hook=tui.show_hook,
                max_steps=settings.max_steps,
                allow_commands=settings.guardrail_allow,
            )
        except KeyboardInterrupt:  # Ctrl+C：结束本轮，回到输入行（不是退出程序）
            tui.end_stream()
            tui.show_note("已中断本轮（这一轮的历史未落盘）", "warn")
            continue
        except Exception as e:  # 网络断了 / 模型报错 / 参数解析炸了——报一句，保住会话
            # history 是被 agent_loop 换成新列表的（history + [...]），失败时原列表没被改写
            tui.end_stream()
            tui.show_note(f"本轮失败：{type(e).__name__}: {e}", "error")
            continue
        tui.end_stream()
        save_messages(WORKSPACE, session_id, history[old_len:])


