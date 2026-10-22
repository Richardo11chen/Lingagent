"""工具层单测：run_shell 的超时兜底 + 输出处理（合并 / 编码 / stdin）。"""

import sys
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


PY = f'"{sys.executable}"'  # 用当前解释器，别赌 PATH


def test_shell_merges_stderr_in_order():  # stderr 并入 stdout，且不丢时序
    out = run_shell(f'{PY} -c "import sys;print(\'E\',file=sys.stderr);print(\'O\')"')
    assert out.index("E") < out.index("O")  # 分通道收集再拼接会变成 O 在前


def test_shell_decodes_non_utf8_output():  # 子进程按 GBK 写字节，也不该乱码或抛异常
    out = run_shell(f'{PY} -c "import sys;sys.stdout.buffer.write(\'中文\'.encode(\'gbk\'))"')
    assert "中文" in out


def test_shell_does_not_wait_on_stdin():  # 等 stdin 的命令立刻 EOF 退出，不该挂到超时
    start = time.time()
    out = run_shell(f'{PY} -c "input()"', timeout=30)
    assert time.time() - start < 10
    assert "超时" not in out
