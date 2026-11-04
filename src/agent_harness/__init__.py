"""Agent Harness — 用代码造一台会自己干活的 agent 机器。

包根刻意保持为空：这里**不导入**任何运行时模块。

为什么：这个包既是命令行程序，也被人当库用（`from agent_harness.llm import Msg`
这种）。Python 会把 `import agent_harness.llm` 变成「先执行本文件」——只要这里写了
`from . import tui`，任何一次取用都会顺带加载 prompt_toolkit / rich / subprocess，
启动慢、依赖重，底层模块回头拿包根属性时（`tools.py` 的 `from . import tasks`）
还会把整台终端拖进来。

所以：
- 应用主体在 `agent_harness.cli`（入口点 `lingan` 指向那里）；
- 各模块自己就是公共接口，按需 import（`agent_harness.loop`、`agent_harness.llm` …）；
- 版本号只有一处真相：`pyproject.toml` 的 `[project].version`，这里不再抄一份。

这条约束由 `tests/test_package.py` 守着——它会在干净的子进程里断言
`import agent_harness` 之后 `prompt_toolkit` 仍未加载。
"""
