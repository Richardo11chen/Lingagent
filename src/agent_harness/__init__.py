"""Agent Harness — 用代码造一台会自己干活的 agent 机器。"""

from pathlib import Path
from .loop import agent_loop
from .llm import Action, LLM
from .tools import read_file, write_file, count_lines, run_shell
from .guardrail import safe_tool
from .sensors import run_sensors

SYSTEM_PROMPT = (
    "你是 agent-harness，一台会自己干活的 coding agent。\n"
    "你可以使用工具读写文件。任务完成时发出 done。"
)

WORKSPACE = Path.cwd()   # 工作目录 = 项目根

tools = {
    "read_file": safe_tool(read_file, WORKSPACE),
    "write_file": safe_tool(write_file, WORKSPACE),
    "count_lines": safe_tool(count_lines, WORKSPACE),
    "run_shell": run_shell,   # shell 不包——命令危险面归 H 护栏管
}

def main() -> None:
    script = [
        Action("call_tool", "run_shell", {"command": "pytest test_demo.py -q"}),
        Action("call_tool", "write_file", {"path": "../evil.txt", "content": "hack"}),  # 越界 → 拦截
        Action("call_tool", "write_file", {"path": "ok.txt", "content": "fine"}),       # 界内 → 放行
    ]
    llm = LLM(script)
    approver = lambda cmd: input(f"⚠️ 允许执行「{cmd}」吗？(y/n) ").strip().lower() == "y"

    print("Agent Harness · S3 治理护栏演示")
    print("输入任务回车执行；exit 退出。")
    history: list[tuple[str, str]] = []
    sensor = lambda: run_sensors(["pytest", "-q", "test_demo.py"])  
    while True:
        goal = input("\n你> ").strip()
        if goal in ("exit", "quit"):
            break
        if not goal:
            continue
        old_len = len(history)
        reply, history = agent_loop(goal, llm, tools, SYSTEM_PROMPT, sensor, approver, history)
        for role, content in history[old_len:]:   # 只打本轮新增的
            print(f"  [{role}] {content}")
        print(f"Agent> {reply}")



