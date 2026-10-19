"""工具层单测：run_shell 的超时兜底（S10 真机撞出的墙——卡住的命令不许冻住整台机器）。"""

import time

from agent_harness.tools import run_shell


def test_run_shell_timeout_kills_hang():
    t0 = time.perf_counter()
    out = run_shell("ping -n 6 127.0.0.1", timeout=0.3)  # 正常要 ~5 秒
    assert "超时" in out
    assert time.perf_counter() - t0 < 4  # 没等它自己跑完
