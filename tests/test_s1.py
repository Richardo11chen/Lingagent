"""S1 单测：移除真实 LLM，核心机制全部可确定性验证（规格硬标准）。"""

from agent_harness.llm import Action, ScriptLLM
from agent_harness.loop import agent_loop
from agent_harness.tools import read_file, write_file

SYSTEM_PROMPT = "test system prompt"


def make_tools():
    return {"read_file": read_file, "write_file": write_file}


def test_should_stop_ends_round_before_next_step(tmp_path):  # 停止按钮：当前这一步跑完，后面不再发起
    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    ran = []

    def spy(**kw):
        ran.append(kw)
        return "内容"

    llm = ScriptLLM([
        Action("read_file", {"path": "a"}),
        Action("read_file", {"path": "b"}),
        Action("read_file", {"path": "c"}),
    ])
    answer, _ = agent_loop(
        "干活", llm, {"read_file": spy}, SYSTEM_PROMPT,
        should_stop=lambda: len(ran) >= 1, max_steps=5,
    )
    assert len(ran) == 1  # 第一个动作跑完后，第二步开头就停了
    assert "中止" in answer


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
    # 首条 = goal 入史；停机轮的 assistant 回复也入史（会话持久化要求——继续会话时模型能看到自己说过什么）
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]
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
    assert reply == "已达到最大步数（3），本轮停机。想接着做就再发一句话。"
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
