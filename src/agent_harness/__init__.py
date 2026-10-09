"""Agent Harness — 用代码造一台会自己干活的 agent 机器。"""

import time
from pathlib import Path

from . import tui
from .assembly import (
    WORKSPACE,
    build_system_prompt,
    list_visible_sessions,
    load_settings_and_llm,
    tools,
)
from .session import list_sessions, new_session_id, load_session, save_messages
from .loop import agent_loop
from .llm import Msg, DeepSeekLLM

def choose_session(workspace: Path) -> tuple[str, list[Msg]]:
    """列历史会话让用户选；返回 (session_id, history)。"""
    if not tui.interactive():
        return new_session_id(workspace), []          # 非终端（管道喂任务）：选择没意义，直接新会话
    sessions = list_visible_sessions()
    if not sessions:
        return new_session_id(workspace), []          # 首次：开新会话
    print("历史会话：")
    for i, s in enumerate(sessions[:10], 1):          # 最多列 10 个
        t = time.strftime("%m-%d %H:%M", time.localtime(s["mtime"]))
        print(f"  {i}. [{t}] {s['first_user_msg'] or '(空会话)'}")
    print("  0. 新会话")
    try:
        choice = input("选择> ").strip()
    except EOFError:                                   # 终端读尽（Ctrl+Z 回车）——开新会话
        return new_session_id(workspace), []
    if choice == "0" or not choice:
        return new_session_id(workspace), []
    try:
        s = sessions[int(choice) - 1]
    except (ValueError, IndexError):
        print("输入无效，开新会话。")
        return new_session_id(workspace), []
    return s["id"], load_session(workspace, s["id"])

def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Agent Harness CLI")
    ap.add_argument("--model", help="模型 profile 名（配置 [models.*]；缺省用 default）")
    args = ap.parse_args()

    base_prompt = build_system_prompt()
    llm, settings = load_settings_and_llm(base_prompt, args.model)  # 可切换门面（流式）
    approver = lambda cmd: tui.show_approval(cmd).strip().lower() == "y"

    session_id, history = choose_session(WORKSPACE)
    tui.show_banner(session_id, len(history), llm.current)
    if history:
        tui.show_history(history)  # 恢复的会话：把历史渲染出来
    while True:
        try:
            goal = tui.ask().strip()
        except EOFError:
            break  # 输入读尽（管道喂完 / Ctrl+Z 回车）——收工退出，不是错误
        if goal in ("exit", "quit"):
            break
        if goal == "/model":  # 运行时切换模型
            tui.show_models(llm.profiles, llm.current_profile)
            pick = tui.ask().strip()
            if pick.isdigit() and 1 <= int(pick) <= len(llm.profiles):
                tui.show_note(f"已切换到 {llm.switch(llm.profiles[int(pick) - 1])}")
            continue
        if not goal:
            continue
        tui.show_user(goal)
        tui.begin_stream()
        old_len = len(history)
        reply, history = agent_loop(
            goal, llm, tools, base_prompt,
            approver=approver, history=history,
            on_text=tui.stream_chunk, on_tool=tui.show_tool,
            on_reasoning=tui.reasoning_chunk,
            max_steps=settings.max_steps,
        )
        tui.end_stream()
        save_messages(WORKSPACE, session_id, history[old_len:])


