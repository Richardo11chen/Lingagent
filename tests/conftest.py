"""全局测试夹具：别让测试碰到真实的家目录。"""

import pytest


@pytest.fixture(autouse=True)
def _no_real_state_file(tmp_path, monkeypatch):
    """`weblaunch` 的启动记录与单实例锁默认落在 `~/.agent-harness/`。

    测试里一律改到临时目录：否则每跑一次测试就在用户机器上留一份指向假 pid 的记录
    （下一次真 TUI 启动会拿它去"认领"），而且锁会一直挡着真的实例。
    """
    from agent_harness import weblaunch

    monkeypatch.setattr(weblaunch, "_state_path", lambda: tmp_path / "web.json")
    monkeypatch.setattr(weblaunch, "_lock_path", lambda: tmp_path / "web.lock")
