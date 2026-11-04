"""一轮的编排：装配 → 调 agent_loop → 交回结果。

为什么单独一层：TUI 和 Web 原来各写一遍「挂 notice → build_session_tools → 调
agent_loop」。这段**顺序**写错了不会报错——比如先建工具表再挂 notice，压缩提示就只
进 trace 不进界面——而两边已经开始漂（网页有 on_tool_result / should_stop，终端没有）。
这里统一顺序，**不统一界面**：回调长什么样、审批怎么等、停止信号从哪来，仍由入口决定。

只做"一轮"。会话生命周期（tracer 何时建、session_id 怎么换、历史落不落盘）留在调用方：
- tracer 由调用方建——TUI 一个会话一个，Web 每次 resume 换一个；
- 不落盘——失败时 history 未被改写，"这一轮不记"是刻意的语义，藏进来就看不见了；
- 异常原样抛——入口要区分「用户中断」和「跑失败了」，吞掉就丢了这个信息。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .assembly import POST_HOOKS, PRE_HOOKS, build_session_tools, build_system_prompt
from .loop import agent_loop
from .messages import Msg
from .trace import Tracer


@dataclass
class TurnCallbacks:
    """界面回调。全可选——不传就是这一侧不需要这类事件。"""

    on_text: Callable[[str], None] | None = None          # 正文增量
    on_reasoning: Callable[[str], None] | None = None     # 思考增量
    on_tool: Callable[[str, dict], None] | None = None    # 工具开始
    on_tool_result: Callable[[dict], None] | None = None  # 工具结束（状态 / 耗时 / 结果 / diff）
    on_hook: Callable[[str, str], None] | None = None     # 策略拦下（by, reason）
    on_notice: Callable[[str], None] | None = None        # 压缩这类"背后发生了事"的一句话


@dataclass
class TurnResult:
    """一轮跑完的产物。

    system_prompt 一并带回来，是为了让调用方算上下文用量时**不用再读一次文件**——
    TUI 的探针要用它做分母。谁读提示词只能有一处，这里就是那一处。
    """

    reply: str
    history: list[Msg]
    system_prompt: str


def describe_failure(exc: BaseException) -> str:
    """一轮失败说成一句人话。两个入口共用——各写一遍的话，文案会慢慢跑偏。"""
    return f"本轮失败：{type(exc).__name__}: {exc}"


class Session:
    """一次会话的装配：模型、审批、轨迹、工作区、历史。入口建一次，每轮调 `run()`。

    为什么值得一个对象：这几样东西的生命周期都比"一轮"长，散在参数表里时很容易被建到
    错误的层级上——网页端的 approver 就一度被建成了每轮一个，「本会话不再问」于是每轮
    清零，而功能测试全绿（单轮里看不出差别）。放进对象后，"建在连接上"是唯一顺手的写法。

    和 `agent_harness.session`（会话存档）不是一回事：那个管 JSONL 文件，这个管跑一轮
    需要的东西。
    """

    def __init__(self, *, llm, settings, approver, workspace, tracer=None, history=None):
        self.llm = llm
        self.settings = settings
        self.approver = approver
        self.workspace = workspace
        self.tracer = tracer if tracer is not None else Tracer()
        # 不复制：入口手里那份列表和这里必须是同一个对象，否则「原地更新」看不到
        self.history: list[Msg] = history if history is not None else []

    def run(self, goal, *, callbacks=None, should_stop=None, tracer=None) -> TurnResult:
        """跑一轮；历史原地更新（外面对同一个列表的引用会同步看到）。

        tracer 可按轮覆盖：网页端 resume 换了 session_id，轨迹要跟到新文件；
        终端是一个会话一个 tracer，不用传。
        """
        result = run_turn(
            goal,
            llm=self.llm,
            settings=self.settings,
            history=self.history,
            tracer=tracer if tracer is not None else self.tracer,
            approver=self.approver,
            workspace=self.workspace,
            callbacks=callbacks,
            should_stop=should_stop,
        )
        self.history[:] = result.history  # 失败会抛出去，走不到这行——历史保持原样
        return result


def run_turn(
    goal: str,
    *,
    llm,
    settings,
    history: list[Msg],
    tracer: Tracer,
    approver,
    workspace: Path,
    callbacks: TurnCallbacks | None = None,
    system_prompt: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> TurnResult:
    """跑一轮，返回这一轮的产物。落盘与错误展示交给调用方。

    system_prompt=None：现取（含 AGENTS.md 的最新内容）。缓存它等于凭空多一条
    「改了规则要重启」的隐性规矩——提示词本来就是当轮请求的一部分。
    """
    cb = callbacks or TurnCallbacks()
    sp = build_system_prompt() if system_prompt is None else system_prompt

    # 压缩这类提示：给人看一眼，同时记账。挂在这里 = 两个入口都挂上，谁也漏不了。
    def notice(text: str, **fields) -> None:
        if cb.on_notice is not None:
            cb.on_notice(text)
        tracer.emit("compact", text=text, **fields)

    llm.set_notice(notice)

    session_tools = build_session_tools(
        llm, sp,
        approver=approver,
        tracer=tracer,
        max_steps=settings.max_steps,
        on_hook=cb.on_hook,
        should_stop=should_stop,
        allow_commands=settings.guardrail_allow,
        workspace=workspace,
    )

    reply, new_history = agent_loop(
        goal, llm, session_tools, sp,
        approver=approver,
        history=history,
        on_text=cb.on_text,
        on_tool=cb.on_tool,
        on_tool_result=cb.on_tool_result,
        on_reasoning=cb.on_reasoning,
        tracer=tracer,
        pre_hooks=PRE_HOOKS,
        post_hooks=POST_HOOKS,
        on_hook=cb.on_hook,
        max_steps=settings.max_steps,
        should_stop=should_stop,
        allow_commands=settings.guardrail_allow,
        workspace=workspace,
    )
    return TurnResult(reply=reply, history=new_history, system_prompt=sp)