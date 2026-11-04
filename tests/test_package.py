"""包根的形状：`import agent_harness` 不许把运行时依赖拖进来。

为什么值得单测：这是唯一一条「改回去也没人发现」的约束——在 `__init__.py` 里加一行
`from . import tui`，所有功能测试照样绿，只有把包当库用、或者在意启动时间的人会发现
代价。所以必须由一条会红的断言守着，而不是靠注释提醒。

（在子进程里跑：pytest 本身已经加载过一堆东西，同进程查 sys.modules 只会得到假阴性。）
"""

import subprocess
import sys


def _run(code: str) -> str:
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_package_root_does_not_load_the_ui_stack():
    """只 import 包根：终端界面那一套（prompt_toolkit / rich）一个都不该被拉进来。"""
    out = _run(
        "import sys, agent_harness\n"
        "print('prompt_toolkit' in sys.modules, 'rich' in sys.modules)\n"
    )
    assert out == "False False"


def test_cli_is_reachable_from_its_own_module():
    """应用主体在 `agent_harness.cli`——入口点（pyproject 的 lingan）指着这里。"""
    out = _run("import agent_harness.cli as c\nprint(callable(c.main))\n")
    assert out == "True"
