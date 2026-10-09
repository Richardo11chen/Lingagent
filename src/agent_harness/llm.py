"""模型抽象层：主循环只认这条接口，不认背后是谁。

演进（S5）：history 条目从二元组升级为 Msg 结构（要装 tool_call_id / tool_calls——
API 状态机强制 tool 消息按 id 关联）；接口升级为传结构（messages + tools schema），
build_context 拼 str 退役给展示层。
"""

from dataclasses import dataclass, field
from typing import Protocol
import json
import os
import time

from openai import APIStatusError, OpenAI

from .messages import Msg  # 重新导出——兼容从 llm 导入 Msg 的既有代码
from .compact import microcompact, microcompact_if_needed, snip


@dataclass
class Action:
    """模型发出的结构化指令。"""

    tool: str = ""  # call_tool 时用：工具名
    args: dict = field(default_factory=dict)  # call_tool 时用：参数
    tool_call_id: str | None = None  # 真模型路径：API 的 tool_call id（回灌 tool 消息时用）
    tool_calls: list | None = None  # 真模型路径：API 原始 tool_calls（assistant 消息重发用）


class LLM(Protocol):
    """模型接口：输入历史结构 + 工具 schema，输出 (text, actions)。

    on_text：文本增量回调（流式显示用，可为 None）。
    """

    def __call__(
        self, messages: list[Msg], tools: list[dict], on_text=None
    ) -> tuple[str, list[Action]]: ...


class ScriptLLM:
    """假模型（测试标准件）：按预设剧本演，完全不看输入。

    三态决策仍可用（看最后一条消息）——但简化版：剧本弹完直接 done。
    """

    def __init__(self, script: list[Action], fix_script: list[Action] | None = None):
        self._script = list(script)
        self._fix_script = list(fix_script or [])

    def __call__(
        self, messages: list[Msg], tools: list[dict], on_text=None
    ) -> tuple[str, list[Action]]:
        last = messages[-1].content if messages else ""
        if "测试失败" in last:
            if self._fix_script:
                return ("（假模型）测试失败，继续修改。", [self._fix_script.pop(0)])
            return ("（假模型）多次修复未果，停止。", [])
        if "测试全部通过" in last:
            return ("测试通过，任务完成。", [])
        if self._script:
            action = self._script.pop(0)
            return (f"（假模型）下一步：{action.tool}", [action])
        return ("任务完成。", [])


def to_api_messages(system_prompt: str, messages: list[Msg]) -> list[dict]:
    """组装 API 请求消息（纯函数，可脱离 API 单测）。

    三分支映射：assistant 带 tool_calls 原样重发；tool 带 tool_call_id 回送；其余直转。
    """
    api_messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            # assistant 消息带原始 tool_calls——API 要求原样重发
            api_messages.append({"role": "assistant", "content": m.content, "tool_calls": m.tool_calls})
        elif m.role == "tool":
            api_messages.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        else:
            api_messages.append({"role": m.role, "content": m.content})
    return api_messages


class DeepSeekLLM:
    """真模型适配器：history → API messages → 解析 tool_calls 成 Action。

    停机语义：tool_calls is None = 模型给最终答案 = done（官方循环条件）。
    """

    def __init__(self, system_prompt: str, model: str = "deepseek-v4-flash"):
        self._client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com",
        )
        self._model = model
        self._system_prompt = system_prompt

    def _request(self, api_messages: list[dict], tools: list[dict], on_text):
        """流式发一次请求——返回 (text, raw_tool_calls)。

        on_text：每个文本增量回调（界面流式显示用；可为 None）。
        raw_tool_calls：按 index 拼装完整的 dict 列表（id/type/function——请求侧格式）。
        """
        stream = self._client.chat.completions.create(
            model=self._model, messages=api_messages, tools=tools or None, stream=True
        )
        text_parts: list[str] = []
        acc: dict[int, dict] = {}  # index → 拼装中的调用（分片按 index 归位）
        for chunk in stream:
            if not chunk.choices:  # 部分 chunk 无 choices（usage 等）
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                text_parts.append(delta.content)
                if on_text:
                    on_text(delta.content)
            if delta.tool_calls:
                for tcd in delta.tool_calls:
                    slot = acc.setdefault(
                        tcd.index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tcd.id:
                        slot["id"] = tcd.id
                    if tcd.function:
                        if tcd.function.name:
                            slot["function"]["name"] = tcd.function.name
                        if tcd.function.arguments:  # arguments 分片累积
                            slot["function"]["arguments"] += tcd.function.arguments
        raw = [acc[i] for i in sorted(acc)]  # 按 index 排序——与到达顺序无关
        return "".join(text_parts), raw

    def __call__(
        self, messages: list[Msg], tools: list[dict], on_text=None
    ) -> tuple[str, list[Action]]:
        # 在调用 API 前进行上下文压缩（投影——不改 history）
        messages = microcompact_if_needed(messages)
        api_messages = to_api_messages(self._system_prompt, messages)

        try:
            text, raw = self._request(api_messages, tools, on_text)
        except APIStatusError as e:
            if e.status_code != 413:  # 非超限（401/500 等）——重试无意义，直接抛
                raise
            # Reactive 兜底：退避（疑限流）→ 加压（全清旧结果）→ 重试一次；
            # 第二次不在 try 内——结构上不可能有第三次（无需 has_attempted 标志）
            time.sleep(3)
            # 两刀：丢中间（砍大头）+ 清存活段的胖结果
            pressed = microcompact(snip(messages), keep_last=0)
            text, raw = self._request(to_api_messages(self._system_prompt, pressed), tools, on_text)

        if not raw:  # 无工具调用 = 最终答案 = done（官方停机条件）
            return (text, [])

        actions: list[Action] = []
        for tc in raw:
            args = json.loads(tc["function"]["arguments"])
            actions.append(Action(
                tool=tc["function"]["name"],
                args=args,
                tool_call_id=tc["id"],
                tool_calls=raw,  # 原始结构——assistant 消息重发时原样带回
            ))

        return (text, actions)
