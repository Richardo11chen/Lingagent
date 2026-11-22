"""工作区（项目根）——一个显式的值，而不是"到处 Path.cwd()"。

之所以要有这个类型：`Path.cwd()` 是**进程的**状态，不是**任务的**状态。谁在任何时候调它，
拿到的都是"启动后有没有被别人 cd 过"的副产品。三个地方各取一次时，未来说要支持
"同时开两个目录"或者"远程工作区"，就得把每一处都找出来改——而漏一处不会有任何报错，
只会默默读写错地方。

现在的规矩：
- 进程启动时在装配层构造一次（`assembly.WORKSPACE`）
- 其余模块只接受传进来的 `root: Path`，自己不许调 `Path.cwd()`
- 测试可以造一个指向临时目录的 `Workspace`，不必 chdir
"""

from dataclasses import dataclass
from pathlib import Path

# 工具输出溢出时落盘的位置（相对项目根，已 gitignore）
DUMP_DIR = ".agent-harness-out"


@dataclass(frozen=True)
class Workspace:
    """项目根。冻结的：跑一轮任务期间它不该变，变了就是另一个工作区。"""

    root: Path

    @property
    def dump_dir(self) -> Path:
        """工具输出全文的落脚点——由工作区决定，不由"进程恰好在哪"决定。"""
        return self.root / DUMP_DIR

    def relative(self, path: Path) -> str:
        """转成给人看的相对路径；不在工作区内就原样返回绝对路径。"""
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)
