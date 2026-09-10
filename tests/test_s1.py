"""S1 单测：移除真实 LLM，核心机制全部可确定性验证（规格硬标准）。"""

from agent_harness.llm import Action, ScriptLLM
from agent_harness.loop import agent_loop
from agent_harness.tools import read_file, write_file, count_lines

SYSTEM_PROMPT = "test system prompt"


def make_tools():
    return {"read_file": read_file, "write_file": write_file, "count_lines": count_lines}


def test_1(tmp_path):  # 剧本驱动：工具结果作为 user 消息回灌进历史，写盘真发生
    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    llm = ScriptLLM(
        [
            Action("read_file", {"path": str(target)}),
            Action("write_file", {"path": str(tmp_path / "b.txt"), "content": "hi"}),
        ]
    )
    reply, history = agent_loop("做点事", llm, make_tools(), SYSTEM_PROMPT)
    assert reply == "任务完成。"
    roles = [m.role for m in history]
    # 首条 = goal 入史；done 轮（空动作列表）不产生 assistant 消息
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    # 工具结果作为 user 消息回灌：read 的内容、write 的确认都进了历史
    assert "hello" in history[2].content
    assert "写入文件" in history[4].content
    # 写盘确实发生
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "hi"


def test_2():  # 不存在的工具：回灌「工具不存在」
    llm = ScriptLLM([Action("no_such_tool", {})])
    _, history = agent_loop("做点事", llm, make_tools(), SYSTEM_PROMPT)
    assert any("工具不存在" in m.content for m in history if m.role == "user")


def test_3(tmp_path):  # 工具抛异常（读不存在的文件）：回灌「工具调用失败」
    llm = ScriptLLM([Action("read_file", {"path": str(tmp_path / "missing.txt")})])
    _, history = agent_loop("做点事", llm, make_tools(), SYSTEM_PROMPT)
    assert any("工具调用失败" in m.content for m in history if m.role == "user")


def test_4():  # 永不 done（剧本填满 max_steps 个动作）：步数耗尽停机
    llm = ScriptLLM([Action("read_file", {"path": "x"}) for _ in range(3)])
    reply, history = agent_loop("做点事", llm, {}, SYSTEM_PROMPT, max_steps=3)
    assert reply == "已达到最大步数，停机。"
    # 每步都因工具不存在回灌 user 消息，共 max_steps 轮
    assert len([m for m in history if m.role == "assistant"]) == 3


def test_5(tmp_path):  # 历史延续：第二轮传入第一轮的 history，前段一条不丢
    target = tmp_path / "a.txt"
    target.write_text("data", encoding="utf-8")
    tools = make_tools()

    llm1 = ScriptLLM([Action("read_file", {"path": str(target)})])
    _, history1 = agent_loop("第一轮", llm1, tools, SYSTEM_PROMPT)

    llm2 = ScriptLLM([Action("read_file", {"path": str(target)})])
    _, history2 = agent_loop("第二轮", llm2, tools, SYSTEM_PROMPT, history=history1)
    # 第二轮的历史包含第一轮的记录，且没丢
    assert history2[: len(history1)] == history1
    assert len(history2) > len(history1)

def test_6(tmp_path): # 剧本驱动版——count_lines 的结果（「共 N 行」）出现在回灌的 user 消息里
    target = tmp_path / "a.txt"
    target.write_text("line1\nline2\nline3", encoding="utf-8")
    llm = ScriptLLM(
        [
            Action("count_lines", {"path": str(target)}),
        ]
    )
    reply, history = agent_loop("统计行数", llm, make_tools(), SYSTEM_PROMPT)
    assert reply == "任务完成。"
    # 工具结果作为 user 消息回灌：count_lines 的结果进了历史
    assert any("共 3 行" in m.content for m in history if m.role == "user")