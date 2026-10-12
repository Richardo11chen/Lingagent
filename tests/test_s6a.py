"""S6a 单测：TUI 降级路径（非终端 / 输入读尽）——管道喂任务不崩、审批读尽=驳回。"""

import io
import os
import subprocess
import sys

from agent_harness import tui


def test_approval_eof_denies(monkeypatch):  # 审批时输入读尽（管道/流末尾）→ 无声即驳回
    monkeypatch.setattr(tui, "interactive", lambda: False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # 空流：任何读取立刻读尽
    assert tui.show_approval("rm -rf /") == "n"


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
    assert ans.strip() == "n"
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
