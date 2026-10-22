"""S4b 单测：上下文压缩（投影微压缩 / 白名单 / 组边界 / 阈值 / reactive 兜底）。

规格 (C)：压缩是纯机制——脱离真实 LLM 可确定性验证（reactive 用 mock client）。
"""

import httpx
import pytest
from openai import APIStatusError

from agent_harness.compact import (
    estimate_tokens,
    microcompact,
    microcompact_if_needed,
    snip,
)
from agent_harness.messages import Msg


def _tool_msg(content: str, tool_name: str, i: int = 0) -> Msg:
    """构造一条 tool 结果消息（带配对 id）。"""
    return Msg("tool", content, tool_call_id=f"c{i}", tool_name=tool_name)


def _long_history(n: int = 7, tool: str = "read_file") -> list[Msg]:
    """构造：user + n 组(assistant[tool_calls] + tool 结果) + 收尾 user/assistant。"""
    msgs = [Msg("user", "任务")]
    for i in range(n):
        msgs.append(
            Msg(
                "assistant",
                "",
                tool_calls=[
                    {"id": f"c{i}", "type": "function", "function": {"name": tool, "arguments": "{}"}}
                ],
            )
        )
        msgs.append(_tool_msg("X" * 1000, tool, i))
    msgs.append(Msg("user", "收尾问题"))
    msgs.append(Msg("assistant", "收尾回答"))
    return msgs


def test_estimate_counts_content_and_tool_calls():  # 估算 = content + tool_calls 结构量
    msgs = [Msg("user", "12345"), Msg("assistant", "", tool_calls=[{"id": "c"}] * 10)]
    assert estimate_tokens(msgs) >= 5 + len(str([{"id": "c"}] * 10))


def test_microcompact_keeps_recent_and_clears_old():  # 保留最近 5 条白名单结果，更早的清
    msgs = _long_history(7)
    out = microcompact(msgs, keep_last=5)
    cleared = [m for m in out if "已清理" in m.content]
    assert len(cleared) == 2  # 7 - 5
    assert "[已清理：read_file 的旧输出，1000 字符]" == cleared[0].content


def test_microcompact_is_projection():  # 投影：原列表与原对象零污染（replace 产新对象）
    msgs = _long_history(7)
    before = msgs[2].content  # 第一条 tool 结果
    out = microcompact(msgs, keep_last=5)
    assert msgs[2].content == before          # 原对象没动
    assert out[2] is not msgs[2]              # 被清的是新对象
    assert out[0] is msgs[0]                  # 未清的复用引用（零拷贝）


def test_microcompact_skips_non_whitelist():  # 白名单外（write_file）结果不清
    msgs = [Msg("user", "t")] + [
        Msg("assistant", "", tool_calls=[{"id": f"w{i}"}] ) for i in range(3)
    ]
    msgs += [_tool_msg("短确认", "write_file", i) for i in range(3)]
    out = microcompact(msgs, keep_last=0)
    assert all("已清理" not in m.content for m in out if m.role == "tool")


def test_microcompact_keeps_structure():  # 结构保真：tool_call_id 不动、配对完整
    msgs = _long_history(7)
    out = microcompact(msgs, keep_last=0)
    called = {tc["id"] for m in out if m.role == "assistant" and m.tool_calls for tc in m.tool_calls}
    results = {m.tool_call_id for m in out if m.role == "tool"}
    assert results <= called  # 无孤儿 tool 消息


def test_if_needed_threshold():  # 阈值：低于不动（同对象返回）、超了才压
    msgs = _long_history(2)
    assert microcompact_if_needed(msgs, threshold=10**9) is msgs
    assert len(microcompact_if_needed(msgs, threshold=100)) == len(msgs)  # 压了但条数不变


def test_snip_keeps_head_and_recent_groups():  # 丢中间：保首条 user + 最近 K 组，标记并入首条
    msgs = _long_history(5)
    out = snip(msgs, keep_groups=2)
    assert out[0].content.startswith("任务") and "已省略中间" in out[0].content
    assert out[-1].content == "收尾回答"          # 尾部原文保留
    assert len(out) < len(msgs)                    # 中间真的丢了


def test_snip_group_boundary_safe():  # 组边界：切点不拆配对（无孤儿 tool 消息）
    msgs = _long_history(5)
    out = snip(msgs, keep_groups=2)
    # 每条存活 tool 消息，其 id 必须出现在某条存活的 assistant.tool_calls 里
    called = {tc["id"] for m in out if m.role == "assistant" and m.tool_calls for tc in m.tool_calls}
    results = {m.tool_call_id for m in out if m.role == "tool"}
    assert results <= called


def test_snip_noop_when_nothing_to_drop():  # 组数不足时不丢、不标
    msgs = [Msg("user", "short"), Msg("assistant", "done")]
    out = snip(msgs, keep_groups=5)
    assert [m.content for m in out] == ["short", "done"]


# ---------- reactive（适配器层，mock client） ----------


def _status_error(code: int) -> APIStatusError:
    return APIStatusError("err", response=httpx.Response(code, request=httpx.Request("POST", "http://x")), body=None)


class _FakeDelta:
    def __init__(self, content=None):
        self.content = content
        self.tool_calls = None


class _FakeChunk:
    def __init__(self, content):
        class _Choice:
            delta = _FakeDelta(content)

        self.choices = [_Choice()]


def _fake_stream(text: str = "ok"):  # 逐字符 chunk——模拟流式响应
    return [_FakeChunk(ch) for ch in text]


def _fake_llm(monkeypatch, failures: int = 0, **kw):
    """构造 DeepSeekLLM，client 替换为假件：前 failures 次抛 413。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    from agent_harness.llm import DeepSeekLLM

    llm = DeepSeekLLM("sys", **kw)
    calls: list[dict] = []

    class FakeCompletions:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) <= failures:
                raise _status_error(413)
            return _fake_stream()

    llm._client = type("F", (), {"chat": type("C", (), {"completions": FakeCompletions()})()})()
    return llm, calls


def test_reactive_retries_once_with_harsher_compaction(monkeypatch):  # 413 → 退避+加压(snip+清) → 重试一次
    monkeypatch.setattr("time.sleep", lambda s: None)  # 跳过真实退避
    llm, calls = _fake_llm(monkeypatch, failures=1)
    text, actions, _ = llm(_long_history(6), [])
    assert len(calls) == 2                       # 第一次 413 + 重试一次
    second = str(calls[1]["messages"])
    assert "已省略中间" in second                # 重试用的是 snip 加压版
    assert text == "ok"


def test_reactive_non_413_raises_without_retry(monkeypatch):  # 非 413（401）→ 不重试直接抛
    llm, calls = _fake_llm(monkeypatch, failures=0)

    class Fail401:
        def create(self, **kw):
            calls.append(kw)
            raise _status_error(401)

    llm._client = type("F", (), {"chat": type("C", (), {"completions": Fail401()})()})()
    with pytest.raises(APIStatusError):
        llm(_long_history(2), [])
    assert len(calls) == 1  # 只调了一次——没有重试


def test_reactive_gives_up_after_second_413(monkeypatch):  # 两次都 413 → 向上抛（只试一次）
    monkeypatch.setattr("time.sleep", lambda s: None)
    llm, calls = _fake_llm(monkeypatch, failures=2)
    with pytest.raises(APIStatusError):
        llm(_long_history(6), [])
    assert len(calls) == 2


def test_compaction_is_announced(monkeypatch):  # 压了就要出声——否则只看到这一轮莫名变慢
    seen = []
    llm, _ = _fake_llm(monkeypatch, compact_threshold=10, keep_last=1, on_notice=seen.append)
    llm(_long_history(4), [])
    assert any("已清理早期工具输出" in s for s in seen)


def test_no_notice_when_under_threshold(monkeypatch):  # 没压就别吵
    seen = []
    llm, _ = _fake_llm(monkeypatch, on_notice=seen.append)
    llm([Msg("user", "hi")], [])
    assert seen == []


def test_413_retry_is_announced(monkeypatch):  # 撞上限重试也要说一声
    monkeypatch.setattr("time.sleep", lambda s: None)
    seen = []
    llm, _ = _fake_llm(monkeypatch, failures=1, on_notice=seen.append)
    llm(_long_history(6), [])
    assert any("413" in s for s in seen)


def test_compaction_is_announced(monkeypatch):  # 压了就要出声——否则只看到这一轮莫名变慢
    seen = []
    llm, _ = _fake_llm(monkeypatch, compact_threshold=10, keep_last=1, on_notice=seen.append)
    llm(_long_history(4), [])
    assert any("已清理早期工具输出" in s for s in seen)


def test_no_notice_when_under_threshold(monkeypatch):  # 没压就别吵
    seen = []
    llm, _ = _fake_llm(monkeypatch, on_notice=seen.append)
    llm([Msg("user", "hi")], [])
    assert seen == []


def test_413_retry_is_announced(monkeypatch):  # 撞上限重试也要说一声
    monkeypatch.setattr("time.sleep", lambda s: None)
    seen = []
    llm, _ = _fake_llm(monkeypatch, failures=1, on_notice=seen.append)
    llm(_long_history(6), [])
    assert any("413" in s for s in seen)
