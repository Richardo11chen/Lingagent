"""主循环：组织上下文 → 调模型 → 解析动作 → 分发执行 → 回灌结果 → 停机判断。"""

from typing import Callable
from .llm import LLM, Action
from .sensors import run_sensors
from .guardrail import is_dangerous_command

MAX_STEPS = 20
Tool = Callable[..., str]


def build_context(system_prompt: str, goal: str, history: list[tuple[str, str]]) -> str:
    # 1. parts = [system_prompt, f"用户任务：{goal}"]
    # 2. 逐条把 history 转成 "[role] content" 追加
    # 3. "\n\n".join(parts)
    parts = [system_prompt]
    for role, content in history:
        parts.append(f"[{role}] {content}")
    parts.append(f"用户任务：{goal}")
    return "\n\n".join(parts)

def agent_loop(
    goal: str,
    llm: LLM,
    tools: dict[str, Tool],
    system_prompt: str,
    sensor: Callable[[], str] | None = None, 
    approver: Callable[[str], bool] | None = None,
    history: list[tuple[str, str]] | None = None,
    max_steps: int = MAX_STEPS,
) -> tuple[str, list[tuple[str, str]]]:
    # 0. history = list(history or [])  # 拷贝，别动调用者的
    # for _ in range(max_steps):
    #    1. context = build_context(...)
    #    2. text, action = llm(context, list(tools.keys()))
    #    3. history.append(("assistant", ...))  # 模型的话+动作
    #    4. if action.type == "done": return text, history
    #    5. if action.type == "call_tool": 查表 → 执行/兜底 → history.append(("user", result))
    # return "已达到最大步数，停机。", history
    answer = None
    history = list(history or [])

    for _ in range(max_steps):
        context = build_context(system_prompt, goal, history)
        text, action = llm(context, list(tools.keys()))
        history.append(("assistant", f"{text} {action.args}"))
        if action.type == "done":
            answer = text
            break
        elif action.type == "call_tool":
            tool_func = tools.get(action.tool)
            if action.tool == "run_shell" and is_dangerous_command(action.args.get("command", "")):
                # 治理护栏：危险命令，先问人——批准才真执行，其余路径都到此为止
                if approver is None:
                    result = "无批准者，命令被策略拦截：危险命令不执行。"
                elif approver(action.args["command"]):
                    try:
                        result = tool_func(**action.args)      # 批准 → 真执行
                    except Exception as e:
                        result = f"{action.tool}工具调用失败。"
                else:
                    result = "命令被驳回，未执行。"
            elif tool_func:
                try:
                    result = tool_func(**action.args)
                except Exception as e:
                    result = f"{action.tool}工具调用失败。"
            else:
                result = f"工具不存在：未知工具 {action.tool}。"
            history.append(("user", result))   # 只回灌一次
        else:
            result = "未知动作类型。"
            history.append(("user", result))

        if action.changed_code and sensor:
            report = sensor()
            history.append(("user", report))

    if answer is None:
        answer = "已达到最大步数，停机。"

    return answer, history