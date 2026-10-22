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
        "dir README* /b 2>nul && echo --- && dir /b",  # 空设备重定向不是写文件
        "dir >nul", "dir 2>NUL", "dir 2>/dev/null",
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
               "sys", approver=lambda c, reason: asked.append(c) or True, max_steps=3)
    assert asked == [] and ran == [1]


def test_loop_mutating_asks_and_runs_when_approved():  # 非只读问人；批准才执行
    asked, ran = [], []
    llm = ScriptLLM([Action(tool="run_shell", args={"command": "del x"})])
    agent_loop("干活", llm, {"run_shell": lambda **kw: ran.append(1) or "退出码 0"},
               "sys", approver=lambda c, reason: asked.append(c) or True, max_steps=3)
    assert asked == ["del x"] and ran == [1]


def test_approver_receives_reason():  # 审批回调同时拿到「为什么问」——只给命令等于让人盲批
    seen = []
    llm = ScriptLLM([Action(tool="run_shell", args={"command": "weirdtool"})])
    agent_loop("干活", llm, {"run_shell": lambda **kw: "x"},
               "sys", approver=lambda c, reason: seen.append((c, reason)) or False, max_steps=2)
    assert seen == [("weirdtool", "未知命令：weirdtool")]


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
                         "sys", approver=lambda c, reason: asked.append(c) or True, max_steps=3)
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


def test_cmd_syntax_words_are_not_commands():  # 真机那条：for / do / 通配符 / 变量不是命令
    cmd = 'cd src\\agent_harness && for %f in (*.py) do @echo %f & @find /c /v "" < %f'
    assert risk(cmd) == "read_only", risk(cmd).reason


def test_find_read_only_unless_it_writes():  # find 默认只读；带删除 / 执行才升档
    assert risk('find . -name "*.py"') == "read_only"
    assert risk('find . -name "*.py" -delete') != "read_only"
    assert risk('find . -name "*.py" -exec rm {} ;') != "read_only"


def test_syntax_stripping_keeps_the_real_command():  # 剥语法词不能把真命令一起吞掉
    assert risk("for %f in (*.py) do @del %f") == "mutating"
    assert risk("for %f in (*.py) do @echo %f > out.txt") == "mutating"


# ---- A+：人写的允许规则（前缀语义）+ 会话内记忆 ----

def test_allow_rules_are_prefix_matches():  # `git *` = 以 git 开头的任何命令；写全了就是只这一条
    from agent_harness.guardrail import classify_command, matches_allow

    allow = ["git *", "docker ps"]
    assert matches_allow("git status", allow) == "git *"
    assert matches_allow("git push --force", allow) == "git *"
    assert matches_allow("docker ps -a", allow) is None     # 没写 * 就是精确匹配
    assert matches_allow("docker ps", allow) == "docker ps"
    assert matches_allow("gitk", allow) is None             # 不误伤同前缀的别的程序


def test_allow_rule_skips_the_question():  # 命中的规则直接放行，理由里写明是哪条规则
    from agent_harness.guardrail import classify_command

    r = classify_command("del /q build", allow=["del *"])
    assert r.risk == "read_only" and "已允许" in r.reason


def test_allow_rule_cannot_unlock_catastrophic():  # 地板：allow = ["*"] 也放不出 format / rm -rf /
    from agent_harness.guardrail import classify_command

    assert classify_command("format c:", allow=["*"]).risk == "catastrophic"
    assert classify_command("rm -rf /", allow=["*"]).risk == "catastrophic"


def test_command_head_skips_syntax_words():  # 会话内记忆按"真程序名"归类
    from agent_harness.guardrail import command_head

    assert command_head("for %f in (*.py) do @del %f") == "del"
    assert command_head('cd src && find . -name "*.py"') == "cd"
    assert command_head("git status") == "git"


def test_variable_headed_command_is_not_skipped():  # 变量里可能藏命令——带参数时必须问
    assert risk("set x=del & %x% /q a.txt") == "unknown"


def test_pure_variable_fragment_is_skipped():  # 但 for 的循环变量本身不是命令
    assert risk("for %f in (*.py) do @echo %f") == "read_only"


def test_semicolon_splits_commands():  # bash 里 ; 也是分隔符——漏掉就是 fail open
    assert risk("echo a; rm -rf build") != "read_only"
    assert risk("echo a; ls") == "read_only"


def test_bash_write_commands_ask():  # cp / mv / touch / sed -i 是 bash 侧的主力写操作
    for cmd in ("cp a b", "mv a b", "touch x", "sed -i 's/a/b/' f.txt"):
        assert risk(cmd) != "read_only", cmd
    assert risk("sed -n '1,10p' a.txt") == "read_only"  # 不带 -i 的 sed 只是看代码


def test_program_tables_do_not_overlap():  # 同一条程序只能属于一档——重叠会让结果取决于表序
    from agent_harness import guardrail as g

    table = [g.READ_ONLY_PROGRAMS, g.MUTATING_PROGRAMS, g.EXECUTING_PROGRAMS, g.NETWORK_PROGRAMS]
    for i, a in enumerate(table):
        for b in table[i + 1:]:
            assert not (a & b), sorted(a & b)


def test_version_flag_is_the_only_read_only_form():  # `python --version` 不问，`python x.py` 必须问
    assert risk("uv --version") == "read_only"
    assert risk("python script.py") != "read_only"



