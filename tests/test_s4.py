"""S4a 单测：会话持久化（JSONL 存档 / 列会话 / 恢复 / 坏行处理）。

规格 (C)：「记忆读写」= 存储管线——存进去能读出来、格式往返、边界稳。
"""

import json

import pytest

from agent_harness import session as session_mod
from agent_harness.llm import Msg
from agent_harness.session import (
    list_sessions,
    load_session,
    new_session_id,
    save_messages,
    session_path,
)


@pytest.fixture(autouse=True)
def _isolate_archive_root(tmp_path, monkeypatch):
    """存档根重定向到临时目录——测试不污染 ~/.agent_harness_sessions。"""
    monkeypatch.setattr(session_mod, "SESSION_ROOT", tmp_path / "_archives")


def test_archive_lives_under_session_root_not_workspace(tmp_path):  # 新位置：存档落在 SESSION_ROOT，项目目录保持干净
    sid = new_session_id(tmp_path)
    save_messages(tmp_path, sid, [Msg("user", "hi")])
    path = session_path(tmp_path, sid)
    root = session_mod.SESSION_ROOT  # fixture 已重定向；用模块变量读，不重复实现
    assert path.parent.parent == root                      # <root>/<项目键>/<sid>.jsonl
    assert path.suffix == ".jsonl"
    assert not (tmp_path / ".agent_sessions").exists()      # 旧位置不再被写


def test_save_load_roundtrip(tmp_path):  # 存→读往返：内容/中文/tool_calls 结构全保真
    sid = new_session_id(tmp_path)
    raw = [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}]
    msgs = [
        Msg("user", "修好 demo.py 的中文 bug"),
        Msg("assistant", "我去读文件", tool_calls=raw),
        Msg("tool", "def add(a, b):\n    return a - b", tool_call_id="call_1"),
    ]
    save_messages(tmp_path, sid, msgs)
    back = load_session(tmp_path, sid)
    assert len(back) == 3
    assert back[0].content == "修好 demo.py 的中文 bug"      # 中文往返
    assert back[1].tool_calls[0]["id"] == "call_1"            # tool_calls 结构保真
    assert back[2].tool_call_id == "call_1"                   # 配对 id 保真
    assert back[2].content.startswith("def add")


def test_append_only_multiple_rounds(tmp_path):  # 多轮追加：逐轮 save，文件累积不重写
    sid = new_session_id(tmp_path)
    save_messages(tmp_path, sid, [Msg("user", "第一轮")])
    save_messages(tmp_path, sid, [Msg("assistant", "回一"), Msg("user", "第二轮")])
    back = load_session(tmp_path, sid)
    assert [m.content for m in back] == ["第一轮", "回一", "第二轮"]
    # 文件确实是 3 行（每轮追加，不覆盖）
    path = session_path(tmp_path, sid)
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 3


def test_list_sessions_order_and_preview(tmp_path):  # 列表会话：mtime 倒序 + 首条 user 消息
    import time

    sid1 = new_session_id(tmp_path)
    save_messages(tmp_path, sid1, [Msg("user", "旧会话的内容")])
    time.sleep(0.05)  # 拉开 mtime
    sid2 = new_session_id(tmp_path)
    save_messages(tmp_path, sid2, [Msg("user", "新会话的内容")])

    sessions = list_sessions(tmp_path)
    assert [s["id"] for s in sessions] == [sid2, sid1]       # 倒序：新的在前
    assert sessions[0]["first_user_msg"] == "新会话的内容"
    assert sessions[1]["first_user_msg"] == "旧会话的内容"


def test_bad_line_skipped(tmp_path):  # 坏行（崩溃残留半行）跳过——会话其余部分仍可读
    sid = new_session_id(tmp_path)
    save_messages(tmp_path, sid, [Msg("user", "好行一"), Msg("assistant", "好行二")])
    path = session_path(tmp_path, sid)
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"role": "user", "content": "半行')          # 模拟崩溃残留
    back = load_session(tmp_path, sid)
    assert [m.content for m in back] == ["好行一", "好行二"]


def test_session_id_unique_in_rapid_succession(tmp_path):  # id 唯一：连续生成不撞（微秒+原子创建）
    ids = {new_session_id(tmp_path) for _ in range(20)}
    assert len(ids) == 20


def test_load_empty_session(tmp_path):  # 刚占位的新会话：空文件读回空列表
    sid = new_session_id(tmp_path)
    assert load_session(tmp_path, sid) == []
    # 空会话出现在列表里（first_user_msg 为空）
    assert list_sessions(tmp_path)[0]["first_user_msg"] == ""
