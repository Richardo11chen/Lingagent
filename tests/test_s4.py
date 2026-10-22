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


def test_prune_empty_sessions(tmp_path):  # 打扫：只删「空且老」的占位——有内容的/刚建的不动
    import os
    import time

    from agent_harness.session import prune_empty_sessions, session_path

    old_empty = new_session_id(tmp_path)      # 空占位（旧）→ 该删
    fresh_empty = new_session_id(tmp_path)    # 空占位（新）→ 留着
    used = new_session_id(tmp_path)           # 有内容（旧）→ 不碰
    save_messages(tmp_path, used, [Msg("user", "有内容")])
    past = time.time() - 7200
    for sid in (old_empty, used):             # 把两个的 mtime 调老
        os.utime(session_path(tmp_path, sid), (past, past))

    assert prune_empty_sessions(tmp_path) == 1
    left = {s["id"] for s in list_sessions(tmp_path)}
    assert old_empty not in left
    assert fresh_empty in left and used in left


def test_session_model_meta_roundtrip(tmp_path):  # 会话级模型：meta 行落盘/读回/后写为准/不进消息
    from agent_harness.session import load_session_model, save_session_model

    sid = new_session_id(tmp_path)
    save_messages(tmp_path, sid, [Msg("user", "hi")])
    assert load_session_model(tmp_path, sid) is None          # 没切过 = 没记录
    assert load_session_model(tmp_path, "不存在的会话") is None  # 文件不存在 = None
    save_session_model(tmp_path, sid, "MiniMax-M3")
    assert load_session_model(tmp_path, sid) == "MiniMax-M3"
    save_session_model(tmp_path, sid, "deepseek-flash")       # 再切：后写的为准
    assert load_session_model(tmp_path, sid) == "deepseek-flash"
    assert [m.content for m in load_session(tmp_path, sid)] == ["hi"]  # meta 不进消息
    assert list_sessions(tmp_path)[0]["first_user_msg"] == "hi"        # 预览不受扰


def test_project_id_file_is_created_once(tmp_path):  # 身份文件：一个项目一个 uuid，反复调用不变
    sid = new_session_id(tmp_path)
    first = (tmp_path / session_mod.PROJECT_ID_FILE).read_text(encoding="utf-8").strip()
    assert first and sid
    new_session_id(tmp_path)  # 再开一个会话——身份不该变
    assert (tmp_path / session_mod.PROJECT_ID_FILE).read_text(encoding="utf-8").strip() == first


def test_archive_survives_project_rename(tmp_path):  # 项目改名：历史跟着走（这是整个身份的用处）
    ws = tmp_path / "proj"
    ws.mkdir()
    sid = new_session_id(ws)
    save_messages(ws, sid, [Msg("user", "改名前写的")])

    moved = tmp_path / "proj-renamed"
    ws.rename(moved)

    assert [s["id"] for s in list_sessions(moved)] == [sid]
    assert [m.content for m in load_session(moved, sid)] == ["改名前写的"]


def test_legacy_archive_is_migrated(tmp_path):  # 换布局前的老目录（纯路径名）→ 首次访问自动搬过来
    ws = tmp_path / "proj"
    ws.mkdir()
    legacy = session_mod.SESSION_ROOT / session_mod._project_key(ws)
    legacy.mkdir(parents=True)
    (legacy / "20260101-000000-000000.jsonl").write_text(
        json.dumps({"role": "user", "content": "老会话"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    listed = list_sessions(ws)

    assert [s["first_user_msg"] for s in listed] == ["老会话"]
    assert not legacy.exists()                                    # 老目录已经搬走
    assert listed[0]["id"] in {p.stem for p in session_mod._session_dir(ws).glob("*.jsonl")}


def test_load_session_repairs_dangling_tool_calls(tmp_path):  # 悬空调用补一条回应，否则之后每次请求都 400
    sid = new_session_id(tmp_path)
    raw = [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
    save_messages(tmp_path, sid, [Msg("user", "干活"), Msg("assistant", "", tool_calls=raw)])

    loaded = load_session(tmp_path, sid)

    tool_msgs = [m for m in loaded if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["c1"]
    assert tool_msgs[0].tool_name == "read_file"
    assert loaded.index(tool_msgs[0]) == 2  # 补在发起它的那条之后


def test_load_session_repairs_dangling_tool_calls(tmp_path):  # 悬空调用补一条回应，否则之后每次请求都 400
    sid = new_session_id(tmp_path)
    raw = [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
    save_messages(tmp_path, sid, [Msg("user", "干活"), Msg("assistant", "", tool_calls=raw)])

    loaded = load_session(tmp_path, sid)

    tool_msgs = [m for m in loaded if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["c1"]
    assert tool_msgs[0].tool_name == "read_file"
    assert loaded.index(tool_msgs[0]) == 2  # 补在发起它的那条之后
