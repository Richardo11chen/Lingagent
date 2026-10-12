"""S9 单测：策略挂载点（预钩子判定/短路/fail-closed，后钩子观察，随附 .env 策略）。"""

import json

from agent_harness.hooks import deny, forbid_env_writes, run_pre
from agent_harness.llm import Action, ScriptLLM
from agent_harness.loop import agent_loop
from agent_harness.trace import Tracer


def test_pre_deny_short_circuits():  # 首个 deny 短路：后面的钩子不再跑
    calls = []

    def hook_a(action):
        calls.append("a")
        return None

    def hook_b(action):
        calls.append("b")
        return deny("不许")

    def hook_c(action):
        calls.append("c")
        return None

    r = run_pre([hook_a, hook_b, hook_c], Action(tool="x"))
    assert r.denied and r.by == "hook_b" and r.reason == "不许"
    assert calls == ["a", "b"]  # c 没跑


def test_pre_exception_fail_closed():  # 钩子抛异常 = 默认拦截（决策 2）
    def boom(action):
        raise RuntimeError("坏了")

    r = run_pre([boom], Action(tool="x"))
    assert r.denied and r.by == "boom" and "按默认拦截" in r.reason


def test_forbid_env_writes():  # 随附策略：.env 家族禁写，其他放行，读不禁
    assert forbid_env_writes(Action(tool="write_file", args={"path": ".env"})) is not None
    assert forbid_env_writes(Action(tool="edit_file", args={"path": "a/.env.local"})) is not None
    assert forbid_env_writes(Action(tool="write_file", args={"path": "notes.md"})) is None
    assert forbid_env_writes(Action(tool="read_file", args={"path": ".env"})) is None


def _recs(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().splitlines()]


def test_loop_hook_denies_and_traces(tmp_path):  # 拦下：工具不执行、回灌带原因、trace 留 hook 行
    p = tmp_path / "t.trace.jsonl"

    def block_fake(action):
        return deny("测试拦截") if action.tool == "fake" else None

    ran = []
    llm = ScriptLLM([Action(tool="fake", args={})])
    _, hist = agent_loop(
        "干活", llm, {"fake": lambda: ran.append(1) or "结果"}, "sys",
        pre_hooks=[block_fake], tracer=Tracer(p), max_steps=5,
    )
    assert ran == []  # 没执行
    fed = [m for m in hist if m.role in ("tool", "user") and "被策略拦截" in m.content]
    assert fed and "测试拦截" in fed[0].content  # 回灌带原因（模型知道怎么换路）
    recs = _recs(p)
    hook = next(r for r in recs if r["kind"] == "hook")
    assert hook["verdict"] == "deny" and hook["by"] == "block_fake"
    tool = next(r for r in recs if r["kind"] == "tool")
    assert tool["status"] == "denied" and "被策略拦截" in tool["result"]


def test_loop_hook_allow_then_execute(tmp_path):  # 放行：执行 + trace 留 allow 行
    p = tmp_path / "t.trace.jsonl"
    llm = ScriptLLM([Action(tool="fake", args={})])
    _, hist = agent_loop(
        "干活", llm, {"fake": lambda: "结果全文"}, "sys",
        pre_hooks=[lambda a: None], tracer=Tracer(p), max_steps=5,
    )
    assert any("结果全文" in m.content for m in hist)
    hook = next(r for r in _recs(p) if r["kind"] == "hook")
    assert hook["verdict"] == "allow"


def test_post_hooks_observe_and_not_on_deny():  # 后钩子只在真执行后跑；被拦不跑
    seen = []

    def post(action, result):
        seen.append((action.tool, result))

    llm = ScriptLLM([Action(tool="fake", args={})])
    agent_loop("干活", llm, {"fake": lambda: "好结果"}, "sys",
               post_hooks=[post], max_steps=5)
    assert seen == [("fake", "好结果")]

    seen.clear()
    llm2 = ScriptLLM([Action(tool="fake", args={})])
    agent_loop("干活", llm2, {"fake": lambda: "好结果"}, "sys",
               pre_hooks=[lambda a: deny("不")], post_hooks=[post], max_steps=5)
    assert seen == []  # 没执行 → 观察者看不到


def test_post_exception_swallowed():  # 观察者异常吞掉——不拖垮主流程
    def boom(action, result):
        raise RuntimeError("观察者坏了")

    llm = ScriptLLM([Action(tool="fake", args={})])
    _, hist = agent_loop("干活", llm, {"fake": lambda: "好的结果"}, "sys",
                         post_hooks=[boom], max_steps=5)
    assert any("好的结果" in m.content for m in hist)


def test_guardrail_precedes_hooks():  # 三道理序：护栏先拦 → 钩子名单根本不跑
    called = []

    def spy(action):
        called.append(action.tool)
        return None

    llm = ScriptLLM([Action(tool="run_shell", args={"command": "rm -rf x"})])
    agent_loop("干活", llm, {"run_shell": lambda **kw: "不该跑"}, "sys",
               approver=None, pre_hooks=[spy], max_steps=5)
    assert called == []
