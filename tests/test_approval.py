"""审批的会话记忆：两个入口共用一份语义。

这层之所以值得单测：终端和网页的「怎么问」天生不同（按键 vs 卡片 + Event），
而「记住什么粒度、记住之后怎么判、什么算批准」必须一样。它挂在 `agent_loop` 的
注入接口上，`agent_loop` 只看得见 True/False——漂了没人报警。
"""

import pathlib

from agent_harness.approval import APPROVED, SessionAllow, is_approved


def test_always_means_do_not_ask_again():
    asked = []

    def ask(command, reason):
        asked.append(command)
        return "always"

    allow = SessionAllow(ask)
    assert allow("rm a.txt", "改动文件") is True
    assert allow("rm b.txt", "改动文件") is True  # 同一个程序，第二次不问
    assert asked == ["rm a.txt"]


def test_single_approve_does_not_remember():
    asked = []

    def ask(command, reason):
        asked.append(command)
        return "approve"

    allow = SessionAllow(ask)
    assert allow("rm a.txt", "") is True
    assert allow("rm b.txt", "") is True
    assert asked == ["rm a.txt", "rm b.txt"]  # 每次都问


def test_unknown_decision_falls_back_to_denial():
    """界面上线新结局时，没来得及改这里也不能变成"批准"。"""
    allow = SessionAllow(lambda command, reason: "maybe")
    assert allow("rm a.txt", "") is False
    assert is_approved("maybe") is False
    assert set(APPROVED) == {"approve", "always", "auto"}


def test_grant_and_reuse_hooks_fire_once_each():
    granted, reused = [], []

    def ask(command, reason):
        return "always"

    allow = SessionAllow(ask, on_granted=granted.append, on_reused=reused.append)
    allow("rm a.txt", "")
    allow("rm b.txt", "")
    assert granted == ["rm"]   # 第一次：记住
    assert reused == ["rm"]    # 第二次：直接放行，但留一行痕


def test_no_entry_keeps_its_own_approval_memory():
    """记忆只能有一份。谁再在两个入口里各写一个集合，这条会红。

    为什么用源码检查：这是"删除式"约束——多写一份集合不会让任何功能测试失败，
    只会让两边的行为慢慢分叉。没有别的地方能观察到它。
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "agent_harness"
    offenders = [
        f"{path.name}:{marker}"
        for path in root.glob("*.py")
        for marker in ("session_allowed", "approved_programs")
        if marker in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
