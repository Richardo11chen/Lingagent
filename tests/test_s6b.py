"""S6b 单测：Web 服务的接线（注入假 LLM——不发真 API）。

覆盖：WS 会话/流式事件、线程桥接、审批跨进程（Event 等待/唤醒）、驳回回灌、done。
"""

import pytest
from fastapi.testclient import TestClient

from agent_harness import assembly, server
from agent_harness.llm import Action
from agent_harness.settings import Settings


@pytest.fixture(autouse=True)
def _isolate_archive_root(tmp_path, monkeypatch):
    """存档根重定向到临时目录——WS 连接会造占位会话，别污染 ~/.agent_harness_sessions。"""
    from agent_harness import session as session_mod

    monkeypatch.setattr(session_mod, "SESSION_ROOT", tmp_path / "_archives")


class FakeLLM:
    """假模型：第一轮发危险命令动作；后续轮回 done。"""

    current = "fake-model"  # SwitchableLLM 门面属性（server 的 session 事件带模型名）

    def set_notice(self, fn):  # 门面接口：server 每轮给模型挂「压缩提示」口
        self.notice = fn

    def __init__(self, *a, **kw):
        self.calls = 0

    def __call__(self, messages, tools, on_text=None, on_reasoning=None, system_prompt=None):
        self.calls += 1
        if self.calls == 1:
            if on_text:
                on_text("准备执行命令…")
            return ("", [Action(tool="run_shell", args={"command": "rm -rf test_dir"})], "")
        return ("已收到驳回，未执行。", [], "")


def _no_save(workspace, session_id, msgs):  # 测试不落盘
    return None


def test_ws_streaming_and_done(monkeypatch, tmp_path):  # 基本流：session → chunk → done
    monkeypatch.setattr(server, "load_settings_and_llm", lambda sp, m=None, **kw: (FakeLLM(), Settings()))
    monkeypatch.setattr(server, "save_messages", _no_save)
    monkeypatch.setattr(server, "trace_path", lambda w, s: tmp_path / f"{s}.trace.jsonl")
    c = TestClient(server.app)
    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "user", "text": "干活"})
        types = []
        while True:
            m = ws.receive_json()
            types.append(m["type"])
            if m["type"] == "approval_request":
                ws.send_json({"type": "approval_response", "approved": False})  # 驳回
            if m["type"] == "assistant_done":
                assert "驳回" in m["text"]
                break
        assert "assistant_chunk" in types       # 流式
        assert "tool" in types                  # 工具提示
        assert "approval_request" in types      # 审批被触发（桥接通）


def test_ws_approval_approved(monkeypatch, tmp_path):  # 批准路径：命令真执行（run_shell 被调）
    monkeypatch.setattr(server, "load_settings_and_llm", lambda sp, m=None, **kw: (FakeLLM(), Settings()))
    monkeypatch.setattr(server, "save_messages", _no_save)
    monkeypatch.setattr(server, "trace_path", lambda w, s: tmp_path / f"{s}.trace.jsonl")
    ran = {}

    def fake_run_shell(command: str) -> str:
        ran["cmd"] = command
        return "退出码 0\n(假执行)"

    monkeypatch.setitem(assembly.tools, "run_shell", fake_run_shell)  # 会话工具表从注册表拷贝（S10）
    c = TestClient(server.app)
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "user", "text": "干活"})
        while True:
            m = ws.receive_json()
            if m["type"] == "approval_request":
                ws.send_json({"type": "approval_response", "approved": True})  # 批准
            if m["type"] == "assistant_done":
                break
    assert ran.get("cmd") == "rm -rf test_dir"  # 批准后真执行


def test_ensure_web_skips_when_running(monkeypatch, tmp_path):
    # 幂等：端口都有人在听 → 一个进程都不许拉
    from agent_harness import weblaunch

    calls = []
    monkeypatch.setattr(weblaunch, "_port_open", lambda port: True)
    monkeypatch.setattr(weblaunch, "_backend_is_stale", lambda port: False)  # 对面是当前版本
    monkeypatch.setattr(weblaunch.subprocess, "Popen", lambda *a, **k: calls.append(a))
    assert weblaunch.ensure_web(8000, tmp_path, tmp_path) == ([], None)
    assert calls == []


def test_ensure_web_spawns_missing(monkeypatch, tmp_path):
    # 缺谁拉谁：后端（python -m）+ 前端（npm.cmd → cmd /c）
    from agent_harness import weblaunch

    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text("{}", encoding="utf-8")
    spawned = []
    monkeypatch.setattr(weblaunch, "_port_open", lambda port: False)
    monkeypatch.setattr(weblaunch.shutil, "which", lambda name: "C:/fake/npm.cmd")
    monkeypatch.setattr(weblaunch.subprocess, "Popen", lambda argv, **k: spawned.append(argv))

    started, warning = weblaunch.ensure_web(8000, tmp_path, web)
    assert started == ["后端", "前端"] and warning is None
    assert spawned[0][1:] == ["-m", "agent_harness.server"]
    assert spawned[1][0].lower().endswith("cmd.exe") and "run" in spawned[1]


def test_ensure_web_warns_about_a_stale_backend(monkeypatch, tmp_path):
    """端口有人听、但对面是旧代码 → 不许静默复用，必须说出来。"""
    from agent_harness import weblaunch

    spawned = []
    monkeypatch.setattr(weblaunch, "_port_open", lambda port: True)
    monkeypatch.setattr(weblaunch, "_backend_is_stale", lambda port: True)
    monkeypatch.setattr(weblaunch.subprocess, "Popen", lambda *a, **k: spawned.append(a))

    started, warning = weblaunch.ensure_web(8000, tmp_path, tmp_path)
    assert started == [] and spawned == []  # 不乱拉第二份
    assert warning and "不是当前版本" in warning


def test_health_reports_the_protocol_weblaunch_expects():
    from agent_harness import server, weblaunch

    assert server.PROTOCOL == weblaunch.EXPECTED_PROTOCOL  # 两处常量不许漂
    body = TestClient(server.app).get("/health").json()
    assert body == {"app": "agent-harness", "protocol": server.PROTOCOL}


def test_ws_hook_denied_event(monkeypatch, tmp_path):  # 策略拦下 → WS 推 hook 事件（实时流一行）
    class EnvLLM:
        current = "fake-model"

        def set_notice(self, fn):
            self.notice = fn

        def __init__(self, *a, **kw):
            self.calls = 0

        def __call__(self, messages, tools, on_text=None, on_reasoning=None, system_prompt=None):
            if self.calls:
                return ("完成", [], "")
            self.calls += 1
            return ("", [Action(tool="write_file", args={"path": ".env", "content": "x"})], "")

    monkeypatch.setattr(server, "load_settings_and_llm", lambda sp, m=None, **kw: (EnvLLM(), Settings()))
    monkeypatch.setattr(server, "save_messages", _no_save)
    monkeypatch.setattr(server, "trace_path", lambda w, s: tmp_path / f"{s}.trace.jsonl")
    c = TestClient(server.app)
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()  # session
        ws.send_json({"type": "user", "text": "干活"})
        hook_ev = None
        while True:
            m = ws.receive_json()
            if m["type"] == "hook":
                hook_ev = m
            if m["type"] == "assistant_done":
                break
    assert hook_ev is not None
    assert hook_ev["by"] == "forbid_env_writes" and "敏感文件" in hook_ev["reason"]


def test_ws_trace_request(monkeypatch, tmp_path):  # 轨迹时间线：trace_request → trace 事件（只读）
    monkeypatch.setattr(server, "load_settings_and_llm", lambda sp, m=None, **kw: (FakeLLM(), Settings()))
    tf = tmp_path / "t.trace.jsonl"
    tf.write_text('{"kind": "model", "step": 1, "n_actions": 0}\n', encoding="utf-8")
    monkeypatch.setattr(server, "trace_path", lambda w, s: tf)
    c = TestClient(server.app)
    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "session"
        assert ws.receive_json()["type"] == "status"  # 连上就报一次：审批模式 + 上下文用量
        ws.send_json({"type": "trace_request"})
        ev = ws.receive_json()
    assert ev["type"] == "trace"
    assert ev["total"] == 1 and ev["list"][0]["kind"] == "model"


def test_ws_auto_approve_toggle(monkeypatch):  # 网页版自动批准：开关状态由服务端回执，别只在前端记
    monkeypatch.setattr(server, "load_settings_and_llm", lambda sp, m=None, **kw: (FakeLLM(), Settings()))
    c = TestClient(server.app)
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()  # session
        assert ws.receive_json()["auto"] is False

        ws.send_json({"type": "set_auto", "on": True})
        ev = ws.receive_json()
        assert ev["type"] == "status" and ev["auto"] is True
        assert ev["limit"] > 0  # 分母是压缩阈值
        assert ev["used"] >= 0

        ws.send_json({"type": "set_auto", "on": False})
        assert ws.receive_json()["auto"] is False


def test_ws_new_session_switches_id_without_reconnect(monkeypatch):
    monkeypatch.setattr(server, "load_settings_and_llm", lambda sp, m=None, **kw: (FakeLLM(), Settings()))
    c = TestClient(server.app)
    with c.websocket_connect("/ws") as ws:
        first = ws.receive_json()["id"]
        ws.receive_json()  # status

        ws.send_json({"type": "new_session"})
        ev = ws.receive_json()
        assert ev["type"] == "session" and ev["id"] != first  # 换了会话
        assert ws.receive_json() == {"type": "history", "messages": []}
        assert ws.receive_json()["type"] == "status"


def test_server_cli_model_fail_fast(tmp_path):
    # 启动期就拦住打错的 profile 名（别等 WS 连上才在 handler 里炸——前端只看到连接即断）
    import os
    import subprocess
    import sys

    cfg = tmp_path / "c.toml"
    cfg.write_text('[models.a]\nname = "m"\n', encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.argv=['server', '--model', 'nope'];"
         "from agent_harness.server import main; main()"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "AGENT_HARNESS_CONFIG": str(cfg), "PYTHONIOENCODING": "utf-8"},
        timeout=120,
    )
    assert r.returncode == 1
    assert "未知模型 profile" in r.stderr
