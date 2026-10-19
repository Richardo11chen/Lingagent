"""子 agent（S10·分身）：把一块子任务派给独立上下文的自己，只拿摘要回来。

核心 = 上下文隔离（Isolate）：子任务跑在自己的 history 里，中间的工具往返不进父上下文；
父只收到一条「[子 agent 报告]」。

实现：同一个 agent_loop 的递归调用——不是新引擎。装配层用 make_spawn_tool 造工具，
工具闭包持有该会话的整套装配（模型 / 提示 / 审批 / hooks / trace）。
"""

from .loop import MAX_STEPS, agent_loop

MAX_DEPTH = 1  # 深度上限（用户拍板）：父（0）可派一个子（1）；子不能再派孙

SUBAGENT_PROMPT_SUFFIX = (
    "\n\n（你是被父 agent 派出的子 agent：只做被派的子任务，完成后直接给结论；"
    "你看不到父对话，结论要自包含。\n"
    "**默认只读**：父任务没明确要求修改文件时，只做读 / 查 / 跑验证——不要用写工具或 shell "
    "改动工作区（重定向 > / >>、copy / move / del / rm、git 写操作、安装依赖等）。"
    "父任务明确要求写入时，才动手改，并在结论里列明改了哪些文件。）"
)


class _ChildTracer:
    """给子 agent 的 trace 适配器：每条事件多一个 sub 字段（父子分层）。"""

    def __init__(self, inner, depth: int):
        self._inner = inner
        self._depth = depth

    def emit(self, kind: str, **fields) -> None:
        fields["sub"] = self._depth
        self._inner.emit(kind, **fields)

    def timed(self, kind: str, **fields):
        fields["sub"] = self._depth
        return self._inner.timed(kind, **fields)


def make_spawn_tool(
    *,
    llm,
    tools: dict,
    system_prompt: str,
    approver=None,
    tracer=None,
    pre_hooks=None,
    post_hooks=None,
    depth: int = 0,
    max_depth: int = MAX_DEPTH,
    max_steps: int | None = None,
    on_hook=None,
):
    """构造 spawn_subagent 工具（装配层工厂）。

    depth：**使用这个工具的** agent 所在层级（父 = 0）；子 agent 跑在 depth + 1。
    tools：基础工具表（不含 spawn）——到顶时子 agent 的工具表里不会再挂 spawn。
    子 agent 拿**全量基础工具**；只读是系统提示词的默认约束（用户拍板，2026-10-19）。
    """

    def spawn_subagent(subtask: str) -> str:
        child_depth = depth + 1
        child_tools = dict(tools)
        if child_depth < max_depth:  # 还有余量才给子 agent 发「派孙」的插座
            child_tools["spawn_subagent"] = make_spawn_tool(
                llm=llm,
                tools=tools,
                system_prompt=system_prompt,
                approver=approver,
                tracer=tracer,
                pre_hooks=pre_hooks,
                post_hooks=post_hooks,
                depth=child_depth,
                max_depth=max_depth,
                max_steps=max_steps,
                on_hook=on_hook,
            )
        child_tracer = _ChildTracer(tracer, child_depth) if tracer is not None else None
        answer, _child_history = agent_loop(  # 子 agent 的 history 用完即弃（上下文隔离）
            subtask,
            llm,
            child_tools,
            system_prompt + SUBAGENT_PROMPT_SUFFIX,
            approver=approver,
            history=None,
            tracer=child_tracer,
            pre_hooks=pre_hooks,
            post_hooks=post_hooks,
            on_hook=on_hook,
            max_steps=max_steps or MAX_STEPS,
        )
        # 固定模板摘要（用户拍板）：父只看到目标与结论
        return f"[子 agent 报告]\n目标：{subtask}\n结论：{answer}"

    return spawn_subagent
