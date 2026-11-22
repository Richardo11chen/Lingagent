"""工具层单测：run_shell 的超时兜底 + 输出处理（合并 / 编码 / stdin）。"""

import ast
import os
import sys
import time
from pathlib import Path

import pytest

from agent_harness import tools as tools_mod
from agent_harness.tools import (
    apply_change,
    edit_file,
    read_file,
    run_shell,
    task_output,
    task_stop,
    write_file,
)


def _slow_command(seconds: int) -> str:
    """跨平台的「睡一会儿」。

    `ping -n N` 是 Windows 语法（-n = 次数）；在 Linux 上 -n 表示"只输出数字 IP"，
    会一直 ping 下去——拿它当"跑 N 秒就结束"的命令，在 Linux 上测试会假死/假过。
    """
    return f"ping -n {seconds + 1} 127.0.0.1" if os.name == "nt" else f"sleep {seconds}"


def test_run_shell_timeout_kills_hang():
    t0 = time.perf_counter()
    out = run_shell(_slow_command(5), timeout=0.3)  # 正常要 ~5 秒
    assert "超时" in out
    assert time.perf_counter() - t0 < 4  # 没等它自己跑完


def test_tool_menu_matches_registry():  # 菜单与注册表必须同集合：漏一边就是「模型看不见」或「调不到」
    from agent_harness.assembly import tools
    from agent_harness.schema import TOOL_SCHEMAS

    menu = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert set(tools) - menu == set()                # 注册了却没菜单 → 模型永远看不到
    # 菜单多出来的只能是会话级注入的：它们要 llm / settings 才能造，进不了模块级注册表
    assert menu - set(tools) == {
        "spawn_subagent", "web_search", "fetch_url", "view_image",
    }


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
    out = run_shell(_slow_command(2), timeout=20)  # 约 2 秒
    assert "超时" not in out
    assert "退出码" in out


def _bg_id(msg: str) -> str:
    """从启动回执里取任务 id（第一行是「已在后台启动 tN」）。"""
    return msg.splitlines()[0].split()[-1]


def test_background_returns_at_once_and_then_has_output():  # 服务不会「跑完」，前台的等法根本不成立
    from agent_harness import tasks

    # 不拿墙钟断言「立刻」：CI/负载高的机器上起个进程就能花十几秒，那条断言会随机红，
    # 而它想证明的其实是「没等那条 sleep 30 跑完」——返回时进程还活着，就是这件事本身。
    msg = run_shell("echo bg-hello; sleep 30", background=True)
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


def test_write_file_returns_intent_and_apply_records_the_diff(tmp_path):  # 工具只表态，落盘在 apply
    p = tmp_path / "a.txt"

    change = write_file(str(p), "one\n")
    assert not p.exists()  # 关键：返回意图时文件还没动
    assert change.old is None  # 新建：磁盘上没有旧内容

    first = apply_change(change)
    assert p.read_text(encoding="utf-8") == "one\n"
    assert "+one" in first.diff  # 新文件 = 全是新增

    second = apply_change(write_file(str(p), "one\ntwo\n"))
    assert "a.txt" in second.diff and "+two" in second.diff
    # 两份 diff 各自独立：没有可共享的旁路状态，也就没有"串到下一条"这回事
    assert first.diff != second.diff


def test_edit_file_records_only_the_change(tmp_path):  # 替换型改动的 diff 不该带上整份文件
    p = tmp_path / "a.txt"
    apply_change(write_file(str(p), "alpha\nbeta\ngamma\n"))

    r = apply_change(edit_file(str(p), "beta", "BETA"))
    assert "+BETA" in r.diff and "-beta" in r.diff
    # 只有被改的那一行带 +/-；alpha / gamma 最多作为上下文行出现（n=3 会带进去）
    changed = [x for x in r.diff.splitlines() if x[:1] in "+-" and x[:3] not in ("+++", "---")]
    assert changed == ["-beta", "+BETA"]


def test_only_apply_change_touches_the_disk():
    """写盘只有一个出口（P0 的全部意义）。

    不守住这条，新增一个"自己写盘"的工具就等于又把 diff 弄丢了一次——
    而这次不会有 ContextVar 可以怪，只会静静地少一行显示。
    """
    tree = ast.parse(Path(tools_mod.__file__).read_text(encoding="utf-8"))
    sites = {
        fn.name
        for fn in tree.body if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "_write_text"
    }
    assert sites == {"apply_change"}, f"写盘出口不止 apply_change：{sites}"


def test_task_snapshot_shape(tmp_path, monkeypatch):  # 给界面用的结构化快照（不是给模型读的那段文字）
    from agent_harness import tasks
    from agent_harness.tools import task_snapshot

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path)
    # 假 Proc 没有 pid，别让它活到 atexit 清理那一步
    monkeypatch.setattr(tasks, "_registry", {})
    log = tmp_path / "1-tSNAP.log"
    log.write_text("listening on 5173\n", encoding="utf-8")

    class Proc:
        def poll(self):
            return None  # 还在跑

    tasks.register("tSNAP", "npm run dev", Proc(), log)
    snap = task_snapshot("tSNAP")
    assert snap["id"] == "tSNAP" and snap["alive"] is True
    assert snap["command"] == "npm run dev"
    assert "5173" in snap["tail"]
    assert snap["started"] > 0  # 排序用
    assert task_snapshot("没有这个") is None


def test_task_log_names_do_not_collide_across_processes(tmp_path, monkeypatch):
    """TUI 与网页后端是两个进程，各自都有 t1——同一秒起的日志名必须不同。

    撞名不是抽象风险：`open("wb")` 会把对方正在写的日志截断，现象是「日志只剩前几行」，
    而且两边都指望着那份日志排错。
    """

    class _Clock:  # 钉在同一秒，把撞名条件逼出来
        @staticmethod
        def time():
            return 1_700_000_000

    from agent_harness import tasks

    monkeypatch.setattr(tasks, "TASKS_DIR", tmp_path)
    monkeypatch.setattr(tasks, "time", _Clock)
    monkeypatch.setattr(tasks.os, "getpid", lambda: 11111)
    a = tasks.output_path("t1")
    monkeypatch.setattr(tasks.os, "getpid", lambda: 22222)
    b = tasks.output_path("t1")

    assert a != b
    assert a.name.endswith("-t1.log") and b.name.endswith("-t1.log")
    assert "1700000000" in a.name  # 时间戳仍在最前：按名字排序就是时间序


# ---- 文本 I/O：换行、目录、行范围、诊断 ----


def test_write_file_writes_lf_even_on_windows(tmp_path):
    """Windows 上写出 CRLF 是这次实跑踩出来的坑：模型给的多行 old 永远匹配不上。"""
    p = tmp_path / "a.js"
    apply_change(write_file(str(p), "line1\nline2\n"))
    assert b"\r\n" not in p.read_bytes()


def test_edit_file_matches_multiline_across_line_endings(tmp_path):
    """文件是 CRLF、old 是 LF，也要能改；写回统一成 LF。"""
    p = tmp_path / "a.js"
    p.write_bytes(b"line1\r\nline2\r\nline3\r\n")  # 模拟别的编辑器留下的老文件

    out = apply_change(edit_file(str(p), "line1\nline2", "x"))

    assert "已编辑" in out.content
    assert p.read_bytes() == b"x\nline3\n"


def test_edit_file_says_which_failure(tmp_path):
    """0 次和多次要能分清：前者该改 old，后者该缩小范围——报同一句话等于让人猜。"""
    p = tmp_path / "a.py"
    p.write_text("a = 1\nb = 2\na = 1\n", encoding="utf-8", newline="\n")

    miss = edit_file(str(p), "c = 3", "c = 4")
    assert "未找到" in miss

    dup = edit_file(str(p), "a = 1", "a = 9")
    assert "2 次" in dup and "第 1、3 行" in dup  # 直接说到哪去找


def test_edit_file_hints_the_nearest_line(tmp_path):
    """「未找到」时至少指出最像的那一行——不然只能整篇重读。"""
    p = tmp_path / "a.py"
    p.write_text("def handler(req):\n    return 1\n", encoding="utf-8", newline="\n")

    out = edit_file(str(p), "def handle(req):\n    return 2", "x")

    assert "最接近的是第 1 行" in out


def test_write_file_creates_missing_directories(tmp_path):
    p = tmp_path / "scripts" / "serve.mjs"
    apply_change(write_file(str(p), "console.log(1)\n"))
    assert p.exists()


def test_read_file_can_read_a_slice(tmp_path):
    """18KB 的文件不该整篇灌进上下文。"""
    p = tmp_path / "big.js"
    p.write_text("".join(f"line{i}\n" for i in range(1, 11)), encoding="utf-8", newline="\n")

    out = read_file(str(p), offset=3, limit=2)

    assert "第 3-4 行，共 10 行" in out  # 说清还有多少没看，否则会把一段当全文
    assert "line3" in out and "line5" not in out


def test_shell_marks_empty_output():
    """「跑成功但没输出」和「根本没执行」长得一样，至少标出来。"""
    out = run_shell("true")
    assert "退出码 0" in out and "没有产生任何输出" in out


@pytest.mark.skipif(os.name == "nt", reason="进程组语义只在 POSIX 上；Windows 走 taskkill /T")
def test_timeout_kills_the_whole_tree_not_just_the_shell(tmp_path):
    """超时要把孙进程一起收掉。

    `npm run build` 会起一堆 node、`pytest` 会起 worker——它们都是 shell 的孙子。
    只杀 shell 的话，这些进程会活下来继续占端口和 CPU，而模块报告"命令已被终止"，
    人只会觉得"为什么端口还被占着"。
    """
    marker = tmp_path / "child.pid"
    # 先起一个后台 sleep 并记下它的 pid（= 孙进程），再让前台卡住等超时
    out = run_shell(f"sleep 60 & echo $! > '{marker}'; sleep 60", timeout=1)
    assert "超时" in out

    pid = int(marker.read_text().strip())
    time.sleep(0.5)
    with pytest.raises(OSError):
        os.kill(pid, 0)  # 信号 0 只探测存在性；进程没了就是 ProcessLookupError


def test_read_file_refuses_oversized_files(tmp_path):
    """整读一个 500MB 日志会被 OOM 杀掉，而那看起来像"工具莫名消失"。"""
    from agent_harness.tools import MAX_READ_BYTES

    big = tmp_path / "huge.log"
    with big.open("wb") as f:
        f.truncate(MAX_READ_BYTES + 1)  # 稀疏文件，不真占磁盘

    out = read_file(str(big))

    assert "太大" in out and "sed -n" in out  # 拒绝的同时给一条出路
    assert "echo" not in out  # 没读内容


def test_dump_follows_the_given_workspace(tmp_path):
    """溢出落盘要听工作区的，而不是"进程恰好在哪"——否则 cd 过之后全文就写错地方。"""
    from agent_harness.tools import MAX_RESULT_CHARS, cap_result

    out = cap_result("x" * (MAX_RESULT_CHARS + 100), root=tmp_path)

    assert (tmp_path / ".agent-harness-out").is_dir()
    assert "全文在" in out and ".agent-harness-out" in out


def test_workspace_exposes_dump_dir(tmp_path):
    from agent_harness.workspace import Workspace

    ws = Workspace(tmp_path)
    assert ws.dump_dir == tmp_path / ".agent-harness-out"
    assert ws.relative(tmp_path / "a" / "b.py") == "a/b.py"
    assert ws.relative(tmp_path.parent / "outside.py") == str(tmp_path.parent / "outside.py")
