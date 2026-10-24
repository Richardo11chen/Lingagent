"""S6b 单测：Web 服务的接线（注入假 LLM——不发真 API）。

覆盖：WS 会话/流式事件、线程桥接、审批跨进程（Event 等待/唤醒）、驳回回灌、done。
"""

import json
import threading
import time

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


class _FakeProc:
    """假 Popen：退出收尸只需要 pid 和 poll()。"""

    def __init__(self, pid):
        self.pid = pid
        self.poll = lambda: None  # None = 还活着


def test_ensure_web_skips_when_running(monkeypatch, tmp_path):
    # 幂等：端口都有人在听 → 一个进程都不许拉
    from agent_harness import weblaunch

    calls = []
    monkeypatch.setattr(weblaunch, "_port_open", lambda port: True)
    monkeypatch.setattr(weblaunch, "_backend_is_stale", lambda port: False)  # 对面是当前版本
    monkeypatch.setattr(weblaunch, "dev_web_dir", lambda: None)
    monkeypatch.setattr(weblaunch, "bundled_web_dir", lambda: None)
    monkeypatch.setattr(weblaunch.subprocess, "Popen", lambda *a, **k: calls.append(a))
    run = weblaunch.ensure_web(8000, tmp_path)
    assert run.started == [] and run.warning is None
    assert calls == []


def test_ensure_web_spawns_missing(monkeypatch, tmp_path):
    # 缺谁拉谁：后端（python -m）+ 前端（npm.cmd → cmd /c）
    from agent_harness import weblaunch

    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text("{}", encoding="utf-8")
    spawned = []
    monkeypatch.setattr(weblaunch, "_port_open", lambda port: False)
    monkeypatch.setattr(weblaunch, "dev_web_dir", lambda: web)  # 假装这是源码树
    monkeypatch.setattr(weblaunch, "bundled_web_dir", lambda: None)
    monkeypatch.setattr(weblaunch.shutil, "which", lambda name: "C:/fake/npm.cmd")

    def fake_popen(argv, **k):
        spawned.append(argv)
        return _FakeProc(1000 + len(spawned))

    monkeypatch.setattr(weblaunch.subprocess, "Popen", fake_popen)

    run = weblaunch.ensure_web(8000, tmp_path)
    assert run.started == ["后端", "前端"] and run.warning is None
    assert spawned[0][1:] == ["-m", "agent_harness.server"]
    assert spawned[1][0].lower().endswith("cmd.exe") and "run" in spawned[1]
    assert [p.pid for _, p in run.procs] == [1001, 1002]  # 句柄留着，退出时按 pid 收
    # 同时落一份认领凭据：TUI 万一被强杀，下次启动靠它认出这些孤儿
    st = json.loads(weblaunch._state_path().read_text(encoding="utf-8"))
    assert [(p["name"], p["pid"], p["port"]) for p in st["procs"]] == [
        ("后端", 1001, 8000), ("前端", 1002, weblaunch.FRONTEND_PORT)
    ]


def test_ensure_web_warns_about_a_stale_backend(monkeypatch, tmp_path):
    """端口有人听、但对面是旧代码 → 不许静默复用，必须说出来。"""
    from agent_harness import weblaunch

    spawned = []
    monkeypatch.setattr(weblaunch, "_port_open", lambda port: True)
    monkeypatch.setattr(weblaunch, "_backend_is_stale", lambda port: True)
    monkeypatch.setattr(weblaunch, "dev_web_dir", lambda: None)
    monkeypatch.setattr(weblaunch, "bundled_web_dir", lambda: None)
    monkeypatch.setattr(weblaunch.subprocess, "Popen", lambda *a, **k: spawned.append(a))

    run = weblaunch.ensure_web(8000, tmp_path)
    assert run.started == [] and spawned == []  # 不乱拉第二份
    assert run.warning and "不是当前版本" in run.warning


def test_stop_run_kills_only_what_this_run_spawned(monkeypatch):
    """复用别人的后端（端口已占）→ 退出时不许去杀它；自己拉的才收。"""
    from agent_harness import weblaunch

    assert weblaunch.stop_run(weblaunch.WebRun()) == []  # 什么都没拉 → 什么也不做

    killed = []
    monkeypatch.setattr(weblaunch, "_kill_tree", lambda pid: killed.append(pid) or True)
    run = weblaunch.WebRun(procs=[("后端", _FakeProc(4242))])
    assert weblaunch.stop_run(run) == ["后端已结束（PID 4242）"]
    assert killed == [4242]


def test_stop_run_reports_a_process_that_already_died(monkeypatch):
    """后端自己崩了（PID 早没了）→ 别报「结束失败」吓人，照实说已自行退出。"""
    from agent_harness import weblaunch

    proc = _FakeProc(7)
    proc.poll = lambda: 1  # 已退出
    killed = []
    monkeypatch.setattr(weblaunch, "_kill_tree", lambda pid: killed.append(pid) or True)
    assert weblaunch.stop_run(weblaunch.WebRun(procs=[("前端", proc)])) == ["前端已自行退出"]
    assert killed == []


def test_wait_until_idle_waits_for_the_running_turn(monkeypatch):
    """网页上还有一轮在跑 → 等；只在开始等的时候说一句，别每 0.5 秒唠叨一次。"""
    from agent_harness import weblaunch

    seq = [1, 1, 0]  # 问三次：还在跑、还在跑、空了
    monkeypatch.setattr(weblaunch, "backend_busy", lambda port: seq.pop(0) if seq else 0)
    said, slept = [], []
    assert weblaunch.wait_until_idle(8000, 10, on_wait=said.append, sleep=slept.append) is True
    assert said == [1]
    assert slept == [0.5, 0.5]


def test_wait_until_idle_gives_up_after_timeout(monkeypatch):
    """等不是无限等——超时就走，否则一个卡住的后端能让 TUI 永远退不出去。"""
    from agent_harness import weblaunch

    monkeypatch.setattr(weblaunch, "backend_busy", lambda port: 1)  # 一直有人在跑
    clock = [0.0]
    monkeypatch.setattr(weblaunch.time, "monotonic", lambda: clock[0])

    def fake_sleep(sec):
        clock[0] += sec

    assert weblaunch.wait_until_idle(8000, 2, sleep=fake_sleep) is False
    assert clock[0] >= 2  # 真的等满了超时才走


def test_wait_until_idle_returns_at_once_when_nothing_to_wait_for(monkeypatch):
    """闲着 / 后端已经没了 → 立刻返回，不睡。"""
    from agent_harness import weblaunch

    monkeypatch.setattr(weblaunch, "backend_busy", lambda port: 0)
    assert weblaunch.wait_until_idle(8000, 10) is True
    monkeypatch.setattr(weblaunch, "backend_busy", lambda port: None)
    assert weblaunch.wait_until_idle(8000, 10) is True


def _write_state(tui_pid: int, procs: list[dict], age: float = 0.0) -> None:
    from agent_harness import weblaunch

    weblaunch._state_path().write_text(
        json.dumps({"tui": tui_pid, "at": time.time() - age, "procs": procs}), encoding="utf-8"
    )


def test_reap_orphans_takes_over_after_a_killed_tui(monkeypatch):
    """TUI 被强杀（finally 跑不到）→ 下次启动认领它的遗留进程，收掉再起新的。"""
    from agent_harness import weblaunch

    _write_state(111, [{"name": "后端", "pid": 222, "port": 8000}])
    monkeypatch.setattr(weblaunch, "_pid_alive", lambda pid: pid == 222)  # 111 没了，222 还在
    monkeypatch.setattr(weblaunch, "_port_open", lambda port: True)  # 端口还被它占着
    monkeypatch.setattr(weblaunch, "_health", lambda port, *a, **k: {"app": "agent-harness"})
    monkeypatch.setattr(weblaunch, "_wait_port_closed", lambda port, *a: True)
    killed = []
    monkeypatch.setattr(weblaunch, "_kill_tree", lambda pid: killed.append(pid) or True)

    assert weblaunch.reap_orphans(8000) == ["后端已结束（上次遗留 PID 222）"]
    assert killed == [222]
    assert not weblaunch._state_path().exists()  # 认领一次就够，别留着下次再认


def test_reap_orphans_leaves_a_live_tui_alone(monkeypatch):
    """那个 TUI 还开着 → 网页端归它管，不许替它收。"""
    from agent_harness import weblaunch

    _write_state(111, [{"name": "后端", "pid": 222, "port": 8000}])
    monkeypatch.setattr(weblaunch, "_pid_alive", lambda pid: True)  # 111 活着
    killed = []
    monkeypatch.setattr(weblaunch, "_kill_tree", lambda pid: killed.append(pid) or True)

    assert weblaunch.reap_orphans(8000) == []
    assert killed == []


def test_reap_orphans_refuses_to_guess(monkeypatch):
    """三个条件缺一不杀：pid 还在、端口还占着、对面还是我们的后端。

    全满足才动手——pid 会被系统复用，杀错一个用户进程比留个孤儿严重得多。
    """
    from agent_harness import weblaunch

    killed = []
    monkeypatch.setattr(weblaunch, "_kill_tree", lambda pid: killed.append(pid) or True)

    # ① 记下的 pid 早就自己退干净了（正常退出，没什么可收）
    _write_state(111, [{"name": "后端", "pid": 222, "port": 8000}])
    monkeypatch.setattr(weblaunch, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(weblaunch, "_port_open", lambda port: True)
    assert weblaunch.reap_orphans(8000) == []

    # ② 它已经不占端口了
    _write_state(111, [{"name": "后端", "pid": 222, "port": 8000}])
    monkeypatch.setattr(weblaunch, "_pid_alive", lambda pid: pid == 222)
    monkeypatch.setattr(weblaunch, "_port_open", lambda port: False)
    assert weblaunch.reap_orphans(8000) == []

    # ③ 端口有人听，但对面不是 agent-harness（pid 被别的程序复用了）
    _write_state(111, [{"name": "后端", "pid": 222, "port": 8000}])
    monkeypatch.setattr(weblaunch, "_port_open", lambda port: True)
    monkeypatch.setattr(weblaunch, "_health", lambda port, *a, **k: {"app": "nginx"})
    assert weblaunch.reap_orphans(8000) == []

    # ④ 记录是八天前的（早过期了，pid 早不知被谁复用）
    _write_state(111, [{"name": "后端", "pid": 222, "port": 8000}], age=8 * 24 * 3600)
    monkeypatch.setattr(weblaunch, "_health", lambda port, *a, **k: {"app": "agent-harness"})
    assert weblaunch.reap_orphans(8000) == []

    assert killed == []


def test_ensure_web_reaps_before_deciding(monkeypatch, tmp_path):
    """顺序：先收遗物，再看端口。

    反过来的话端口还被遗物占着，会被判成"已经在跑"→ 复用旧代码（正是 2026-10-24 那个坑）。
    """
    from agent_harness import weblaunch

    _write_state(111, [{"name": "后端", "pid": 222, "port": 8000}])
    monkeypatch.setattr(weblaunch, "_pid_alive", lambda pid: pid == 222)
    monkeypatch.setattr(weblaunch, "_health", lambda port, *a, **k: {"app": "agent-harness"})
    monkeypatch.setattr(weblaunch, "_wait_port_closed", lambda port, *a: True)
    order = []
    holds = {"orphan": True}  # 遗物还占着端口；收掉之后才空闲（真实世界里就是这个顺序）

    def fake_kill(pid):
        order.append(("kill", pid))
        holds["orphan"] = False
        return True

    monkeypatch.setattr(weblaunch, "_kill_tree", fake_kill)
    monkeypatch.setattr(
        weblaunch, "_port_open", lambda port: order.append(("probe", port)) or holds["orphan"]
    )
    monkeypatch.setattr(weblaunch, "dev_web_dir", lambda: None)
    monkeypatch.setattr(weblaunch, "bundled_web_dir", lambda: None)
    monkeypatch.setattr(weblaunch.subprocess, "Popen", lambda *a, **k: _FakeProc(5555))

    run = weblaunch.ensure_web(8000, tmp_path)
    assert run.reaped == ["后端已结束（上次遗留 PID 222）"]
    # 先探到「端口还被遗物占着」→ 收掉 → 之后的判断才看到空闲，于是决定起新的
    assert order.index(("kill", 222)) > 0
    assert run.started == ["后端"]  # 而不是"端口有人听，复用吧"（那正是 10-24 的坑）


def test_health_reports_the_protocol_weblaunch_expects():
    from agent_harness import server, weblaunch

    assert server.PROTOCOL == weblaunch.EXPECTED_PROTOCOL  # 两处常量不许漂
    body = TestClient(server.app).get("/health").json()
    # busy = 还有几轮在跑：TUI 退出前靠它决定「等还是直接关」
    assert body == {"app": "agent-harness", "protocol": server.PROTOCOL, "busy": 0}


def test_busy_counts_a_running_turn():
    """一轮跑着的时候 busy 要 > 0——TUI 就是看这个数字决定要不要等。"""
    from agent_harness import server

    server._turn_started()
    try:
        assert TestClient(server.app).get("/health").json()["busy"] == 1
    finally:
        server._turn_ended()
    assert TestClient(server.app).get("/health").json()["busy"] == 0


def test_busy_never_goes_negative():
    """多减一次不能变成 -1——那会让 TUI 以为「永远有人在跑」，退出时白等满超时。"""
    from agent_harness import server

    server._turn_ended()
    assert server.health()["busy"] == 0


def test_busy_is_one_while_a_web_turn_runs(monkeypatch, tmp_path):
    """真跑一轮（WS → 旁路线程）时 /health 说 busy=1，跑完回 0。

    TUI 退出前就是查这个数字：> 0 就先等，别把网页那半轮砍断。
    """
    gate = threading.Event()

    class SlowLLM(FakeLLM):
        def __call__(self, *a, **kw):
            gate.wait(5)  # 卡在模型这一步，模拟「还在写」
            return ("好了", [], "")

    monkeypatch.setattr(
        server, "load_settings_and_llm", lambda sp, m=None, **kw: (SlowLLM(), Settings())
    )
    monkeypatch.setattr(server, "save_messages", _no_save)
    monkeypatch.setattr(server, "trace_path", lambda w, s: tmp_path / f"{s}.trace.jsonl")
    c = TestClient(server.app)
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()  # session
        ws.receive_json()  # status
        ws.send_json({"type": "user", "text": "慢慢来"})
        for _ in range(100):  # 等它在旁路线程里真的跑起来
            if c.get("/health").json()["busy"] == 1:
                break
            time.sleep(0.02)
        assert c.get("/health").json()["busy"] == 1  # 这一轮在跑
        gate.set()
        while ws.receive_json()["type"] != "assistant_done":
            pass
    assert c.get("/health").json()["busy"] == 0  # 跑完了


def test_parse_netstat_picks_only_listening_rows():
    from agent_harness.weblaunch import _parse_netstat

    sample = """活动连接

  协议  本地地址          外部地址        状态           PID
  TCP    0.0.0.0:5173       0.0.0.0:0       LISTENING       25104
  TCP    127.0.0.1:8000     0.0.0.0:0       LISTENING       34724
  TCP    127.0.0.1:8000     127.0.0.1:58913 ESTABLISHED     34724
  TCP    [::]:8000          [::]:0          LISTENING       34724
"""
    assert _parse_netstat(sample, 8000) == [34724]  # 两条监听 + 一条连接，PID 只出现一次
    assert _parse_netstat(sample, 5173) == [25104]
    assert _parse_netstat(sample, 9999) == []


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


def test_resume_renders_tool_detail_and_body(monkeypatch):
    """刷新后恢复会话：工具行的参数（命令）和输出要拼回来，不能只剩名字。"""
    from agent_harness.messages import Msg

    monkeypatch.setattr(server, "load_settings_and_llm", lambda sp, m=None, **kw: (FakeLLM(), Settings()))
    monkeypatch.setattr(server, "load_session_model", lambda w, sid: None)
    monkeypatch.setattr(
        server,
        "load_session",
        lambda w, sid: [
            Msg(role="user", content="跑一下测试"),
            Msg(
                role="assistant",
                content="",
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "run_shell", "arguments": '{"command": "pytest -q"}'},
                }],
            ),
            Msg(role="tool", content="3 passed", tool_call_id="call_1", tool_name="run_shell"),
        ],
    )

    c = TestClient(server.app)
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()  # session
        ws.receive_json()  # status
        ws.send_json({"type": "resume", "id": "whatever"})
        assert ws.receive_json()["type"] == "session"  # resumed
        ev = ws.receive_json()  # history
        assert ev["type"] == "history"
        tool = next(m for m in ev["messages"] if m["kind"] == "tool")
        assert tool["name"] == "run_shell"
        assert tool["detail"] == "pytest -q"  # 参数从 assistant 的 tool_calls 里拼回来
        assert tool["body"] == "3 passed"  # 输出也回来了


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


# ---- 打包形态：装好的包没有开发树，界面由后端同一个端口托管（不用 Node / vite）----


def test_ensure_web_single_port_when_no_dev_tree(monkeypatch, tmp_path):
    """装好的包：没有 `web/` → 不起 vite，前端静态文件跟后端同端口。"""
    from agent_harness import weblaunch

    dist = tmp_path / "web_dist"
    dist.mkdir()
    (dist / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
    monkeypatch.setattr(weblaunch, "dev_web_dir", lambda: None)
    monkeypatch.setattr(weblaunch, "bundled_web_dir", lambda: dist)
    monkeypatch.setattr(weblaunch, "_port_open", lambda port: False)
    monkeypatch.setattr(weblaunch.shutil, "which", lambda name: "C:/fake/npm.cmd")  # 有 npm 也不许用
    spawned = []

    def fake_popen(argv, **k):
        spawned.append(argv)
        return _FakeProc(7000 + len(spawned))

    monkeypatch.setattr(weblaunch.subprocess, "Popen", fake_popen)

    run = weblaunch.ensure_web(8000, tmp_path)
    assert run.started == ["后端"]  # 没有"前端"
    assert run.ui_port == 8000  # 界面就走后端这个端口
    assert len(spawned) == 1 and "npm" not in " ".join(spawned[0])


def test_ensure_web_dev_tree_still_uses_vite(monkeypatch, tmp_path):
    """源码树照旧：vite 5173 热更新，别因为加单端口就把开发体验弄没了。"""
    from agent_harness import weblaunch

    dev = tmp_path / "web"
    dev.mkdir()
    (dev / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(weblaunch, "dev_web_dir", lambda: dev)
    monkeypatch.setattr(weblaunch, "bundled_web_dir", lambda: None)
    monkeypatch.setattr(weblaunch, "_port_open", lambda port: False)
    monkeypatch.setattr(weblaunch.shutil, "which", lambda name: "C:/fake/npm.cmd")
    spawned = []

    def fake_popen(argv, **k):
        spawned.append(argv)
        return _FakeProc(7100 + len(spawned))

    monkeypatch.setattr(weblaunch.subprocess, "Popen", fake_popen)

    run = weblaunch.ensure_web(8000, tmp_path)
    assert run.started == ["后端", "前端"]
    assert run.ui_port == weblaunch.FRONTEND_PORT
    assert "run" in spawned[1]


def test_serves_the_built_ui_on_the_same_port(monkeypatch, tmp_path):
    """有构建产物 → 后端把界面发出去：/ 是 index，深链回 index，/health 还是 JSON。"""
    from agent_harness import server

    dist = tmp_path / "web_dist"
    dist.mkdir()
    (dist / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
    monkeypatch.setattr(server, "WEB_DIST", dist)
    c = TestClient(server.app)
    assert b'id="root"' in c.get("/").content
    assert b'id="root"' in c.get("/deep/link").content  # 前端自己按路径渲染
    assert c.get("/health").json()["app"] == "agent-harness"  # 没被兜底吞掉
    assert c.get("/../pyproject.toml").status_code == 200  # 穿越不出去：回 index


def test_no_built_ui_is_a_clean_404(monkeypatch, tmp_path):
    """没打包（纯 API / 开发模式）→ 404，而不是 500 或者半个 index。"""
    from agent_harness import server

    monkeypatch.setattr(server, "WEB_DIST", tmp_path / "nope")
    assert TestClient(server.app).get("/").status_code == 404
