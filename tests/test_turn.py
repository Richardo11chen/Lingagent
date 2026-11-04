"""一轮编排（`run_turn`）：回调透传、提示双写、失败不落盘。

为什么单测这一层：它是 TUI 与 Web 现在唯一的共用路径。之前两个入口各写一遍时，
「网页有 on_tool_result、终端没有」这种漂移没有任何测试会变红——现在只要编排层把参数
接全，入口少传一个就能在这一层被看见（`test_should_stop_is_forwarded` 就是这种守卫）。
"""

import json

import pytest

from agent_harness.llm import Action, ScriptLLM
from agent_harness.messages import Msg
from agent_harness.settings import Settings
from agent_harness.trace import Tracer
from agent_harness.turn import Session, TurnCallbacks, describe_failure, run_turn


def test_callbacks_reach_the_loop(tmp_path):
    """界面回调必须真的到 loop 手里——不然就是"接上了但没通"。"""
    seen = {"tool": [], "result": []}
    llm = ScriptLLM([Action("no_such_tool", {})])

    run_turn(
        "干活",
        llm=llm,
        settings=Settings(),
        history=[],
        tracer=Tracer(),
        approver=None,
        workspace=tmp_path,
        callbacks=TurnCallbacks(
            on_tool=lambda name, args: seen["tool"].append(name),
            on_tool_result=lambda payload: seen["result"].append(payload["status"]),
        ),
    )

    assert seen["tool"] == ["no_such_tool"]
    assert seen["result"] == ["error"]  # 未知工具 → status=error


def test_should_stop_is_forwarded(tmp_path):
    """停止信号传没传下去，直接看这一轮的收尾语——传了才会在第一步之前停下。"""
    llm = ScriptLLM([Action("no_such_tool", {})])

    result = run_turn(
        "干活",
        llm=llm,
        settings=Settings(),
        history=[],
        tracer=Tracer(),
        approver=None,
        workspace=tmp_path,
        should_stop=lambda: True,
    )

    assert "中止" in result.reply


def test_result_carries_the_prompt_it_used(tmp_path):
    """提示词只在一处读：run_turn 读的那份要带回来，调用方不用再读一次文件。"""
    result = run_turn(
        "干活",
        llm=ScriptLLM([]),
        settings=Settings(),
        history=[],
        tracer=Tracer(),
        approver=None,
        workspace=tmp_path,
        system_prompt="本轮专用的提示词",
    )
    assert result.system_prompt == "本轮专用的提示词"


def test_notice_is_shown_and_traced(tmp_path):
    """压缩提示要同时到两个地方：界面（人看得见）与 trace（事后查得到）。"""
    trace_file = tmp_path / "t.jsonl"
    shown = []
    llm = ScriptLLM([])

    run_turn(
        "干活",
        llm=llm,
        settings=Settings(),
        history=[],
        tracer=Tracer(trace_file),
        approver=None,
        workspace=tmp_path,
        callbacks=TurnCallbacks(on_notice=shown.append),
    )

    llm.notice("上下文偏长", reason="threshold")  # 适配器内部真触发时就是这么调的

    assert shown == ["上下文偏长"]
    rec = json.loads(trace_file.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["kind"] == "compact" and rec["reason"] == "threshold"


def test_failure_is_raised_and_history_is_untouched(tmp_path):
    """失败要原样抛（入口要区分中断与失败），而且不许改写调用方那份 history。"""

    class BoomLLM:
        current = "x"

        def set_notice(self, fn):
            pass

        def __call__(self, *a, **k):
            raise RuntimeError("连不上模型")

    kept = [Msg("user", "上一轮说的话")]
    with pytest.raises(RuntimeError):
        run_turn(
            "这一轮", llm=BoomLLM(), settings=Settings(), history=kept,
            tracer=Tracer(), approver=None, workspace=tmp_path,
        )

    assert [m.content for m in kept] == ["上一轮说的话"]


def test_describe_failure_is_one_line():
    assert describe_failure(RuntimeError("连不上模型")) == "本轮失败：RuntimeError: 连不上模型"


# ---- Session：会话级装配的生命周期 ----


def test_session_updates_history_in_place(tmp_path):
    """历史必须原地更新：入口手里那份引用要跟着变，否则落盘会漏掉这一轮。"""
    session = Session(
        llm=ScriptLLM([Action("no_such_tool", {})]), settings=Settings(),
        approver=None, workspace=tmp_path,
    )
    same_list = session.history
    session.run("干活")
    assert session.history is same_list
    assert len(same_list) == 4  # user / assistant / 回灌 / 收尾


def test_session_failure_leaves_history_untouched(tmp_path):
    class BoomLLM:
        current = "x"

        def set_notice(self, fn):
            pass

        def __call__(self, *a, **k):
            raise RuntimeError("断网")

    session = Session(
        llm=BoomLLM(), settings=Settings(), approver=None, workspace=tmp_path,
        history=[Msg("user", "上一轮")],
    )
    with pytest.raises(RuntimeError):
        session.run("这一轮")
    assert [m.content for m in session.history] == ["上一轮"]


def test_session_tracer_can_be_overridden_per_turn(tmp_path):
    """网页端每轮换 tracer（resume 换文件）；终端不传就用会话那个。"""
    default = tmp_path / "default.jsonl"
    per_turn = tmp_path / "turn.jsonl"
    session = Session(
        llm=ScriptLLM([Action("no_such_tool", {})]), settings=Settings(),
        approver=None, workspace=tmp_path, tracer=Tracer(default),
    )

    session.run("干活", tracer=Tracer(per_turn))

    assert "model" in per_turn.read_text(encoding="utf-8")
    assert not default.exists()  # 默认那个一次都没写
