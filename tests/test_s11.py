"""S11 单测：命令风险判定（只读放行 / 改动·执行·网络·未知问人 / 极危险直拒）。"""

import json

from agent_harness.guardrail import classify_command
from agent_harness.llm import Action, ScriptLLM
from agent_harness.loop import agent_loop
from agent_harness.trace import Tracer


def risk(cmd: str) -> str:
    return classify_command(cmd).risk


def test_read_only_allowed():  # 只读命令放行（不吵人）
    for cmd in (
        "dir", "dir /b tests", "type README.md", 'findstr "def" tests\\*.py',
        "git status", "git log --oneline -5", "git diff", "git remote -v",
        "python --version", "where python", "echo hello", "cd src", "tasklist",
        'echo "a & b"', 'type "C:\\a>b.txt"', "dir 2>&1",
    ):
        assert risk(cmd) == "read_only", cmd


def test_mutating_asks():  # 改动类：裸 del、重定向、复制移动都进闸
    for cmd in (
        "del file.txt", "rm x", "copy a b", "move a b", "mkdir out",
        "echo hi > f.txt", "dir > listing.txt", "type a 2>err.txt",
        "attrib +r f",
    ):
        assert risk(cmd) != "read_only", cmd


def test_executing_asks():  # 解释器 / 脚本 / 跑测试：全部问人
    for cmd in (
        'python -c "print(1)"', "python script.py", "pytest -q",
        "powershell -Command Get-ChildItem", "cmd /c del x",
        "npm install", "node app.js",
    ):
        assert risk(cmd) == "executing", cmd


def test_network_asks():
    for cmd in ("curl http://x", "wget http://x", "certutil -urlcache -f http://x y"):
        assert risk(cmd) == "network", cmd


def test_unknown_asks():  # 判不出来 → 问人（fail closed）
    for cmd in ("weirdtool --x", "C:\\Tools\\mystery.exe", "mystery.bat"):
        assert risk(cmd) == "unknown", cmd


def test_compound_strictest():  # 复合命令：任一段有风险 → 整条有风险
    assert risk("dir & del x") != "read_only"
    assert risk("echo hi > f && dir") != "read_only"
    assert risk("git status | findstr x") == "read_only"  # 两段都只读才放行
    assert risk("dir & format c:") == "catastrophic"


def test_unbalanced_quote_unknown():  # 解析不了 → 问人
    assert risk('echo "abc') == "unknown"


def test_catastrophic_class():  # 极危险：直拒
    for cmd in (
        "format c:", "mkfs.ext4 /dev/sda1", "diskpart /s x.txt",
        "rm -rf /", "del /s /q C:\\", "dd if=x of=/dev/sda",
    ):
        assert risk(cmd) == "catastrophic", cmd


def test_loop_read_only_no_approval():  # 只读命令不问人
    asked, ran = [], []
    llm = ScriptLLM([Action(tool="run_shell", args={"command": "dir"})])
    agent_loop("干活", llm, {"run_shell": lambda **kw: ran.append(1) or "退出码 0"},
               "sys", approver=lambda c: asked.append(c) or True, max_steps=3)
    assert asked == [] and ran == [1]


def test_loop_mutating_asks_and_runs_when_approved():  # 非只读问人；批准才执行
    asked, ran = [], []
    llm = ScriptLLM([Action(tool="run_shell", args={"command": "del x"})])
    agent_loop("干活", llm, {"run_shell": lambda **kw: ran.append(1) or "退出码 0"},
               "sys", approver=lambda c: asked.append(c) or True, max_steps=3)
    assert asked == ["del x"] and ran == [1]


def test_loop_unknown_without_approver_blocked():  # 无审批者 → fail closed
    ran = []
    llm = ScriptLLM([Action(tool="run_shell", args={"command": "weirdtool"})])
    _, hist = agent_loop("干活", llm, {"run_shell": lambda **kw: ran.append(1) or "x"},
                         "sys", max_steps=3)
    assert ran == []
    assert any("被策略拦截" in m.content for m in hist if m.role == "user")


def test_loop_catastrophic_blocked_without_asking():  # 极危险：直接拒，不给审批
    asked, ran = [], []
    llm = ScriptLLM([Action(tool="run_shell", args={"command": "format c:"})])
    _, hist = agent_loop("干活", llm, {"run_shell": lambda **kw: ran.append(1) or "x"},
                         "sys", approver=lambda c: asked.append(c) or True, max_steps=3)
    assert asked == [] and ran == []
    assert any("直接拒绝" in m.content for m in hist if m.role == "user")


def test_trace_records_risk(tmp_path):  # 判定进 trace
    p = tmp_path / "t.trace.jsonl"
    llm = ScriptLLM([Action(tool="run_shell", args={"command": "dir"})])
    agent_loop("干活", llm, {"run_shell": lambda **kw: "退出码 0"}, "sys",
               tracer=Tracer(p), max_steps=3)
    recs = [json.loads(line) for line in p.read_text(encoding="utf-8").strip().splitlines()]
    tool = next(r for r in recs if r["kind"] == "tool")
    assert tool["risk"] == "read_only"
