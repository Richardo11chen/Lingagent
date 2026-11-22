"""S5 单测：真模型适配器的组装逻辑（history → API messages，纯函数脱离 API 可测）。

规格 (C) 延伸：适配器里不调 API 的部分必须可确定性验证。
"""

from agent_harness.llm import Msg, to_api_messages


def test_system_prompt_comes_first():  # system 消息永远在最前（骨架）
    out = to_api_messages("sys", [Msg("user", "你好")])
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[1] == {"role": "user", "content": "你好"}


def test_plain_messages_pass_through():  # 普通 user/assistant（无 tool_calls）直转
    out = to_api_messages("sys", [Msg("user", "q"), Msg("assistant", "a")])
    assert out[1] == {"role": "user", "content": "q"}
    assert out[2] == {"role": "assistant", "content": "a"}
    assert "tool_calls" not in out[2]


def test_assistant_with_tool_calls_resent_verbatim():  # assistant 带 tool_calls → 原样重发（content + tool_calls 都在）
    raw = [{"id": "id_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}]
    out = to_api_messages("sys", [Msg("assistant", "我去读文件", tool_calls=raw)])
    assert out[1]["tool_calls"] is raw  # 原对象引用——一字不差带回
    assert out[1]["content"] == "我去读文件"


def test_tool_message_carries_id():  # tool 消息必须带 tool_call_id（API 按 id 配对）
    out = to_api_messages("sys", [Msg("tool", "文件内容", tool_call_id="id_1")])
    assert out[1] == {"role": "tool", "tool_call_id": "id_1", "content": "文件内容"}


def test_full_pair_structure():  # 完整配对：1 assistant[N calls] → N tool——数量与 id 严格对应
    raw = [
        {"id": "id_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        {"id": "id_2", "type": "function", "function": {"name": "run_tests", "arguments": "{}"}},
    ]
    messages = [
        Msg("user", "干活"),
        Msg("assistant", "并行读两个", tool_calls=raw),
        Msg("tool", "内容A", tool_call_id="id_1"),
        Msg("tool", "测试全部通过。", tool_call_id="id_2"),
    ]
    out = to_api_messages("sys", messages)
    assert len(out[2]["tool_calls"]) == 2
    tool_msgs = [m for m in out if m["role"] == "tool"]
    assert {m["tool_call_id"] for m in tool_msgs} == {"id_1", "id_2"}  # id 严格配对


def test_edit_file_replaces_unique_text(tmp_path):  # 精确替换成功：其余内容保留
    from agent_harness.tools import edit_file
    f = tmp_path / "a.py"
    f.write_text("# 注释保留\ndef add(a, b):\n    return a - b\n", encoding="utf-8")
    out = edit_file(str(f), old="return a - b", new="return a + b")
    assert "已编辑" in out
    content = f.read_text(encoding="utf-8")
    assert "return a + b" in content
    assert "# 注释保留" in content  # 注释没丢（对比 write_file 覆盖）


def test_edit_file_rejects_no_match(tmp_path):  # old 不匹配 → 拒绝并回灌原因
    from agent_harness.tools import edit_file
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    out = edit_file(str(f), old="x = 2", new="x = 3")
    assert "编辑失败" in out and "未找到" in out
    assert f.read_text(encoding="utf-8") == "x = 1\n"  # 文件没动


def test_edit_file_rejects_ambiguous(tmp_path):  # old 多次出现（不唯一）→ 拒绝，并说清几处
    from agent_harness.tools import edit_file
    f = tmp_path / "a.py"
    f.write_text("pass\npass\n", encoding="utf-8", newline="\n")
    out = edit_file(str(f), old="pass", new="continue")
    assert "编辑失败" in out and "2 次" in out and "第 1、2 行" in out
    assert f.read_text(encoding="utf-8") == "pass\npass\n"  # 文件没动


def test_long_message_keeps_head_and_tail():
    """用户粘 200k 日志：留头尾并标出省略多少，别让模型以为看到了全文。"""
    from agent_harness.compact import MAX_MESSAGE_CHARS, cap_long_messages
    from agent_harness.llm import Msg

    text = "头" * 1000 + "中" * 200_000 + "尾" * 1000
    out, capped = cap_long_messages([Msg("user", text)])

    assert capped == 1
    kept = out[0].content
    assert len(kept) < MAX_MESSAGE_CHARS
    assert kept.startswith("头") and kept.endswith("尾")  # 两头都在
    assert "中间省略" in kept and str(len(text)) in kept  # 说清省略了多少
    assert text.endswith(out[0].content[-10:])  # 原消息没被改（投影）


def test_cap_skips_tool_results_and_short_messages():
    """工具结果已被 cap_result 限到 32k；短消息一根毫毛都不动。"""
    from agent_harness.compact import cap_long_messages
    from agent_harness.llm import Msg

    tool_msg = Msg("tool", "x" * 200_000, tool_name="read_file", tool_call_id="t1")
    short = Msg("user", "你好")
    out, capped = cap_long_messages([tool_msg, short])

    assert capped == 0
    assert out[0].content == tool_msg.content and out[1].content == "你好"
