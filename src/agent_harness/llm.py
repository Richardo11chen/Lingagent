"""模型抽象层：主循环只认这条接口，不认背后是谁。

演进（S5）：history 条目从二元组升级为 Msg 结构（要装 tool_call_id / tool_calls——
API 状态机强制 tool 消息按 id 关联）；接口升级为传结构（messages + tools schema），
build_context 拼 str 退役给展示层。
"""

from dataclasses import dataclass, field
from typing import Protocol
import json
import os

from openai import OpenAI


@dataclass
class Msg:
    """一条历史消息。role: user / assistant / tool。tool 消息必须带 tool_call_id。"""

    role: str
    content: str = ""
    tool_call_id: str | None = None  # tool 消息：关联到那次模型调用
    tool_calls: list | None = None  # assistant 消息：模型发出的原始 tool_calls（重发时原样带回）


@dataclass
class Action:
    """模型发出的结构化指令。"""

    tool: str = ""  # call_tool 时用：工具名
    args: dict = field(default_factory=dict)  # call_tool 时用：参数
    tool_call_id: str | None = None  # 真模型路径：API 的 tool_call id（回灌 tool 消息时用）
    tool_calls: list | None = None  # 真模型路径：API 原始 tool_calls（assistant 消息重发用）


class LLM(Protocol):
    """模型接口：输入历史结构 + 工具 schema，输出 (text, action)。"""

    def __call__(self, messages: list[Msg], tools: list[dict]) -> tuple[str, list[Action]]: ...


class ScriptLLM:
    """假模型（测试标准件）：按预设剧本演，完全不看输入。

    三态决策仍可用（看最后一条消息）——但简化版：剧本弹完直接 done。
    """

    def __init__(self, script: list[Action], fix_script: list[Action] | None = None):
        self._script = list(script)
        self._fix_script = list(fix_script or [])

    def __call__(self, messages: list[Msg], tools: list[dict]) -> tuple[str, list[Action]]:
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

    def __call__(self, messages: list[Msg], tools: list[dict]) -> tuple[str, list[Action]]:
        api_messages = to_api_messages(self._system_prompt, messages)

        resp = self._client.chat.completions.create(
            model=self._model, messages=api_messages, tools=tools or None
        )
        msg = resp.choices[0].message

        if not msg.tool_calls:  # 无工具调用 = 最终答案 = done（官方停机条件）
            return (msg.content or "", [])

        actions: list[Action] = []
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            actions.append(Action(
                tool=tc.function.name,
                args=args,
                tool_call_id=tc.id,
                tool_calls=msg.tool_calls,  # 原始结构——assistant 消息重发时原样带回
            ))
            
        return (msg.content or "", actions)
