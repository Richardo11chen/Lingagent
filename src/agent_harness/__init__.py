"""Agent Harness — 用代码造一台会自己干活的 agent 机器。"""

from .sensors import run_sensors
from .loop import agent_loop
from .llm import Action, LLM
from .tools import read_file, write_file, count_lines, run_shell

SYSTEM_PROMPT = (
    "你是 agent-harness，一台会自己干活的 coding agent。\n"
    "你可以使用工具读写文件。任务完成时发出 done。"
)


def main() -> None:
    tools = {"read_file": read_file, "write_file": write_file, "count_lines": count_lines, "run_shell": run_shell}
    # S2 演示剧本：改 demo.py → 故意改错（红）→ 修复（绿）→ done
    script = [
        Action("call_tool", "read_file", {"path": "demo.py"}),
        Action("call_tool", "write_file", {"path": "demo.py", "content": "def add(a, b):\n    return a * b  # 故意改错，触发红"}, changed_code=True),
    ]
    fix_script = [
        Action("call_tool", "write_file", {"path": "demo.py", "content": "def add(a, b):\n    return a + b  # 修对了"}, changed_code=True),
    ]
    llm = LLM(script, fix_script)

    print("Agent Harness · S2 假模型驱动（反馈闭环演示：红了改、绿了停）")
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
        reply, history = agent_loop(goal, llm, tools, SYSTEM_PROMPT, sensor, history)
        for role, content in history[old_len:]:   # 只打本轮新增的
            print(f"  [{role}] {content}")
        print(f"Agent> {reply}")



