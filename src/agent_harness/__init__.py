"""Agent Harness — 用代码造一台会自己干活的 agent 机器。"""

import time
from pathlib import Path

from agent_harness.config import load_rules

from .session import list_sessions, new_session_id, load_session, save_messages
from .loop import agent_loop
from .llm import Action, Msg, DeepSeekLLM
from .tools import read_file, write_file, count_lines, run_shell, edit_file
from .guardrail import safe_tool
from .sensors import run_tests

SYSTEM_PROMPT = (
    "你是 agent-harness，一台会自己干活的 coding agent，工作目录是项目根。\n\n"
    "不必理会项目根目录下的CLAUDE.md 和 README.md。\n\n"
    "运行环境：Windows（shell 是 cmd.exe）——用本平台原生命令。\n\n"
    "工具使用策略：\n"
    "- 读代码/文件用 read_file。\n"
    "- 修改现有文件优先用 edit_file（old→new 精确替换，保留其余内容）；只有新建文件或整体重写才用 write_file（⚠️ 覆盖整个文件，会丢失未包含的内容）。\n"
    "- 改完代码用 run_tests 验证：红了看报告改，绿了才收工——不要没验证就宣称完成。\n"
    "- 跑其他命令用 run_shell；危险命令会被拦截并询问用户。\n"
    "- 只能在项目目录内活动，越界路径会被拦截。\n\n"
    "任务完成后直接回复总结（不再调用工具）。"
)

WORKSPACE = Path.cwd()   # 工作目录 = 项目根

tools = {
    "read_file": safe_tool(read_file, WORKSPACE),
    "write_file": safe_tool(write_file, WORKSPACE),
    "edit_file": safe_tool(edit_file, WORKSPACE),
    "count_lines": safe_tool(count_lines, WORKSPACE),
    "run_shell": run_shell,   # shell 不包——命令危险面归 H 护栏管
    "run_tests": run_tests,  # pytest 判红绿
}

def choose_session(workspace: Path) -> tuple[str, list[Msg]]:
    """列历史会话让用户选；返回 (session_id, history)。"""
    sessions = list_sessions(workspace)
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
    base_prompt = SYSTEM_PROMPT + "\n\n" + load_rules(WORKSPACE)
    DeepSeekllm = DeepSeekLLM(base_prompt)  # 真模型适配器，按官方 API 循环
    approver = lambda cmd: input(f"⚠️ 允许执行「{cmd}」吗？(y/n) ").strip().lower() == "y"

    print("Agent Harness · S5 真模型")
    print("输入任务回车执行；exit 退出。")
    session_id, history = choose_session(WORKSPACE)
    print(f"会话 {session_id}（{len(history)} 条历史消息）")
    while True:
        goal = input("\n你> ").strip()
        if goal in ("exit", "quit"):
            break
        if not goal:
            continue
        old_len = len(history)
        reply, history = agent_loop(goal, DeepSeekllm, tools, SYSTEM_PROMPT, approver, history)
        save_messages(WORKSPACE, session_id, history[old_len:])
        print(f"Agent> {reply}")

    # for h in history:
    #     print(f"{h.role}> {h.content}\n {h.tool_call_id or ''}\n {h.tool_calls or ''}")


