"""工具层单测：run_shell 的超时兜底 + 输出处理（合并 / 编码 / stdin）。"""

import sys
import time

from agent_harness.tools import read_file, run_shell, task_output, task_stop


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


def _bg_id(msg: str) -> str:
    """从启动回执里取任务 id（第一行是「已在后台启动 tN」）。"""
    return msg.splitlines()[0].split()[-1]


def test_background_returns_at_once_and_then_has_output():  # 服务不会「跑完」，前台的等法根本不成立
    from agent_harness import tasks

    start = time.time()
    msg = run_shell("echo bg-hello; sleep 30", background=True)
    assert time.time() - start < 10  # 立刻回来，不等那 30 秒
    tid = _bg_id(msg)
    try:
        assert tasks.get(tid) is not None and tasks.get(tid).alive
        time.sleep(1.2)  # 等它把 echo 写进文件
        out = task_output(tid)
        assert "运行中" in out and "bg-hello" in out
    finally:
        task_stop(tid)


def test_task_stop_kills_it_but_keeps_the_last_output():  # 停了还能回看日志（排错常用）
    from agent_harness import tasks

    tid = _bg_id(run_shell("echo before-kill; sleep 30", background=True))
    time.sleep(1.2)
    assert "已停止" in task_stop(tid)
    assert not tasks.get(tid).alive
    out = task_output(tid)
    assert "已结束" in out and "before-kill" in out


def test_task_output_rejects_unknown_id():  # 别静默返回空，让它知道 id 打错了
    out = task_output("t999")
    assert "没有任务" in out


def test_oversized_result_lands_in_a_readable_file(tmp_path, monkeypatch):
    """超限时两件事：上下文里给头尾，全文落盘并给出可读路径（省掉重跑原命令）。"""
    from agent_harness.tools import DUMP_DIR, cap_result

    monkeypatch.chdir(tmp_path)
    big = "A" * 40_000 + "MIDDLE" + "TAIL-ERR"
    out = cap_result(big)

    assert DUMP_DIR in out and "sed -n" in out  # 提示里能直接照着做
    dumps = list((tmp_path / DUMP_DIR).glob("*.txt"))
    assert len(dumps) == 1
    assert read_file(str(dumps[0])) == big  # 全文一字不少，且 read_file 读得到（围栏内）
    assert out.startswith("A" * 100) and out.endswith("TAIL-ERR")  # 头尾照旧
    assert len(out) < 33_000


def test_small_result_does_not_touch_the_disk(tmp_path, monkeypatch):  # 没超限就不该拉目录
    from agent_harness.tools import DUMP_DIR, cap_result

    monkeypatch.chdir(tmp_path)
    assert cap_result("ok") == "ok"
    assert not (tmp_path / DUMP_DIR).exists()


def test_tool_detail_covers_every_interesting_arg():  # 派活也要看得见，且不许撑成多行
    from agent_harness.tools import tool_detail

    detail = tool_detail({"subtask": "查清楚 X\n范围：src\n完成标准：给出调用链"})
    assert detail.startswith("查清楚 X 范围")
    assert "\n" not in detail  # 换行会把一行提示撑成三行

    assert tool_detail({"command": "ls -la"}) == "ls -la"
    assert tool_detail({"path": "a.py"}) == "a.py"
    assert tool_detail({"task_id": "t1"}) == "t1"
    assert tool_detail(None) == ""


def test_write_file_records_a_diff(tmp_path):  # 覆盖前先留旧内容，界面才有"改了什么"可显示
    from agent_harness.tools import take_diff, write_file

    p = tmp_path / "a.txt"
    write_file(str(p), "one\n")
    assert "+one" in take_diff()  # 新文件 = 全是新增

    write_file(str(p), "one\ntwo\n")
    diff = take_diff()
    assert "a.txt" in diff and "+two" in diff
    assert take_diff() == ""  # 取走即清空——否则会串到下一个工具上


def test_edit_file_records_only_the_change(tmp_path):  # 替换型改动的 diff 不该带上整份文件
    from agent_harness.tools import edit_file, take_diff, write_file

    p = tmp_path / "a.txt"
    write_file(str(p), "alpha\nbeta\ngamma\n")
    take_diff()

    edit_file(str(p), "beta", "BETA")
    diff = take_diff()
    assert "+BETA" in diff and "-beta" in diff
    # 只有被改的那一行带 +/-；alpha / gamma 最多作为上下文行出现（n=3 会带进去）
    changed = [l for l in diff.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---")]
    assert changed == ["-beta", "+BETA"]
