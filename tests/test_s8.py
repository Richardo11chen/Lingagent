"""S8 单测：轨迹记录层（事件落盘 / 空对象关闭态 / 耗时与异常留痕）。"""

import json

from agent_harness.trace import Tracer


def test_emit_appends_event(tmp_path):  # 一条事件 = 一行 JSON（ts + kind 在）
    p = tmp_path / "t.trace.jsonl"
    t = Tracer(p)
    t.emit("tool", step=1, tool="read_file", status="ok", result="全文")
    t.emit("model", step=2, n_actions=0)
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["kind"] == "tool" and rec["tool"] == "read_file" and rec["result"] == "全文"
    assert "ts" in rec


def test_null_tracer_is_silent(tmp_path):  # 关闭态（空对象）：可调用、不落盘
    t = Tracer(None)
    t.emit("tool", step=1)
    with t.timed("model", step=1):
        pass
    assert list(tmp_path.iterdir()) == []


def test_timed_records_duration_and_error(tmp_path):  # 出块落一条；异常也留痕且照常抛
    import pytest

    p = tmp_path / "t.trace.jsonl"
    t = Tracer(p)
    with t.timed("tool", step=1, tool="run_shell") as ev:
        ev.fields["status"] = "ok"
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    assert rec["duration_ms"] >= 0 and rec["status"] == "ok"

    with pytest.raises(RuntimeError):
        with t.timed("tool", step=1, tool="run_shell"):
            raise RuntimeError("boom")
    rec = json.loads(p.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["status"] == "error" and "boom" in rec["error"]


def test_load_trace_skips_bad_lines(tmp_path):  # 读回：坏行跳过；文件不存在 = 空
    from agent_harness.trace import load_trace

    p = tmp_path / "t.trace.jsonl"
    assert load_trace(p) == []  # 不存在 = 空列表
    p.write_text('{"kind": "model", "step": 1}\n不是 json\n{"kind": "tool"}\n', encoding="utf-8")
    assert [r["kind"] for r in load_trace(p)] == ["model", "tool"]


def _recs(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().splitlines()]


def test_trace_event_detail_renders_raw():  # 钻取明细：全文原样输出，方括号不会被当 markup 解析
    import io

    from rich.console import Console

    from agent_harness import tui

    buf = io.StringIO()
    old = tui.console
    tui.console = Console(file=buf, width=120)
    try:
        tui.show_trace_event(
            {
                "ts": "2026-10-12T16:05:46.037", "kind": "tool", "step": 1, "tool": "run_shell",
                "args": {"command": "del /q x & echo [exit=%errorlevel%]"},
                "status": "ok", "result": "[bold]不是 markup[/] 原始文本", "duration_ms": 5,
            },
            3,
        )
    finally:
        tui.console = old
    out = buf.getvalue()
    assert "[bold]不是 markup[/]" in out    # 原样输出——没有被当 markup 吃掉
    assert "exit=%errorlevel%" in out
    assert "#3" in out


def test_loop_records_model_name(tmp_path):  # model 事件带实际模型名（排查「到底谁在答」用）
    from agent_harness.llm import ScriptLLM
    from agent_harness.loop import agent_loop

    class NamedLLM(ScriptLLM):
        current = "minimax-m3"  # 模拟 SwitchableLLM 的 current 属性

    p = tmp_path / "t.trace.jsonl"
    agent_loop("干活", NamedLLM([]), {}, "sys", tracer=Tracer(p), max_steps=3)
    recs = _recs(p)
    assert recs[0]["kind"] == "model" and recs[0]["model"] == "minimax-m3"


def test_loop_writes_trace(tmp_path):  # 接线：一轮 = model → tool → model 三条事件
    from agent_harness.llm import Action, ScriptLLM
    from agent_harness.loop import agent_loop

    p = tmp_path / "t.trace.jsonl"
    llm = ScriptLLM([Action(tool="fake", args={"path": "x"})])
    agent_loop("干活", llm, {"fake": lambda path: "工具结果全文"}, "sys",
               tracer=Tracer(p), max_steps=5)
    recs = _recs(p)
    assert [r["kind"] for r in recs] == ["model", "tool", "model"]
    assert recs[0]["step"] == 1 and recs[0]["n_actions"] == 1
    assert recs[1]["tool"] == "fake" and recs[1]["status"] == "ok"
    assert recs[1]["result"] == "工具结果全文" and recs[1]["duration_ms"] >= 0
    assert recs[2]["n_actions"] == 0


def test_loop_traces_denied_dangerous(tmp_path):  # 驳回路径：审批与工具两条事件都留痕
    from agent_harness.llm import Action, ScriptLLM
    from agent_harness.loop import agent_loop

    p = tmp_path / "t.trace.jsonl"
    llm = ScriptLLM([Action(tool="run_shell", args={"command": "rm -rf x"})])
    agent_loop("干活", llm, {"run_shell": lambda **kw: "不该执行"}, "sys",
               approver=lambda cmd: False, tracer=Tracer(p), max_steps=5)
    recs = _recs(p)
    approval = next(r for r in recs if r["kind"] == "approval")
    tool = next(r for r in recs if r["kind"] == "tool")
    assert approval["decision"] == "denied" and approval["command"] == "rm -rf x"
    assert tool["status"] == "denied" and "驳回" in tool["result"]
