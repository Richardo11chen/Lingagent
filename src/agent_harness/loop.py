"""主循环：组织上下文 → 调模型 → 解析动作 → 分发执行 → 回灌结果 → 停机判断。"""

from typing import Callable
from .llm import LLM, Action
from .messages import Msg
from .sensors import run_sensors
from .guardrail import is_dangerous_command
from .schema import TOOL_SCHEMAS
from .trace import Tracer
from .hooks import run_post, run_pre

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
    tracer: Tracer | None = None,  # 轨迹记录器（S8）——None = 关闭态，什么都不记
    pre_hooks: list | None = None,  # 预钩子名单（S9）——None = 空名单
    post_hooks: list | None = None,  # 后钩子名单（S9·观察类）
) -> tuple[str, list[Msg]]:
    if tracer is None:
        tracer = Tracer()  # 关闭态空对象：不传 tracer 的调用方，代码一行不改
    pre_hooks = pre_hooks or []
    post_hooks = post_hooks or []

    answer = None
    history = [Msg("user", goal)] if history is None else history + [Msg("user", goal)]

    for step in range(1, max_steps + 1):
        with tracer.timed("model", step=step) as ev:
            text, actions, reasoning = llm(
                history, TOOL_SCHEMAS, on_text=on_text, on_reasoning=on_reasoning
            )
            ev.fields["n_actions"] = len(actions)
            model_name = getattr(llm, "current", "")  # 实际在用的模型名（SwitchableLLM 有、假模型没有）
            if model_name:
                ev.fields["model"] = model_name
        if not actions:
            answer = text
            history.append(Msg("assistant", text, reasoning_content=reasoning or None))
            break
        # 一条 assistant 带全部原始 tool_calls（API 要求 N 个调用 → N 条 tool 回应，严格配对）
        history.append(
            Msg("assistant", text, tool_calls=actions[0].tool_calls, reasoning_content=reasoning or None)
        )
        for action in actions:
            with tracer.timed("tool", step=step, tool=action.tool, args=action.args) as ev:
                if on_tool:
                    on_tool(action.tool, action.args)
                tool_func = tools.get(action.tool)

                # 第一道：内置护栏（基线不可摘——先于 hooks 名单跑，S9 决策 3）
                may_run = True
                if action.tool == "run_shell" and is_dangerous_command(action.args.get("command", "")):
                    # 治理护栏：危险命令，先问人——批准才真执行
                    if approver is None:
                        result = "无批准者，命令被策略拦截：危险命令不执行。"
                        ev.fields["status"] = "blocked"
                        may_run = False
                    else:
                        with tracer.timed("approval", step=step, command=action.args["command"]) as aev:
                            approved = approver(action.args["command"])
                            aev.fields["decision"] = "approved" if approved else "denied"
                        if not approved:
                            result = "命令被驳回，未执行。"
                            ev.fields["status"] = "denied"
                            may_run = False

                # 第二道：预钩子名单（S9）——guardrail 之后、执行之前；首个 deny 短路
                if may_run:
                    pre = run_pre(pre_hooks, action)
                    if pre_hooks:  # 注册了预钩子才留账（没注册时零噪音）
                        tracer.emit(
                            "hook", step=step, tool=action.tool,
                            verdict="deny" if pre.denied else "allow",
                            by=pre.by, reason=pre.reason[:80],
                        )
                    if pre.denied:
                        result = (f"动作被策略拦截（{pre.by}）：{pre.reason}" if pre.reason
                                  else f"动作被策略拦截（{pre.by}）。")
                        ev.fields["status"] = "denied"
                        ev.fields["hook"] = pre.by
                        may_run = False

                # 第三道：真执行
                if may_run:
                    if tool_func:
                        try:
                            result = tool_func(**action.args)
                            ev.fields["status"] = "ok"
                        except Exception as e:
                            result = f"{action.tool}工具调用失败。"
                            ev.fields["status"] = "error"
                            ev.fields["error"] = f"{type(e).__name__}: {e}"
                        else:
                            run_post(post_hooks, action, result)  # 后钩子：只观察（仅在真执行后）
                    else:
                        result = f"工具不存在：未知工具 {action.tool}。"
                        ev.fields["status"] = "error"
                ev.fields["result"] = result  # 全文（S8 决策 3）

            if action.tool_call_id:
                # 回灌 tool 消息：模型发出的原始 tool_calls → 解析成 Action → 执行 → 回灌结果
                history.append(Msg("tool", result, tool_name=action.tool, tool_call_id=action.tool_call_id))  # 只回灌一次
            else:
                history.append(Msg("user", result, tool_name=action.tool, tool_call_id=action.tool_call_id))  # 假模型

    if answer is None:
        answer = "已达到最大步数，停机。"

    return answer, history