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


PY = '"' + sys.executable.replace("\\", "/") + '"'  # 正斜杠：bash 和 cmd 都认，别赌 PATH


def test_shell_prefers_bash_over_cmd():  # 顺序：bash → pwsh → cmd；System32 的 WSL 启动器不算
    from agent_harness import shell

    shell.reset_cache()
    s = shell.detect()
    assert "cmd" not in s.name.lower(), f"退到 cmd 了：{s.name} {s.argv[0]}"
    assert "system32" not in s.argv[0].lower(), "选中了 WSL 启动器"



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


def test_run_shell_schema_exposes_timeout():  # 模型能在菜单里看到 timeout，自己调
    from agent_harness.schema import TOOL_SCHEMAS

    params = next(
        s for s in TOOL_SCHEMAS if s["function"]["name"] == "run_shell"
    )["function"]["parameters"]
    assert "timeout" in params["properties"]
    assert params["required"] == ["command"]  # timeout 可选


def test_run_shell_schema_tells_the_truth_about_the_shell():  # 换 shell 后别留 cmd 的说法
    from agent_harness.schema import TOOL_SCHEMAS

    fn = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "run_shell")["function"]
    cmd_desc = fn["parameters"]["properties"]["command"]["description"].lower()
    assert "bash" in fn["description"].lower()
    assert "bash" in cmd_desc
    assert "cmd.exe" not in cmd_desc  # 原来的"（cmd.exe 语法）"就是这么错的


def test_shell_timeout_is_clamped():  # 调大可以，但不能无限；乱给退回默认
    from agent_harness.tools import MAX_SHELL_TIMEOUT, SHELL_TIMEOUT, _clamp_timeout

    assert _clamp_timeout(30) == 30
    assert _clamp_timeout(99999) == MAX_SHELL_TIMEOUT
    assert _clamp_timeout(-1) == SHELL_TIMEOUT
    assert _clamp_timeout("abc") == SHELL_TIMEOUT
    assert _clamp_timeout(None) == SHELL_TIMEOUT


def test_shell_honours_a_larger_timeout():  # 放宽之后慢命令真的能跑完
    out = run_shell("ping -n 3 127.0.0.1", timeout=20)  # 约 2 秒
    assert "超时" not in out
    assert "退出码" in out
