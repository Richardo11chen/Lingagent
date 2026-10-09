"""S6b 单测：Web 服务的接线（注入假 LLM——不发真 API）。

覆盖：WS 会话/流式事件、线程桥接、审批跨进程（Event 等待/唤醒）、驳回回灌、done。
"""

from fastapi.testclient import TestClient

from agent_harness import server
from agent_harness.llm import Action
from agent_harness.settings import Settings


class FakeLLM:
    """假模型：第一轮发危险命令动作；后续轮回 done。"""

    def __init__(self, *a, **kw):
        self.calls = 0

    def __call__(self, messages, tools, on_text=None, on_reasoning=None):
        self.calls += 1
        if self.calls == 1:
            if on_text:
                on_text("准备执行命令…")
            return ("", [Action(tool="run_shell", args={"command": "rm -rf test_dir"})], "")
        return ("已收到驳回，未执行。", [], "")


def _no_save(workspace, session_id, msgs):  # 测试不落盘
    return None


def test_ws_streaming_and_done(monkeypatch):  # 基本流：session → chunk → done
    monkeypatch.setattr(server, "load_settings_and_llm", lambda sp, m=None: (FakeLLM(), Settings()))
    monkeypatch.setattr(server, "save_messages", _no_save)
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


def test_ws_approval_approved(monkeypatch):  # 批准路径：命令真执行（run_shell 被调）
    monkeypatch.setattr(server, "load_settings_and_llm", lambda sp, m=None: (FakeLLM(), Settings()))
    monkeypatch.setattr(server, "save_messages", _no_save)
    ran = {}

    def fake_run_shell(command: str) -> str:
        ran["cmd"] = command
        return "退出码 0\n(假执行)"

    monkeypatch.setitem(server.tools, "run_shell", fake_run_shell)
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
