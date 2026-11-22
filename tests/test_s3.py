"""S3 单测：治理护栏（危险命令识别）+ HITL 审批（approver 回调）。

规格硬标准：拦截逻辑脱离 LLM / 脱离人工 UI，可确定性验证。
"""

from pathlib import Path

from agent_harness.llm import Action, ScriptLLM
from agent_harness.loop import agent_loop
from agent_harness.tools import FileChange, apply_change, edit_file, run_shell, write_file


def make_tools():
    return {"run_shell": run_shell}


# 命令风险判定测试在 tests/test_s11.py（S11 换锁：黑名单 → 风险分级）

def test_approver_approves_executes_command(tmp_path):  # approver 批准 → 危险命令真执行（回灌执行输出，无驳回语）
    target = tmp_path / "demo_trash"
    llm = ScriptLLM([Action("run_shell", {"command": f"rm -rf {target}"})])
    _, history = agent_loop("干活", llm, make_tools(), "sys", approver=lambda cmd, reason: True)
    # 没有驳回语；回灌的是 run_shell 的执行输出（退出码开头）——证明命令真执行了
    assert not any("被驳回" in m.content for m in history if m.role == "user")
    assert any("退出码" in m.content for m in history if m.role == "user")


def test_approver_rejects_blocks_command(tmp_path):  # approver 驳回 → 回灌驳回语，命令不执行
    target = tmp_path / "demo_trash"
    llm = ScriptLLM([Action("run_shell", {"command": f"rm -rf {target}"})])
    _, history = agent_loop("干活", llm, make_tools(), "sys", approver=lambda cmd, reason: False)
    assert any("命令被驳回" in m.content for m in history if m.role == "user")
    # 没有任何执行输出（退出码）——命令没跑
    assert not any("退出码" in m.content for m in history if m.role == "user")


def test_no_approver_blocks_dangerous(tmp_path):  # approver=None → 危险命令被策略拦截，不执行
    target = tmp_path / "demo_trash"
    llm = ScriptLLM([Action("run_shell", {"command": f"rm -rf {target}"})])
    _, history = agent_loop("干活", llm, make_tools(), "sys")
    assert any("被策略拦截" in m.content for m in history if m.role == "user")
    assert not any("退出码" in m.content for m in history if m.role == "user")


def test_in_workspace_resolves_traversal(tmp_path):  # 路径穿越判定：../ 解析后越界是 False，界内是 True
    from agent_harness.guardrail import in_workspace
    assert in_workspace(str(tmp_path / "a.txt"), tmp_path)          # 界内 → True
    assert in_workspace(str(tmp_path / "sub" / "b.txt"), tmp_path)  # 嵌套子目录 → True
    assert not in_workspace(str(tmp_path / ".." / "evil.txt"), tmp_path)  # ../ 穿越 → False
    assert not in_workspace(str(Path.cwd() / "x.txt"), tmp_path)    # 工作目录外绝对路径 → False


def test_relative_path_follows_the_given_workspace(tmp_path, monkeypatch):  # 别暗中依赖进程 cwd
    from agent_harness.guardrail import in_workspace

    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(tmp_path)  # cwd 故意不等于 workspace
    assert in_workspace("a.txt", other)  # 相对路径该按 workspace 解析 → 在界内
    assert not in_workspace("../escape.txt", other)  # 穿出去仍然是 False


def test_safe_tool_blocks_outside_workspace(tmp_path):  # safe_tool：界外拦截返回越界语，界内正常返回意图
    from agent_harness.guardrail import safe_tool
    safe_write = safe_tool(write_file, tmp_path)
    blocked = safe_write(path=str(tmp_path / ".." / "evil.txt"), content="hack")
    assert "越界拦截" in blocked  # 拦截返回的是字符串：没有意图可落盘
    assert not (tmp_path.parent / "evil.txt").exists()  # 文件确实没写出去

    ok = safe_write(path=str(tmp_path / "ok.txt"), content="fine")
    assert isinstance(ok, FileChange)  # 界内：透传工具给的意图
    apply_change(ok)
    assert (tmp_path / "ok.txt").read_text(encoding="utf-8") == "fine"


def test_agent_loop_fence_blocks_traversal(tmp_path):  # 接线：剧本写 ../ 路径 → 越界拦截进历史
    from agent_harness.guardrail import safe_tool
    tools = {"write_file": safe_tool(write_file, tmp_path)}
    llm = ScriptLLM([Action("write_file", {"path": str(tmp_path / ".." / "evil.txt"), "content": "hack"})])
    _, history = agent_loop("干活", llm, tools, "sys")
    assert any("越界拦截" in m.content for m in history if m.role == "user")
    assert not (tmp_path.parent / "evil.txt").exists()


def test_loop_reports_tool_result(tmp_path):  # 工具结束后回调一次：状态 / 耗时 / 结果 / diff
    seen: list[dict] = []
    target = tmp_path / "a.txt"
    llm = ScriptLLM([Action("write_file", {"path": str(target), "content": "hello\n"})])
    agent_loop("干活", llm, {"write_file": write_file}, "sys", on_tool_result=seen.append)

    assert len(seen) == 1
    r = seen[0]
    assert r["name"] == "write_file" and r["status"] == "ok"
    assert "写入文件" in r["result"]
    assert "+hello" in r["diff"]  # 前端靠这条显示"它到底改了什么"
    assert r["ms"] >= 0


def test_tool_result_diff_does_not_leak_to_next_call(tmp_path):  # 第 2、3 次调用没写文件，diff 必须为空
    seen: list[dict] = []

    def probe() -> str:
        return "ok"

    llm = ScriptLLM([
        Action("write_file", {"path": str(tmp_path / "a.txt"), "content": "x\n"}),
        Action("probe", {}),
    ])
    agent_loop("干活", llm, {"write_file": write_file, "probe": probe}, "sys", on_tool_result=seen.append)

    assert seen[0]["diff"] and seen[1]["diff"] == ""


def test_post_hook_cannot_leak_a_diff_or_edit_the_file(tmp_path):
    """P0 的验收条件：后钩子手里没有 apply_change，改文件这件事结构性做不到。

    改之前这里会翻车——钩子调 edit_file 会把 diff 记到 ContextVar 上，而 take_diff()
    排在后钩子之后，于是纯读工具 probe 报出了别人改的那一行。
    """
    seen: list[dict] = []
    target = tmp_path / "a.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")

    def probe() -> str:
        return "ok"

    def auto_fix(action, result):  # 模拟一个"自动修格式"策略：它以为自己在改文件
        edit_file(str(target), "two", "TWO")
        return None

    llm = ScriptLLM([Action("probe", {})])
    agent_loop("干活", llm, {"probe": probe}, "sys",
               post_hooks=[auto_fix], on_tool_result=seen.append)

    assert seen[0]["name"] == "probe" and seen[0]["status"] == "ok"
    assert seen[0]["diff"] == ""  # 没碰过文件的工具不带 diff
    assert target.read_text(encoding="utf-8") == "one\ntwo\n"  # 钩子也确实没改成


def test_trace_records_what_changed(tmp_path):
    """/trace 回放要能看到改了哪几行。

    注意时序：事件在 timed 块退出时就落盘了，diff 必须在那之前记上——
    之前 take_diff() 排在块外，所以 trace 里一直缺这个字段。
    """
    from agent_harness.trace import Tracer, load_trace

    trace_path = tmp_path / "s.trace.jsonl"
    llm = ScriptLLM([Action("write_file", {"path": str(tmp_path / "a.txt"), "content": "x\n"})])
    agent_loop("干活", llm, {"write_file": write_file}, "sys", tracer=Tracer(trace_path))

    tool_ev = next(r for r in load_trace(trace_path) if r["kind"] == "tool")
    assert "+x" in tool_ev["diff"]


def test_write_failure_is_reported_not_crashed(tmp_path, monkeypatch):
    """写盘从工具搬到了执行点，错误语义也要跟着搬：失败回灌给模型，不许整轮崩。"""
    from agent_harness import tools as tools_mod

    def boom(*a, **k):
        raise OSError("磁盘满了")

    monkeypatch.setattr(tools_mod, "_write_text", boom)
    seen: list[dict] = []
    llm = ScriptLLM([Action("write_file", {"path": str(tmp_path / "a.txt"), "content": "x\n"})])
    _, history = agent_loop("干活", llm, {"write_file": write_file}, "sys",
                            on_tool_result=seen.append)

    assert seen[0]["status"] == "error"
    assert "磁盘满了" in seen[0]["result"]
    assert seen[0]["diff"] == ""  # 没写成就没有 diff，不留"界面说改了"的假象
    assert any("磁盘满了" in m.content for m in history)  # 模型看得见原因
