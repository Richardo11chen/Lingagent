"""S6a 单测：TUI 降级路径（非终端 / 输入读尽）——管道喂任务不崩、审批读尽=驳回。"""

import io
import os
import subprocess
import sys

from agent_harness import tui


def test_approval_eof_denies(monkeypatch):  # 审批时输入读尽（管道/流末尾）→ 无声即驳回
    monkeypatch.setattr(tui, "interactive", lambda: False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # 空流：任何读取立刻读尽
    assert tui.show_approval("rm -rf /") == "deny"


def test_approval_suspends_live(monkeypatch):  # 流式中途审批：Live 让位（stop→答→start）+ 决定落一行
    from agent_harness import tui

    events = []
    lines = []

    class FakeLive:
        def stop(self):
            events.append("stop")

        def start(self):
            events.append("start")

    class FakeConsole:
        def print(self, *a, **k):
            lines.append(str(a[0]) if a else "")

    monkeypatch.setattr(tui, "_live", FakeLive())
    monkeypatch.setattr(tui, "_blocks", [object()])
    monkeypatch.setattr(tui, "Live", lambda **kw: FakeLive())
    monkeypatch.setattr(tui, "console", FakeConsole())
    monkeypatch.setattr(tui, "interactive", lambda: False)
    monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))

    ans = tui.show_approval("del /q x")
    assert ans == "deny"
    assert events == ["stop", "start"]  # 让位→答→重开
    assert tui._blocks == []            # 旧块已被 stop 的最后一帧印掉——不重画
    assert any("驳回" in l for l in lines)  # 答完补一行决定记录（屏幕侧留痕）


def test_choose_session_paginates(monkeypatch, tmp_path):  # 超过一页：n 翻到第 2 页，选到第 2 页的会话
    import agent_harness
    from agent_harness import session as session_mod

    monkeypatch.setattr(session_mod, "SESSION_ROOT", tmp_path / "_archives")
    fake = [{"id": f"s{i:02d}", "mtime": 0.0, "first_user_msg": f"会话{i}"} for i in range(25)]
    monkeypatch.setattr(agent_harness, "list_visible_sessions", lambda: fake)
    monkeypatch.setattr(agent_harness, "load_session", lambda w, sid: [])
    monkeypatch.setattr(tui, "interactive", lambda: True)
    shown = []
    monkeypatch.setattr(
        tui, "show_sessions",
        lambda rows, page, total_pages, total: shown.append((page, total_pages, rows[0]["preview"])),
    )
    inputs = iter(["n", "1"])  # 翻到第 2 页 → 选第 1 个
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    sid, history = agent_harness.choose_session(tmp_path)
    assert sid == "s10"                        # 第 2 页第 1 个 = 总第 11 个
    assert [p for p, _, _ in shown] == [1, 2]  # 渲染了两页
    assert shown[1][1] == 3                    # 25 个 → 共 3 页
    assert shown[1][2] == "会话10"
    assert history == []


def test_choose_session_non_tty(monkeypatch, tmp_path):  # 非终端 → 跳过会话选择，直接新会话
    import agent_harness
    from agent_harness import session

    monkeypatch.setattr(session, "SESSION_ROOT", tmp_path / "sessions")  # 存档隔离，别写进用户目录
    monkeypatch.setattr(tui, "interactive", lambda: False)
    sid, history = agent_harness.choose_session(tmp_path)
    assert history == []
    assert sid  # 新会话 id 已生成（占位文件落在隔离的 SESSION_ROOT）


def test_pipe_exhausted_exits_cleanly(tmp_path):
    # 端到端：stdin 是空管道（DEVNULL）——主循环读到读尽应安静退出，不许 traceback
    cfg = tmp_path / "c.toml"
    cfg.write_text('[models.m]\nname = "fake"\napi_key_env = "FAKE_KEY"\n', encoding="utf-8")
    env = {
        **os.environ,
        "AGENT_HARNESS_CONFIG": str(cfg),
        "FAKE_KEY": "x",               # 构造 OpenAI 客户端只要非空 key——不起真请求
        "PYTHONIOENCODING": "utf-8",   # 中文 banner 管道输出防编码错
        "USERPROFILE": str(tmp_path),  # 会话存档根隔离（Windows 下 Path.home() 看它）
        "HOME": str(tmp_path),
    }
    r = subprocess.run(
        [sys.executable, "-c", "from agent_harness import main; main()"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, cwd=tmp_path, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "EOFError" not in r.stderr and "Traceback" not in r.stderr


def test_exit_closes_the_web_it_started(tmp_path):
    """端到端：进 TUI 拉起后端 → 退出时连它一起收掉。

    为什么要测：这两个进程是后台的，以前退出后还活着，下次启动按端口判断就会复用旧代码
    （2026-10-24 卡的那轮）。这里跑真的 main()、真起后端、真 taskkill——不是打桩。

    管道里 interactive() 是 False（也就不会拉网页端），所以只把「判断在不在终端」换成 True、
    把底下的输入换成立刻 EOF，其余全走真代码。
    """
    import socket

    with socket.socket() as s:  # 借一个此刻空闲的端口，让后端绑上去
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        '[models.m]\nname = "fake"\napi_key_env = "FAKE_KEY"\n\n'
        f"[server]\nport = {port}\n\n[web]\nautostart = true\nstop_on_exit = true\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "AGENT_HARNESS_CONFIG": str(cfg),
        "FAKE_KEY": "x",
        "PYTHONIOENCODING": "utf-8",
        "USERPROFILE": str(tmp_path),
        "HOME": str(tmp_path),
    }
    script = (
        "import pathlib\n"
        "import agent_harness as ah\n"
        f"ah.WORKSPACE = pathlib.Path(r'{tmp_path}')\n"  # 会话/轨迹落临时目录，不污染真工作区
        "import agent_harness.tui as t\n"
        "t.interactive = lambda: True\n"
        "def _eof():\n"
        "    raise EOFError\n"
        "t.ask = _eof\n"
        "ah.main()\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, cwd=tmp_path, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "网页端: http://localhost:5173" in r.stdout  # 确实走到了拉起那步
    # 「已结束」只有 taskkill 真返回成功才会印——这句就代表收尸成功
    assert "网页端已关闭：后端已结束（PID " in r.stdout, r.stdout
    with socket.socket() as s:
        s.settimeout(0.3)
        assert s.connect_ex(("127.0.0.1", port)) != 0  # 端口真空出来了



def test_let_web_finish_waits_for_the_web_turn(monkeypatch, capsys):
    """退 TUI 前先等网页那轮跑完——并说一声在等什么，别让人以为卡住了。"""
    import agent_harness
    from agent_harness.settings import Settings

    seen = {}

    def fake_wait(port, timeout, on_wait=None):
        seen["args"] = (port, timeout)
        on_wait(2)  # 模拟：网页上还有 2 轮
        return True

    monkeypatch.setattr(agent_harness, "wait_until_idle", fake_wait)
    s = Settings()
    s.server_port, s.web_stop_wait = 8123, 30.0
    agent_harness._let_web_finish(s)
    assert seen["args"] == (8123, 30.0)
    assert "还有 2 轮在跑" in capsys.readouterr().out


def test_let_web_finish_can_be_switched_off(monkeypatch):
    """stop_wait = 0 → 不等，直接关（有人就是想让 TUI 立刻退）。"""
    import agent_harness
    from agent_harness.settings import Settings

    def boom(*a, **k):
        raise AssertionError("不该调用等待")

    monkeypatch.setattr(agent_harness, "wait_until_idle", boom)
    s = Settings()
    s.web_stop_wait = 0
    agent_harness._let_web_finish(s)


def test_let_web_finish_lets_ctrl_c_through(monkeypatch):
    """等的时候按 Ctrl+C → 当场不等，而不是把退出卡到超时。"""
    import agent_harness
    from agent_harness.settings import Settings

    def interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(agent_harness, "wait_until_idle", interrupt)
    s = Settings()
    s.web_stop_wait = 120.0
    agent_harness._let_web_finish(s)  # 不抛出去


def test_slash_commands_autocomplete():  # 输入 / 就列命令——不用记口令，也不用查文档
    from prompt_toolkit.document import Document

    from agent_harness.tui import _CommandCompleter

    c = _CommandCompleter()
    assert {x.text for x in c.get_completions(Document("/", 1), None)} == {"/model", "/trace"}
    assert [x.text for x in c.get_completions(Document("/tr", 3), None)] == ["/trace"]
    assert list(c.get_completions(Document("hello", 5), None)) == []  # 普通输入不弹菜单
    # 打全了就收菜单——否则 Enter 会去「接受补全」，用户得按两次才发得出去
    assert list(c.get_completions(Document("/model", 6), None)) == []


def test_approval_ui_failure_denies_instead_of_raising(monkeypatch):
    """审批界面自己坏了也要按驳回走——真机上 pre_run 签名不匹配，TypeError 把整轮打死了。"""
    from agent_harness import tui

    class Broken:
        app = None

        def prompt(self, **kw):
            raise TypeError("boom")

    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "_approval_session", lambda: Broken())
    monkeypatch.setattr(tui, "_live", None)
    assert tui.show_approval("del x", "改动文件：del") == "deny"


def test_approval_keys_map_to_three_decisions():  # Enter 批准 / Esc 驳回 / a 记住；打错字算驳回
    from agent_harness.tui import ESC_SENTINEL, _to_decision

    assert _to_decision("") == "approve"        # 空回车 = 同意
    assert _to_decision("y") == "approve"       # 老习惯也留下
    assert _to_decision("a") == "always"        # 本会话不再问
    assert _to_decision(ESC_SENTINEL) == "deny"  # Esc
    assert _to_decision("n") == "deny"
    assert _to_decision("qwe") == "deny"        # 打错字不该变成放行
    assert _to_decision(None) == "deny"         # 读尽 / 超时


def test_shift_tab_toggles_auto_mode(monkeypatch):  # 一键切换，状态可反悔
    from agent_harness import tui

    monkeypatch.setattr(tui, "_auto", False)
    assert tui.auto_on() is False
    assert tui.toggle_auto() is True and tui.auto_on() is True
    assert tui.toggle_auto() is False and tui.auto_on() is False


def test_auto_mode_approves_without_touching_the_terminal(monkeypatch):  # 开着就别再弹审批行
    from agent_harness import tui

    def boom(*a, **kw):
        raise AssertionError("自动批准不该打开审批界面")

    monkeypatch.setattr(tui, "_auto", True)
    monkeypatch.setattr(tui, "_live", None)
    monkeypatch.setattr(tui, "_approval_session", boom)
    assert tui.show_approval("del x", "改动文件：del") == "auto"


def test_unknown_approval_result_denies():  # 白名单：只有明确的放行才算放行
    from agent_harness import _is_approved

    for ok in ("approve", "always", "auto"):
        assert _is_approved(ok) is True
    for bad in ("deny", "timeout", "error", "", "APPROVE"):
        assert _is_approved(bad) is False  # 将来新增结局时，默认落到「不执行」这一侧


def test_toolbar_shows_context_share(monkeypatch):  # 底部要看得见上下文用了多少
    from agent_harness import tui

    def line() -> str:
        return "".join(t for _, t in tui._toolbar())

    monkeypatch.setattr(tui, "_context_probe", None)
    assert "上下文" not in line()  # 没接探针时不显示（也不该崩）

    monkeypatch.setattr(tui, "_context_probe", lambda: (35_000, 70_000))
    assert "上下文 35k/70k (50%)" in line()  # 绝对值 + 占比，两个都要

    monkeypatch.setattr(tui, "_context_probe", lambda: (820, 70_000))  # 不足 1k 就不带 k
    assert "上下文 820/70k" in line()

    monkeypatch.setattr(tui, "_context_probe", lambda: (1, 0))  # 分母为 0 → 跳过
    assert "上下文" not in line()

    def boom():
        raise RuntimeError("探针坏了")

    monkeypatch.setattr(tui, "_context_probe", boom)
    assert "上下文" not in line()  # 显示层出错不该影响输入行
    assert "Shift+Tab" in line()


def test_session_picker_shows_the_model_that_will_be_used(monkeypatch, tmp_path):
    """没记过模型的会话（新建就用默认）要显示「默认」，不能是一根横杠。"""
    import agent_harness
    from agent_harness import session as session_mod

    monkeypatch.setattr(session_mod, "SESSION_ROOT", tmp_path / "_archives")
    fake = [{"id": "s1", "mtime": 0.0, "first_user_msg": "旧会话"}]
    monkeypatch.setattr(agent_harness, "list_visible_sessions", lambda: fake)
    monkeypatch.setattr(agent_harness, "load_session", lambda w, sid: [])
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "0")  # 直接开新会话
    shown: list[str] = []
    monkeypatch.setattr(tui, "show_sessions", lambda rows, *a: shown.append(rows[0]["model"]))

    agent_harness.choose_session(tmp_path, "deepseek-flash")
    assert shown == ["deepseek-flash（默认）"]

    monkeypatch.setattr(agent_harness, "load_session_model", lambda w, sid: "MiniMax-M3")
    agent_harness.choose_session(tmp_path, "deepseek-flash")
    assert shown[-1] == "MiniMax-M3"  # 记过的会话用记录值，不加"默认"
