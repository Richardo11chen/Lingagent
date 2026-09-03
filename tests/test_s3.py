"""S3 单测：治理护栏（危险命令识别）+ HITL 审批（approver 回调）。

规格硬标准：拦截逻辑脱离 LLM / 脱离人工 UI，可确定性验证。
"""

from pathlib import Path

from agent_harness.guardrail import is_dangerous_command
from agent_harness.llm import Action, LLM
from agent_harness.loop import agent_loop
from agent_harness.tools import run_shell, write_file


def make_tools():
    return {"run_shell": run_shell}


def test_guardrail_detects_dangerous():  # 危险命令判定：rm -rf / del /s / shutdown 拦；pytest、format 函数调用放
    assert is_dangerous_command("rm -rf /")
    assert is_dangerous_command("rm --recursive /tmp/x")
    assert is_dangerous_command("rm -rf=force /x")  # 组合参数（子串匹配）也拦
    assert is_dangerous_command("del /s C:\\temp")
    assert is_dangerous_command("shutdown /s")
    assert not is_dangerous_command("pytest -q")
    assert not is_dangerous_command("python -c 'x = format(1)'")  # format 当函数调用不拦


def test_approver_approves_executes_command(tmp_path):  # approver 批准 → 危险命令真执行（回灌执行输出，无驳回语）
    target = tmp_path / "demo_trash"
    llm = LLM([Action("call_tool", "run_shell", {"command": f"rm -rf {target}"})])
    _, history = agent_loop("干活", llm, make_tools(), "sys", approver=lambda cmd: True)
    # 没有驳回语；回灌的是 run_shell 的执行输出（退出码开头）——证明命令真执行了
    assert not any("被驳回" in c for r, c in history if r == "user")
    assert any("退出码" in c for r, c in history if r == "user")


def test_approver_rejects_blocks_command(tmp_path):  # approver 驳回 → 回灌驳回语，命令不执行
    target = tmp_path / "demo_trash"
    llm = LLM([Action("call_tool", "run_shell", {"command": f"rm -rf {target}"})])
    _, history = agent_loop("干活", llm, make_tools(), "sys", approver=lambda cmd: False)
    assert any("命令被驳回" in c for r, c in history if r == "user")
    # 没有任何执行输出（退出码）——命令没跑
    assert not any("退出码" in c for r, c in history if r == "user")


def test_no_approver_blocks_dangerous(tmp_path):  # approver=None → 危险命令被策略拦截，不执行
    target = tmp_path / "demo_trash"
    llm = LLM([Action("call_tool", "run_shell", {"command": f"rm -rf {target}"})])
    _, history = agent_loop("干活", llm, make_tools(), "sys")
    assert any("被策略拦截" in c for r, c in history if r == "user")
    assert not any("退出码" in c for r, c in history if r == "user")


def test_in_workspace_resolves_traversal(tmp_path):  # 路径穿越判定：../ 解析后越界是 False，界内是 True
    from agent_harness.guardrail import in_workspace
    assert in_workspace(str(tmp_path / "a.txt"), tmp_path)          # 界内 → True
    assert in_workspace(str(tmp_path / "sub" / "b.txt"), tmp_path)  # 嵌套子目录 → True
    assert not in_workspace(str(tmp_path / ".." / "evil.txt"), tmp_path)  # ../ 穿越 → False
    assert not in_workspace(str(Path.cwd() / "x.txt"), tmp_path)    # 工作目录外绝对路径 → False


def test_safe_tool_blocks_outside_workspace(tmp_path):  # safe_tool：界外拦截返回越界语，界内正常执行
    from agent_harness.guardrail import in_workspace, safe_tool
    safe_write = safe_tool(write_file, tmp_path)
    blocked = safe_write(path=str(tmp_path / ".." / "evil.txt"), content="hack")
    assert "越界拦截" in blocked
    assert not (tmp_path.parent / "evil.txt").exists()  # 文件确实没写出去
    ok = safe_write(path=str(tmp_path / "ok.txt"), content="fine")
    assert "写入文件" in ok
    assert (tmp_path / "ok.txt").read_text(encoding="utf-8") == "fine"


def test_agent_loop_fence_blocks_traversal(tmp_path):  # 接线：剧本写 ../ 路径 → 越界拦截进历史
    from agent_harness.guardrail import safe_tool
    tools = {"write_file": safe_tool(write_file, tmp_path)}
    llm = LLM([Action("call_tool", "write_file", {"path": str(tmp_path / ".." / "evil.txt"), "content": "hack"})])
    _, history = agent_loop("干活", llm, tools, "sys")
    assert any("越界拦截" in c for r, c in history if r == "user")
    assert not (tmp_path.parent / "evil.txt").exists()
