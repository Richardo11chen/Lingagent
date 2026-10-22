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

from openai import APIConnectionError, APIStatusError, OpenAI

from .messages import Msg  # 重新导出——兼容从 llm 导入 Msg 的既有代码
from .compact import microcompact, microcompact_if_needed, snip

REQUEST_TIMEOUT = 120  # 秒：一次流式请求的硬上限（SDK 默认 600 秒——卡住十分钟太久）


class LLMError(RuntimeError):
    """模型层可预期故障（连不上 / 超时）——上层捕获后给人一句干净的话，别甩 traceback。"""


@dataclass
class Action:
    """模型发出的结构化指令。"""

    tool: str = ""  # call_tool 时用：工具名
    args: dict = field(default_factory=dict)  # call_tool 时用：参数
    tool_call_id: str | None = None  # 真模型路径：API 的 tool_call id（回灌 tool 消息时用）
    tool_calls: list | None = None  # 真模型路径：API 原始 tool_calls（assistant 消息重发用）
    args_error: str | None = None  # arguments 不是合法 JSON 时的原因（loop 据此拒绝执行并回灌）


class LLM(Protocol):
    """模型接口：输入历史结构 + 工具 schema，输出 (text, actions)。

    on_text：文本增量回调（流式显示用，可为 None）。
    """

    def __call__(
        self, messages: list[Msg], tools: list[dict], on_text=None, on_reasoning=None,
        system_prompt: str | None = None,
    ) -> tuple[str, list[Action], str]: ...


class ScriptLLM:
    """假模型（测试标准件）：按预设剧本演，完全不看输入。

    三态决策仍可用（看最后一条消息）——但简化版：剧本弹完直接 done。
    """

    def __init__(self, script: list[Action], fix_script: list[Action] | None = None):
        self._script = list(script)
        self._fix_script = list(fix_script or [])

    def __call__(
        self, messages: list[Msg], tools: list[dict], on_text=None, on_reasoning=None,
        system_prompt: str | None = None,
    ) -> tuple[str, list[Action], str]:
        last = messages[-1].content if messages else ""
        if "测试失败" in last:
            if self._fix_script:
                return ("（假模型）测试失败，继续修改。", [self._fix_script.pop(0)], "")
            return ("（假模型）多次修复未果，停止。", [], "")
        if "测试全部通过" in last:
            return ("测试通过，任务完成。", [], "")
        if self._script:
            action = self._script.pop(0)
            return (f"（假模型）下一步：{action.tool}", [action], "")
        return ("任务完成。", [], "")


def to_api_messages(system_prompt: str, messages: list[Msg]) -> list[dict]:
    """组装 API 请求消息（纯函数，可脱离 API 单测）。

    三分支映射：assistant 带 tool_calls 原样重发；tool 带 tool_call_id 回送；其余直转。
    """
    api_messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for m in messages:
        if m.role == "assistant" and (m.tool_calls or m.reasoning_content):
            # assistant 消息原样重发：tool_calls + reasoning_content（协议要求回传）
            entry: dict = {"role": "assistant", "content": m.content}
            if m.tool_calls:
                entry["tool_calls"] = m.tool_calls
            if m.reasoning_content:
                entry["reasoning_content"] = m.reasoning_content
            api_messages.append(entry)
        elif m.role == "tool":
            api_messages.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        else:
            api_messages.append({"role": m.role, "content": m.content})
    return api_messages


class ThinkSplitter:
    """把正文里 <mm:think>…</mm:think> 包着的思考拆到思考通道（MiniMax 形态）。

    标签会被流分片切断（"<mm:t" + "hink>"），所以尾部可能残留「半个标签」——扣住等下一片。
    对不吐这形态的厂商（DeepSeek）零影响：拆分器全程一字不扣地放行。
    """

    OPEN = "<mm:think>"
    CLOSE = "</mm:think>"

    def __init__(self):
        self._buf = ""
        self._in_think = False

    def feed(self, chunk: str) -> list[tuple[str, bool]]:
        """喂增量，吐出 [(文本, 是思考吗), ...]——只吐已确定的整块。

        两个标签都当结构件：**孤立的闭标签直接吞掉**（有的网关自己抽了思考、
        只把 `</mm:think>` 残渣落在正文里——minimax 实测）；重复的开标签同理。
        """
        self._buf += chunk
        out: list[tuple[str, bool]] = []
        while True:
            oi = self._buf.find(self.OPEN)
            ci = self._buf.find(self.CLOSE)
            if oi < 0 and ci < 0:
                keep = max(  # 尾巴里有没有「半个标签」——两个标签都要防（分片会切断）
                    (k for tag in (self.OPEN, self.CLOSE) for k in range(1, len(tag))
                     if self._buf.endswith(tag[:k])),
                    default=0,
                )
                emit, self._buf = (self._buf[:-keep], self._buf[-keep:]) if keep else (self._buf, "")
                if emit:
                    out.append((emit, self._in_think))
                return out
            if ci < 0 or (0 <= oi < ci):  # 先遇到开标签
                pre, self._buf, entering = self._buf[:oi], self._buf[oi + len(self.OPEN):], True
            else:                          # 先遇到闭标签
                pre, self._buf, entering = self._buf[:ci], self._buf[ci + len(self.CLOSE):], False
            if pre:
                out.append((pre, self._in_think))
            if entering and not self._in_think:
                self._in_think = True      # 开思考
            elif not entering and self._in_think:
                self._in_think = False     # 收思考
            # 其余情形（重复开 / 孤立闭）＝ 结构残渣，直接吞

    def flush(self) -> list[tuple[str, bool]]:
        """流结束：残留缓冲按当前状态派出（没闭合的标签当思考——模型忘了收尾也不丢字）。"""
        out = []
        if self._buf:
            out.append((self._buf, self._in_think))
            self._buf = ""
        return out


class DeepSeekLLM:
    """真模型适配器：history → API messages → 解析 tool_calls 成 Action。

    停机语义：tool_calls is None = 模型给最终答案 = done（官方循环条件）。
    """

    def __init__(
        self,
        system_prompt: str,
        model: str = "deepseek-v4-flash",
        base_url: str = "https://api.deepseek.com",
        api_key_env: str = "DEEPSEEK_API_KEY",
        api_key: str = "",
        temperature: float = 1.0,
        compact_threshold: int = 70_000,
        keep_last: int = 5,
        snip_keep_groups: int = 3,
        thinking: str = "off",
        reasoning_effort: str = "off",
    ):
        key = api_key or os.environ.get(api_key_env, "")  # 直写优先；不写回退环境变量
        if not key:
            raise SystemExit(f"缺少 API key：profile 没写 api_key，环境变量 {api_key_env} 也没设置")
        self._client = OpenAI(api_key=key, base_url=base_url, timeout=REQUEST_TIMEOUT)
        self._model = model
        self._temperature = temperature
        self._compact_threshold = compact_threshold
        self._keep_last = keep_last
        self._snip_keep_groups = snip_keep_groups
        self._thinking = thinking
        self._reasoning_effort = reasoning_effort
        self._system_prompt = system_prompt

    def _request(self, api_messages: list[dict], tools: list[dict], on_text, on_reasoning=None):
        """流式发一次请求——返回 (text, raw_tool_calls, reasoning)。

        on_text / on_reasoning：文本与思考增量回调（界面显示用；可为 None）。
        raw_tool_calls：按 index 拼装完整的 dict 列表（id/type/function——请求侧格式）。
        """
        # thinking / reasoning_effort 是 DeepSeek 私有参数：值为 "off" 时**不传**
        # （跨厂商安全——别的 OpenAI 兼容端点收到未知参数可能 400）
        kwargs: dict = {
            "model": self._model,
            "messages": api_messages,
            "tools": tools or None,
            "temperature": self._temperature,
            "stream": True,
        }
        if self._thinking != "off":
            kwargs["extra_body"] = {"thinking": {"type": self._thinking}}
        if self._reasoning_effort != "off":
            kwargs["reasoning_effort"] = self._reasoning_effort
        stream = self._client.chat.completions.create(**kwargs)
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        splitter = ThinkSplitter()  # 正文里的 <mm:think> 剥去思考通道（MiniMax 形态）

        def _emit(piece: str, is_think: bool) -> None:
            if is_think:
                reasoning_parts.append(piece)
                if on_reasoning:
                    on_reasoning(piece)
            else:
                text_parts.append(piece)
                if on_text:
                    on_text(piece)

        acc: dict[int, dict] = {}  # index → 拼装中的调用（分片按 index 归位）
        for chunk in stream:
            if not chunk.choices:  # 部分 chunk 无 choices（usage 等）
                continue
            delta = chunk.choices[0].delta
            if delta is None:  # 有的网关收尾 chunk 给 delta=null（minimax-m3 实测）——跳过
                continue
            rc = getattr(delta, "reasoning_content", None)  # 思考增量（SDK 类型外字段）
            if rc:
                reasoning_parts.append(rc)
                if on_reasoning:
                    on_reasoning(rc)
            if delta.content:
                for piece, is_think in splitter.feed(delta.content):
                    _emit(piece, is_think)
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
        for piece, is_think in splitter.flush():  # 收尾：派发残留缓冲
            _emit(piece, is_think)
        raw = [acc[i] for i in sorted(acc)]  # 按 index 排序——与到达顺序无关
        return "".join(text_parts), raw, "".join(reasoning_parts)

    def __call__(
        self, messages: list[Msg], tools: list[dict], on_text=None, on_reasoning=None,
        system_prompt: str | None = None,
    ) -> tuple[str, list[Action], str]:
        # 在调用 API 前进行上下文压缩（投影——不改 history）
        messages = microcompact_if_needed(messages, self._compact_threshold, keep_last=self._keep_last)
        sp = system_prompt or self._system_prompt  # 调用方给了就用它（子 agent 带自己的提示词）
        api_messages = to_api_messages(sp, messages)

        try:
            text, raw, reasoning = self._request(api_messages, tools, on_text, on_reasoning)
        except APIConnectionError as e:  # 断网 / 超时（APITimeoutError 是它的子类）
            raise LLMError(f"模型服务连不上（{type(e).__name__}）：{e}") from e
        except APIStatusError as e:
            if e.status_code != 413:  # 非超限（401/500 等）——重试无意义，直接抛
                raise
            # Reactive 兜底：退避（疑限流）→ 加压（全清旧结果）→ 重试一次；
            # 第二次不在 try 内——结构上不可能有第三次（无需 has_attempted 标志）
            time.sleep(3)
            # 两刀：丢中间（砍大头）+ 清存活段的胖结果
            pressed = microcompact(snip(messages, self._snip_keep_groups), keep_last=0)
            text, raw, reasoning = self._request(
                to_api_messages(sp, pressed), tools, on_text, on_reasoning
            )

        if not raw:  # 无工具调用 = 最终答案 = done（官方停机条件）
            return (text, [], reasoning)

        actions: list[Action] = []
        for tc in raw:
            args, args_error = _parse_args(tc["function"].get("arguments"))
            actions.append(Action(
                tool=tc["function"]["name"],
                args=args,
                tool_call_id=tc["id"],
                tool_calls=raw,  # 原始结构——assistant 消息重发时原样带回
                args_error=args_error,
            ))

        return (text, actions, reasoning)


def _parse_args(raw_args) -> tuple[dict, str | None]:
    """把 API 的 arguments 解析成 dict——失败不抛（一次坏 JSON 不该打死整轮对话）。

    返回 (参数, 错误原因)。错误原因非 None 时 loop 会拒绝执行并把原因回灌给模型，
    让它在下一轮重发合法 JSON——比让异常穿透到 REPL 强得多。
    """
    if isinstance(raw_args, dict):  # 个别网关直接给对象
        return raw_args, None
    text = raw_args or ""
    if not text.strip():  # 无参工具常见形态：空串 = 没有参数
        return {}, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return {}, f"JSON 不合法（{e.msg}，第 {e.pos} 字符）"
    if not isinstance(parsed, dict):
        return {}, f"参数应为 JSON 对象，实际是 {type(parsed).__name__}"
    return parsed, None
