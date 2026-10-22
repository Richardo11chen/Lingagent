"""S10 单测：子 agent（上下文隔离 / 摘要回灌 / 深度上限 / 治理继承 / trace 父子分层）。"""

import json

from agent_harness.hooks import deny
from agent_harness.llm import Action
from agent_harness.loop import agent_loop
from agent_harness.subagent import make_spawn_tool
from agent_harness.trace import Tracer


class RouteLLM:
    """假模型：按 messages[0]（父任务 / 子任务）分派剧本；记录每次看到的工具菜单。"""

    def __init__(self, scripts: dict[str, list[Action]]):
        self._scripts = {k: list(v) for k, v in scripts.items()}
        self.menus: dict[str, list[str]] = {}
        self.prompts: dict[str, str] = {}

    def __call__(self, messages, tools, on_text=None, on_reasoning=None, system_prompt=None):
        goal = messages[0].content
        self.menus.setdefault(goal, [t["function"]["name"] for t in tools])
        self.prompts.setdefault(goal, system_prompt)
        script = self._scripts.get(goal, [])
        if script:
            return ("下一步", [script.pop(0)], "")
        return (f"{goal}：完成。", [], "")


def _recs(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().splitlines()]


def _spawn(llm, tools, **kw):
    return make_spawn_tool(llm=llm, tools=tools, system_prompt="sys", depth=0, **kw)


def test_spawn_returns_report_and_isolates_child_context():
    ran = []
    tools = {"probe": lambda: ran.append("child") or "子工具结果"}
    llm = RouteLLM({
        "父任务": [Action(tool="spawn_subagent", args={"subtask": "子任务"})],
        "子任务": [Action(tool="probe", args={})],
    })
    _, hist = agent_loop(
        "父任务", llm, {**tools, "spawn_subagent": _spawn(llm, tools)}, "sys", max_steps=5
    )
    assert ran == ["child"]  # 子 agent 真的干了活
    reports = [m for m in hist if "[子 agent 报告]" in m.content]
    assert len(reports) == 1
    assert "目标：子任务" in reports[0].content
    assert "结论：子任务：完成。" in reports[0].content  # 固定模板（用户拍板）
    assert not any("子工具结果" in m.content for m in hist)  # 隔离：中间过程不进父上下文
    assert "spawn_subagent" in llm.menus["父任务"]  # 父有插座
    assert "spawn_subagent" not in llm.menus["子任务"]  # 深度 1：子没有（不能派孙）


def test_depth_limit_rejects_grandchild(tmp_path):
    p = tmp_path / "t.trace.jsonl"
    tools = {"probe": lambda: "ok"}
    llm = RouteLLM({
        "父任务": [Action(tool="spawn_subagent", args={"subtask": "子任务"})],
        "子任务": [Action(tool="spawn_subagent", args={"subtask": "孙任务"})],
    })
    agent_loop(
        "父任务", llm,
        {**tools, "spawn_subagent": _spawn(llm, tools, tracer=Tracer(p))},
        "sys", tracer=Tracer(p), max_steps=5,
    )
    assert "spawn_subagent" not in llm.menus["子任务"]
    # 子 agent 硬要派孙 → 注册表里没有 → 「工具不存在」回灌（trace 留档，sub=1）
    child_events = [r for r in _recs(p) if r.get("sub") == 1 and r["kind"] == "tool"]
    assert child_events and "工具不存在" in child_events[0]["result"]


def test_child_inherits_hooks(tmp_path):
    p = tmp_path / "t.trace.jsonl"
    ran = []
    tools = {"probe": lambda: ran.append(1) or "不该跑"}
    llm = RouteLLM({
        "父任务": [Action(tool="spawn_subagent", args={"subtask": "子任务"})],
        "子任务": [Action(tool="probe", args={})],
    })
    block = lambda a: deny("子 agent 也拦") if a.tool == "probe" else None
    agent_loop(
        "父任务", llm,
        {**tools, "spawn_subagent": _spawn(llm, tools, tracer=Tracer(p), pre_hooks=[block])},
        "sys", tracer=Tracer(p), pre_hooks=[block], max_steps=5,
    )
    assert ran == []  # 治理同一套：子 agent 的工具也被拦
    hook = next(r for r in _recs(p) if r["kind"] == "hook" and r.get("sub") == 1)
    assert hook["verdict"] == "deny" and hook["by"] == "<lambda>"


def test_child_inherits_approver():
    asked = []
    tools = {"run_shell": lambda **kw: "不该跑"}
    llm = RouteLLM({
        "父任务": [Action(tool="spawn_subagent", args={"subtask": "子任务"})],
        "子任务": [Action(tool="run_shell", args={"command": "rm -rf x"})],
    })
    spawn = _spawn(llm, tools, approver=lambda cmd, reason: asked.append(cmd) or False)
    agent_loop("父任务", llm, {**tools, "spawn_subagent": spawn}, "sys", max_steps=5)
    assert asked and "rm -rf" in asked[0]  # 同一个审批者：子 agent 的危险动作照样问人


def test_child_receives_own_system_prompt():  # 子 agent 的只读约束真的送到模型（曾经空转）
    tools = {"probe": lambda: "ok"}
    llm = RouteLLM({
        "父任务": [Action(tool="spawn_subagent", args={"subtask": "子任务"})],
        "子任务": [],
    })
    agent_loop(
        "父任务", llm, {**tools, "spawn_subagent": _spawn(llm, tools)}, "sys", max_steps=5
    )
    assert llm.prompts["父任务"] == "sys"
    child_prompt = llm.prompts["子任务"]
    assert child_prompt.startswith("sys")
    assert "默认只读" in child_prompt and "明确要求修改文件" in child_prompt


def test_child_gets_all_tools():  # 用户拍板：工具面全量，只读靠提示词默认约束
    tools = {name: (lambda **kw: "ok") for name in (
        "read_file", "run_shell", "run_tests", "write_file", "edit_file",
    )}
    llm = RouteLLM({
        "父任务": [Action(tool="spawn_subagent", args={"subtask": "子任务"})],
        "子任务": [],
    })
    agent_loop(
        "父任务", llm, {**tools, "spawn_subagent": _spawn(llm, tools)}, "sys", max_steps=5
    )
    child_menu = llm.menus["子任务"]
    assert "read_file" in child_menu and "run_shell" in child_menu
    assert "write_file" in child_menu and "edit_file" in child_menu  # 全量工具（翻案后）


def test_trace_marks_child_events_with_sub(tmp_path):
    p = tmp_path / "t.trace.jsonl"
    tools = {"probe": lambda: "ok"}
    llm = RouteLLM({
        "父任务": [Action(tool="spawn_subagent", args={"subtask": "子任务"})],
        "子任务": [Action(tool="probe", args={})],
    })
    agent_loop(
        "父任务", llm,
        {**tools, "spawn_subagent": _spawn(llm, tools, tracer=Tracer(p))},
        "sys", tracer=Tracer(p), max_steps=5,
    )
    recs = _recs(p)
    assert any(r["kind"] == "model" and r.get("sub") == 1 for r in recs)  # 子 agent 的模型事件分层
    assert any(r["kind"] == "tool" and r.get("sub") == 1 for r in recs)
    parent_tool = [r for r in recs if r["kind"] == "tool" and "sub" not in r]
    assert any(r["tool"] == "spawn_subagent" for r in parent_tool)  # 父的账不带 sub 标记


def test_child_stops_when_parent_stops():  # 父按了停止：子 agent 下一步也停（不再空转）
    ran = []
    tools = {"probe": lambda: ran.append(1) or "ok"}
    llm = RouteLLM({
        "父任务": [Action(tool="spawn_subagent", args={"subtask": "子任务"})],
        "子任务": [Action(tool="probe", args={}), Action(tool="probe", args={})],
    })
    spawn = _spawn(llm, tools, should_stop=lambda: len(ran) >= 1)
    agent_loop("父任务", llm, {**tools, "spawn_subagent": spawn}, "sys", max_steps=3)
    assert len(ran) == 1  # 子 agent 跑完第一个动作就停了
