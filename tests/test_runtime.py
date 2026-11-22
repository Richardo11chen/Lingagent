"""会话运行时：一套编排，两个入口。

这里守的是「别再抄一遍」。新建 / 切换模型 / 存档 / 回放原来在 `cli.py` 和 `server.py`
各写一遍，漂的时候没有任何测试会红——网页端静默丢弃新指令、终端却会提示"这一轮还在跑"，
同一件事两个答案。所以：

- 界面模块不许 import `session`（状态迁移只能走 `SessionRuntime`）；
- "跑着的时候拒绝"这条规则只在这里，消息也只有一份。
"""

import ast
import threading
import time
from pathlib import Path

import pytest

from agent_harness import runtime as runtime_mod
from agent_harness.settings import Settings


class FakeLLM:
    """够 runtime 用就行：门面属性 + switch。不真调模型。"""

    current = "m-one"
    current_profile = "one"
    profiles = ["one", "two"]

    def __init__(self):
        self.seen = []

    def set_notice(self, fn):
        self.notice = fn

    def switch(self, name):
        if name not in self.profiles:
            raise KeyError(name)
        self.current = self.current_profile = name
        return name

    def __call__(self, messages, tools, on_text=None, on_reasoning=None, system_prompt=None):
        return ("做完了", [], "")


@pytest.fixture(autouse=True)
def _isolate_archive_root(tmp_path, monkeypatch):
    """存档根重定向：每个用例都会造占位会话，别写进用户目录。"""
    from agent_harness import session as session_mod

    monkeypatch.setattr(session_mod, "SESSION_ROOT", tmp_path / "_archives")


def _settings(*profiles: str) -> Settings:
    """带指定 profile 的配置——runtime 判"这个模型还在不在配置里"用的就是它。"""
    from agent_harness.settings import ModelProfile

    s = Settings()
    s.models = {p: ModelProfile() for p in profiles}
    s.default_model = profiles[0]
    return s


def _rt(tmp_path, llm=None, **kw):
    return runtime_mod.SessionRuntime(
        llm=llm or FakeLLM(), settings=_settings("one", "two"), approver=None,
        workspace=tmp_path, **kw,
    )


# ---- 架构：界面不许自己动会话状态 ----


@pytest.mark.parametrize("module", ["cli", "server"])
def test_ui_modules_do_not_touch_session_storage(module):
    """`cli.py` / `server.py` 不许 import `session`，只能走 SessionRuntime。

    用测试守架构比写文档可靠：文档里那句"别再抄一遍"已经失效过一次了。
    """
    path = Path(runtime_mod.__file__).with_name(f"{module}.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))

    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + [a.name for a in node.names]
            if "session" in names:
                offenders.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name.rstrip(".").endswith("session") for a in node.names):
                offenders.append(node.lineno)

    assert offenders == [], f"{module}.py 第 {offenders} 行直接碰了 session 存档"
    assert "SessionRuntime" in path.read_text(encoding="utf-8")  # 走的是这条道


# ---- 一轮只跑一个 ----


def test_state_changes_are_refused_while_a_turn_runs(tmp_path):
    """"还在跑"的判定只有一处：四个会改会话状态的命令，报的是同一句话。"""
    release = threading.Event()

    class BlockingLLM(FakeLLM):
        def __call__(self, *a, **k):
            release.wait(5)  # 假装模型在思考
            return ("做完了", [], "")

    rt = _rt(tmp_path, llm=BlockingLLM())
    worker = threading.Thread(target=lambda: rt.run("干活"))
    worker.start()
    try:
        for _ in range(500):  # 等它真的占住（run 是在别的线程里拿的锁）
            if rt.busy:
                break
            time.sleep(0.01)
        assert rt.busy

        for call in (rt.new_session, lambda: rt.resume("any"), lambda: rt.switch_model("two")):
            with pytest.raises(runtime_mod.SessionBusy) as exc:
                call()
            assert str(exc.value) == runtime_mod.BUSY_MESSAGE
    finally:
        release.set()
        worker.join(timeout=5)

    assert not rt.busy  # 跑完就放开，不会卡住下一次


def test_a_new_session_keeps_the_model_and_approver(tmp_path):
    """切会话只换 id / 历史 / 轨迹。模型和审批记忆是"你"的东西，不在会话里。"""
    llm = FakeLLM()
    rt = _rt(tmp_path, llm=llm)
    rt.switch_model("two")
    before = rt.session_id

    view = rt.new_session()

    assert view.id != before
    assert view.profile == "two"
    assert view.history == []
    assert rt.history == []  # 原地清的那份也空了（外面抓着同一个列表）


# ---- 存档 ----


def test_run_persists_only_the_messages_of_this_turn(tmp_path, monkeypatch):
    saved: list = []
    monkeypatch.setattr(runtime_mod, "save_messages", lambda w, s, msgs: saved.append(list(msgs)))

    rt = _rt(tmp_path)
    rt.run("第一句")
    rt.run("第二句")

    assert len(saved) == 2  # 每轮落一次
    assert [m.role for m in saved[0]] == ["user", "assistant"]
    assert saved[1][0].content == "第二句"  # 只存这一轮新增的，不是整份历史


def test_a_failed_turn_is_not_persisted(tmp_path, monkeypatch):
    """跑失败时 history 没被改写，"这一轮不记"是刻意的语义。"""
    saved: list = []
    monkeypatch.setattr(runtime_mod, "save_messages", lambda w, s, msgs: saved.append(list(msgs)))

    class Boom(FakeLLM):
        def __call__(self, *a, **k):
            raise RuntimeError("网络断了")

    rt = _rt(tmp_path, llm=Boom())
    with pytest.raises(RuntimeError):
        rt.run("干活")

    assert saved == []
    assert rt.history == []
    assert not rt.busy  # 抛异常也要放开锁，否则这个会话从此不可用


# ---- 停 / 审批：这两样原来是 UI 攥着的 ----

def test_stop_reaches_the_turn_and_is_cleared_for_the_next_one(tmp_path):
    """停的旗子在 runtime 里：跑着的时候能被看见；下一轮开头自动清掉。"""
    from agent_harness.llm import Action

    holder: dict = {}

    class StopOnce(FakeLLM):
        def __init__(self):
            self.first = True

        def __call__(self, *a, **k):
            if self.first:
                self.first = False
                holder["rt"].stop()  # 第一步跑到一半，用户按了停
                return ("", [Action("noop", {})], "")
            return ("做完了", [], "")

    rt = _rt(tmp_path, llm=StopOnce())
    holder["rt"] = rt

    first = rt.run("干活")
    assert "中止" in first.reply  # 不再发起下一步

    second = rt.run("再来")
    assert second.reply == "做完了"  # 上一轮的「停」没有掐掉这一轮


def test_approval_slot_is_answered_once(tmp_path):
    """审批槽归 runtime：答一次就摘掉，迟到的答复改不了结论。"""
    rt = _rt(tmp_path)
    slot = rt.begin_approval()

    assert rt.answer_approval(True, remember=True) is True
    assert rt.answer_approval(False) is False  # 已经消费掉了
    assert slot.wait(0) == ("always", False)   # remember → always


def test_approval_times_out_as_deny(tmp_path):
    """没人答就按驳回——默认放行是安全事故，默认拒绝只是慢一步。"""
    rt = _rt(tmp_path)
    assert rt.begin_approval().wait(0.01) == ("deny", True)


def test_pending_approval_is_released_when_nobody_can_answer(tmp_path):
    """断线 / 按停：放掉挂起的审批，否则那个线程会挂到超时。"""
    rt = _rt(tmp_path)
    slot = rt.begin_approval()
    rt.deny_pending_approval()
    assert slot.wait(0) == ("deny", False)  # 不是超时，是被拒
    assert rt.deny_pending_approval() is None  # 再放一次不炸


# ---- 回放 / 模型记忆 ----


def test_resume_brings_back_its_history_and_model(tmp_path, monkeypatch):
    from agent_harness.llm import Msg

    monkeypatch.setattr(runtime_mod, "load_session", lambda w, sid: [Msg("user", "旧话")])
    monkeypatch.setattr(runtime_mod, "load_session_model", lambda w, sid: "two")

    rt = _rt(tmp_path)
    view = rt.resume("old-session")

    assert view.id == "old-session"
    assert [m.content for m in view.history] == ["旧话"]
    assert view.profile == "two"  # 会话级模型：跟着会话走
    assert view.notice == ""


def test_resume_says_so_when_the_saved_model_is_gone(tmp_path, monkeypatch):
    """原模型被删了要明说，不能静默回退——用户会以为自己还在用那个模型。"""
    monkeypatch.setattr(runtime_mod, "load_session", lambda w, sid: [])
    monkeypatch.setattr(runtime_mod, "load_session_model", lambda w, sid: "deleted-profile")

    rt = _rt(tmp_path)
    notice = rt.restore_model()

    assert "deleted-profile" in notice and "回退" in notice
    assert rt.llm.current_profile == "one"  # 没切


def test_explicit_model_wins_and_is_recorded(tmp_path, monkeypatch):
    """终端 --model 压过会话记忆，并落账到这个会话上。"""
    monkeypatch.setattr(runtime_mod, "load_session_model", lambda w, sid: "two")
    saved: list = []
    monkeypatch.setattr(runtime_mod, "save_session_model", lambda w, s, p: saved.append(p))

    rt = _rt(tmp_path)
    assert rt.restore_model(explicit="two") is None

    assert rt.llm.current_profile == "two"
    assert saved == ["two"]
