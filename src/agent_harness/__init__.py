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
    sessions = list_visible_sessions()
    if not sessions:
        return new_session_id(workspace), []          # 首次：开新会话
    print("历史会话：")
    for i, s in enumerate(sessions[:10], 1):          # 最多列 10 个
        t = time.strftime("%m-%d %H:%M", time.localtime(s["mtime"]))
        print(f"  {i}. [{t}] {s['first_user_msg'] or '(空会话)'}")
    print("  0. 新会话")
    choice = input("选择> ").strip()
    if choice == "0" or not choice:
        return new_session_id(workspace), []
    try:
        s = sessions[int(choice) - 1]
    except (ValueError, IndexError):
        print("输入无效，开新会话。")
        return new_session_id(workspace), []
    return s["id"], load_session(workspace, s["id"])

def main() -> None:
    base_prompt = build_system_prompt()
    DeepSeekllm, settings = load_settings_and_llm(base_prompt)  # 按配置文件构造（流式）
    approver = lambda cmd: tui.show_approval(cmd).strip().lower() == "y"

    session_id, history = choose_session(WORKSPACE)
    tui.show_banner(session_id, len(history))
    if history:
        tui.show_history(history)  # 恢复的会话：把历史渲染出来
    while True:
        goal = tui.ask().strip()
        if goal in ("exit", "quit"):
            break
        if not goal:
            continue
        tui.show_user(goal)
        tui.begin_stream()
        old_len = len(history)
        reply, history = agent_loop(
            goal, DeepSeekllm, tools, base_prompt,
            approver=approver, history=history,
            on_text=tui.stream_chunk, on_tool=tui.show_tool,
            on_reasoning=tui.reasoning_chunk,
            max_steps=settings.max_steps,
        )
        tui.end_stream()
        save_messages(WORKSPACE, session_id, history[old_len:])


