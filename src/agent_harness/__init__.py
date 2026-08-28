"""Agent Harness — 用代码造一台会自己干活的 agent 机器。"""

from .loop import agent_loop
from .llm import Action, LLM
from .tools import read_file, write_file

SYSTEM_PROMPT = (
    "你是 agent-harness，一台会自己干活的 coding agent。\n"
    "你可以使用工具读写文件。任务完成时发出 done。"
)


def main() -> None:
    tools = {"read_file": read_file, "write_file": write_file}
    script = [Action(type="call_tool", tool="read_file", args={"path": "README.md"}), Action(type="call_tool", tool="write_file", args={"path": "README.md", "content": "这是一个示例文件。"})]   # 演示剧本：读 README.md → 写 README.md
    llm = LLM(script)

    print("Agent Harness · S1 假模型驱动（固定剧本演示）")
    print("输入任务回车执行；exit 退出。")
    history: list[tuple[str, str]] = []
    while True:
        goal = input("\n你> ").strip()
        if goal in ("exit", "quit"):
            break
        if not goal:
            continue
        old_len = len(history)
        reply, history = agent_loop(goal, llm, tools, SYSTEM_PROMPT, history)
        for role, content in history[old_len:]:   # 只打本轮新增的
            print(f"  [{role}] {content}")
        print(f"Agent> {reply}")



