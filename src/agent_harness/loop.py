"""主循环：组织上下文 → 调模型 → 解析动作 → 分发执行 → 回灌结果 → 停机判断。"""

from typing import Callable
from .llm import LLM, Action
from .messages import Msg
from .sensors import run_sensors
from .guardrail import is_dangerous_command
from .schema import TOOL_SCHEMAS

MAX_STEPS = 20
Tool = Callable[..., str]

def agent_loop(
    goal: str,
    llm: LLM,
    tools: dict[str, Tool],
    system_prompt: str,
    approver: Callable[[str], bool] | None = None,
    history: list[Msg] | None = None,
    max_steps: int = MAX_STEPS,
    on_text: Callable[[str], None] | None = None,  # 流式文本增量回调（透传给模型适配器）
    on_tool: Callable[[str, dict], None] | None = None,  # 工具执行提示回调（名字 + 参数，界面用）
    on_reasoning: Callable[[str], None] | None = None,  # 思考增量回调（界面用）
) -> tuple[str, list[Msg]]:
    
    answer = None
    history = [Msg("user", goal)] if history is None else history + [Msg("user", goal)]

    for _ in range(max_steps):
        text, actions, reasoning = llm(
            history, TOOL_SCHEMAS, on_text=on_text, on_reasoning=on_reasoning
        )
        if not actions:
            answer = text
            history.append(Msg("assistant", text, reasoning_content=reasoning or None))
            break
        # 一条 assistant 带全部原始 tool_calls（API 要求 N 个调用 → N 条 tool 回应，严格配对）
        history.append(
            Msg("assistant", text, tool_calls=actions[0].tool_calls, reasoning_content=reasoning or None)
        )
        for action in actions:
            if on_tool:
                on_tool(action.tool, action.args)
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

            if action.tool_call_id:
                # 回灌 tool 消息：模型发出的原始 tool_calls → 解析成 Action → 执行 → 回灌结果
                history.append(Msg("tool", result, tool_name=action.tool, tool_call_id=action.tool_call_id))  # 只回灌一次
            else:
                history.append(Msg("user", result, tool_name=action.tool, tool_call_id=action.tool_call_id))  # 假模型

    if answer is None:
        answer = "已达到最大步数，停机。"

    return answer, history