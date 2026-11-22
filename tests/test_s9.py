"""S9 单测：策略挂载点（预钩子判定/短路/fail-closed，后钩子观察，随附 .env 策略）。"""

import json

from agent_harness.hooks import deny, forbid_env_writes, redact_secrets, run_pre
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
    return [json.loads(line) for line in p.read_text(encoding="utf-8").strip().splitlines()]


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


def test_on_hook_fires_on_deny_only():  # 实时流回调：拦下才冒行，放行不冒
    got = []
    llm = ScriptLLM([Action(tool="fake", args={})])
    agent_loop("干活", llm, {"fake": lambda: "ok"}, "sys",
               pre_hooks=[lambda a: deny("不")],
               on_hook=lambda by, reason: got.append((by, reason)), max_steps=3)
    assert got == [("<lambda>", "不")]

    got.clear()
    llm2 = ScriptLLM([Action(tool="fake", args={})])
    agent_loop("干活", llm2, {"fake": lambda: "ok"}, "sys",
               pre_hooks=[lambda a: None],
               on_hook=lambda by, reason: got.append((by, reason)), max_steps=3)
    assert got == []  # 放行不冒行（防噪音）


def test_guardrail_precedes_hooks():  # 三道理序：护栏先拦 → 钩子名单根本不跑
    called = []

    def spy(action):
        called.append(action.tool)
        return None

    llm = ScriptLLM([Action(tool="run_shell", args={"command": "rm -rf x"})])
    agent_loop("干活", llm, {"run_shell": lambda **kw: "不该跑"}, "sys",
               approver=None, pre_hooks=[spy], max_steps=5)
    assert called == []


# ---- 后钩子 v2：能改写结果（脱敏）----


def test_post_can_rewrite_the_result(tmp_path):
    """钩子返回字符串 = 替换结果；不返回 = 没意见。链式：后一个看到前一个的产物。"""
    seen = []

    def mask(action, result):
        return result.replace("秘密", "***")

    def spy(action, result):
        seen.append(result)
        return None

    llm = ScriptLLM([Action(tool="fake", args={})])
    _, hist = agent_loop(
        "干活", llm, {"fake": lambda: "这里有秘密"}, "sys",
        post_hooks=[mask, spy], max_steps=5,
    )

    assert seen == ["这里有***"]  # 第二个钩子看到的是改写后的
    # 回灌的 tool 结果在 history 里是 user 角色 + tool_name 承载的（这个项目的老约定）
    fed = [m.content for m in hist if m.tool_name == "fake"]
    assert any("这里有***" in c for c in fed)


def test_post_rewrite_reaches_trace(tmp_path):
    """trace 是不截断的审计账——脱敏必须发生在写账之前，否则密钥就永久落盘了。"""
    p = tmp_path / "t.jsonl"
    raw = "DEEPSEEK_API_KEY=sk-abcdefghijklmnopqrstuvwx"

    llm = ScriptLLM([Action(tool="fake", args={})])
    agent_loop("干活", llm, {"fake": lambda: raw}, "sys",
               tracer=Tracer(p), post_hooks=[redact_secrets], max_steps=5)

    text = p.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnopqrstuvwx" not in text  # 原文没进账
    assert "sk-***" in text


def test_redact_secrets_shapes():
    """只认高置信度前缀：宁可漏一个，也不要把正常文本打成星号。"""
    assert redact_secrets(None, "key=sk-abcdefghijklmnopqrstuvwx") == "key=sk-***"
    assert redact_secrets(None, "ghp_1234567890abcdefghijABCD") == "gh*_***"
    assert redact_secrets(None, "AKIAIOSFODNN7EXAMPLE") == "AKIA***"
    assert redact_secrets(None, "xoxb-1234567890-abcdefghij") == "xox*-***"

    # 不像密钥的东西原样返回（None = 没意见，调用方据此判断"改没改"）
    assert redact_secrets(None, "普通输出，没有密钥") is None
    assert redact_secrets(None, "sk-short") is None  # 太短，可能是正常词
    assert redact_secrets(None, "任务完成") is None
