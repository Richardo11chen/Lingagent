"""工具层单测：run_shell 的超时兜底（S10 真机撞出的墙——卡住的命令不许冻住整台机器）。"""

import time

from agent_harness.tools import run_shell


def test_run_shell_timeout_kills_hang():
    t0 = time.perf_counter()
    out = run_shell("ping -n 6 127.0.0.1", timeout=0.3)  # 正常要 ~5 秒
    assert "超时" in out
    assert time.perf_counter() - t0 < 4  # 没等它自己跑完


def test_tool_menu_matches_registry():  # 菜单与注册表必须同集合：漏一边就是「模型看不见」或「调不到」
    from agent_harness.assembly import tools
    from agent_harness.schema import TOOL_SCHEMAS

    menu = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert set(tools) - menu == set()                # 注册了却没菜单 → 模型永远看不到
    assert menu - set(tools) == {"spawn_subagent"}   # 菜单多出来的只能是会话级注入的


def test_read_file_handles_gbk(tmp_path):  # 不是 utf-8 的文件也能读（Windows 老文件很常见）
    from agent_harness.tools import read_file

    p = tmp_path / "gbk.txt"
    p.write_bytes("中文内容".encode("gbk"))
    assert read_file(str(p)) == "中文内容"


def test_tool_menu_matches_registry():  # 菜单与注册表必须同集合：漏一边就是「模型看不见」或「调不到」
    from agent_harness.assembly import tools
    from agent_harness.schema import TOOL_SCHEMAS

    menu = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert set(tools) - menu == set()                # 注册了却没菜单 → 模型永远看不到
    assert menu - set(tools) == {"spawn_subagent"}   # 菜单多出来的只能是会话级注入的


def test_read_file_handles_gbk(tmp_path):  # 不是 utf-8 的文件也能读（Windows 老文件很常见）
    from agent_harness.tools import read_file

    p = tmp_path / "gbk.txt"
    p.write_bytes("中文内容".encode("gbk"))
    assert read_file(str(p)) == "中文内容"
