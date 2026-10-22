"""后台任务登记簿：谁在跑、输出写到哪。

只记账，不启动进程也不杀进程——启动要用 shell 探测、停止要杀整棵进程树，这两样都在
`tools.py` 里（搬过来只会多一次互相 import）。这里保持成纯数据结构，好测。

为什么需要这一层：dev server / watch / 长构建**没有「跑完」这个状态**。前台等它们只有
两种结局——超时被杀，或者把会话堵死十分钟。主流 harness（Claude Code 的
`run_in_background`）的做法是启动就返回任务 id，之后按需读输出、按需停掉。

三条刻意的边界：

1. **输出写文件，不进内存**：一个 vite server 能连着刷几小时，攒在字符串里会吃光内存。
2. **任务只活在内存里**：重启就没了，和「本会话记住的批准」同一个尺度——重启即遗忘。
3. **输出文件留 7 天**：临时日志不该攒成垃圾场，但也不该刚启动就删（人可能还想看）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .paths import APP_DIR

TASKS_DIR = APP_DIR / "tasks"
KEEP_DAYS = 7  # 输出文件留这么久；启动新任务时顺手清理，不另起定时器


@dataclass
class Task:
    """一个后台任务。`proc` 故意用 object 类型——这里不关心它是 subprocess 还是别的。"""

    id: str
    command: str
    proc: object
    path: Path
    started: float = field(default_factory=time.time)

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self.proc.poll()

    @property
    def elapsed(self) -> float:
        return time.time() - self.started


_registry: dict[str, Task] = {}
_counter = 0


def next_id() -> str:
    """t1 / t2 / …：短、能在对话里直接引用，比 uuid 好念。"""
    global _counter
    _counter += 1
    return f"t{_counter}"


def output_path(task_id: str) -> Path:
    """输出文件路径：时间戳在前，按名字排序就是时间序。"""
    _prune()
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    return TASKS_DIR / f"{int(time.time())}-{task_id}.log"


def register(task_id: str, command: str, proc, path: Path) -> Task:
    t = Task(id=task_id, command=command, proc=proc, path=path)
    _registry[task_id] = t
    return t


def get(task_id: str) -> Task | None:
    return _registry.get(task_id)


def all_tasks() -> list[Task]:
    return list(_registry.values())


def running() -> list[Task]:
    return [t for t in _registry.values() if t.alive]


def _prune() -> None:
    """删掉 KEEP_DAYS 天前的输出文件——失败就算了，清垃圾不该打断开工。"""
    cutoff = time.time() - KEEP_DAYS * 86400
    try:
        for p in TASKS_DIR.glob("*.log"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
    except OSError:
        pass
