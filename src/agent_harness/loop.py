"""主循环：组织上下文 → 调模型 → 解析动作 → 分发执行 → 回灌结果 → 停机判断。"""

from collections.abc import Sequence
from typing import Callable
from .llm import LLM, Action
from .messages import Msg
from .guardrail import classify_command
from .schema import TOOL_SCHEMAS
from .tools import cap_result, take_diff
from .trace import Tracer
from .hooks import run_post, run_pre

MAX_STEPS = 40
Tool = Callable[..., str]

def agent_loop(
    goal: str,
    llm: LLM,
    tools: dict[str, Tool],
    system_prompt: str,
    approver: Callable[[str, str], bool] | None = None,  # (命令, 为什么要问) → 放行吗
    history: list[Msg] | None = None,
    max_steps: int = MAX_STEPS,
    on_text: Callable[[str], None] | None = None,  # 流式文本增量回调（透传给模型适配器）
    on_tool: Callable[[str, dict], None] | None = None,  # 工具执行提示回调（名字 + 参数，界面用）
    on_tool_result: Callable[[dict], None] | None = None,  # 工具结束回调（状态 / 耗时 / 结果 / diff）
    on_reasoning: Callable[[str], None] | None = None,  # 思考增量回调（界面用）
    tracer: Tracer | None = None,  # 轨迹记录器（S8）——None = 关闭态，什么都不记
    pre_hooks: list | None = None,  # 预钩子名单（S9）——None = 空名单
    post_hooks: list | None = None,  # 后钩子名单（S9·观察类）
    on_hook: Callable[[str, str], None] | None = None,  # 策略拦截回调（by, reason）——界面实时流用
    should_stop: Callable[[], bool] | None = None,  # 每步开始问一次「还继续吗」——网页的停止按钮
    allow_commands: Sequence[str] = (),  # 配置里写死的允许规则（前缀语义，见 guardrail）
) -> tuple[str, list[Msg]]:
    if tracer is None:
        tracer = Tracer()  # 关闭态空对象：不传 tracer 的调用方，代码一行不改
    pre_hooks = pre_hooks or []
    post_hooks = post_hooks or []

    answer = None
    history = [Msg("user", goal)] if history is None else history + [Msg("user", goal)]

    # 工具菜单 = 工具注册表（S10）：注册表里没有的工具，模型菜单里也不出现——
    # 受限工具集、以及「子 agent 到顶后不许再派」都靠这一条收窄。
    tool_schemas = [s for s in TOOL_SCHEMAS if s["function"]["name"] in tools]

    for step in range(1, max_steps + 1):
        if should_stop is not None and should_stop():
            # 停止的最小语义：不再发起新动作。正在跑的那一步（工具 / 模型请求）跑完才停——
            # 硬中断一个子进程比「多做一步」更麻烦，个人项目不做。
            answer = "已按你的要求中止本轮（后面的步骤没有执行）。"
            break
        with tracer.timed("model", step=step) as ev:
            text, actions, reasoning = llm(
                history, tool_schemas, on_text=on_text, on_reasoning=on_reasoning,
                system_prompt=system_prompt,  # 每次调用带自己的提示词（子 agent 用它）
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
                # S11：黑名单 → 风险分级——只读放行；改动 / 执行 / 未知问人；极危险直接拒
                may_run = True
                if action.tool == "run_shell":
                    risk = classify_command(action.args.get("command", ""), allow_commands)
                    ev.fields["risk"] = risk.risk
                    if risk.risk == "catastrophic":
                        result = f"命令被策略直接拒绝（{risk.reason}），未执行。"
                        ev.fields["status"] = "blocked"
                        may_run = False
                    elif risk.risk != "read_only":
                        if approver is None:
                            result = f"无批准者，命令被策略拦截：{risk.reason}，不执行。"
                            ev.fields["status"] = "blocked"
                            may_run = False
                        else:
                            with tracer.timed(
                                "approval", step=step,
                                command=action.args["command"], risk=risk.risk,
                            ) as aev:
                                # 把「为什么问」一起递出去：只给命令的话，
                                # `find /c /v "" README.md` 看着像只读统计，批的人没得判
                                approved = approver(action.args["command"], risk.reason)
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
                        if on_hook:
                            on_hook(pre.by, pre.reason)  # 实时流冒一行（只在拦下时——放行不冒，防噪音）

                # 第三道：真执行
                if may_run and action.args_error:
                    # 模型给的 arguments 不是合法 JSON——不执行，把解析结果回灌让它重发
                    result = f"工具参数解析失败（{action.args_error}），未执行。请重新调用并给出合法 JSON。"
                    ev.fields["status"] = "error"
                    may_run = False
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
                ev.fields["result"] = result  # 全文（S8 决策 3）——trace 是审计账，不截断

            # 工具结束：状态 / 耗时 / 结果 / 改动一起递出去（界面靠它把「它改了什么」显示出来）。
            # take_diff() 无条件清一次——不清的话，下一次调用的 diff 会串到这一条上。
            diff = take_diff()
            if on_tool_result:
                on_tool_result({
                    "name": action.tool,
                    "status": str(ev.fields.get("status", "")),
                    "ms": ev.fields.get("duration_ms", 0),
                    "result": result,
                    "diff": diff,
                })

            # 回灌历史的那一份要截断：history 会进下一次请求、也会落进 session 存档，
            # 一次 `find /c` 就能灌进 90MB（2026-10-19 真机撞过）——trace 那份保持完整。
            fed = cap_result(result)
            if action.tool_call_id:
                # 回灌 tool 消息：模型发出的原始 tool_calls → 解析成 Action → 执行 → 回灌结果
                history.append(Msg("tool", fed, tool_name=action.tool, tool_call_id=action.tool_call_id))  # 只回灌一次
            else:
                history.append(Msg("user", fed, tool_name=action.tool, tool_call_id=action.tool_call_id))  # 假模型

    if answer is None:
        answer = f"已达到最大步数（{max_steps}），本轮停机。想接着做就再发一句话。"

    return answer, history