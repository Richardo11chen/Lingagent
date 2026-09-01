"""模型抽象层：主循环只认这条接口，不认背后是谁（真模型 / 假模型）。"""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Action:
    """模型发出的结构化指令。"""

    type: str  # "call_tool" | "done"
    tool: str = ""  # call_tool 时用：工具名
    args: dict = field(default_factory=dict)  # call_tool 时用：参数
    changed_code: bool = False  # 模型声明「我改了代码」→ 主循环触发校验


class LLM(Protocol):
    """模型接口：输入 context + 菜单，输出 (text, action)。"""

    def __call__(self, context: str, menu: list[str]) -> tuple[str, Action]: ...


class LLM:
    """假模型：S1 固定剧本；S2 升级——看报告分叉。

    三态决策（都从 context 里读信号）：
    - context 里有失败报告 → 弹修复剧本（红了再改）
    - context 里有通过报告 → done（绿了停）
    - 没报告 → 弹主剧本（第一次干活）
    """

    def __init__(self, script: list[Action], fix_script: list[Action] | None = None):
        self._script = list(script)          # 主剧本：第一次干活
        self._fix_script = list(fix_script or [])  # 修复剧本：红了再改

    def __call__(self, context: str, menu: list[str]) -> tuple[str, Action]:
        last = context.rsplit("[user]", 1)[-1]
        if "测试失败" in last:
            if self._fix_script:
                return ("（假模型）测试失败，继续修改。", self._fix_script.pop(0))
            return ("（假模型）多次修复未果，停止。", Action(type="done"))
        if "测试全部通过" in last:
            return ("测试通过，任务完成。", Action(type="done"))
        if self._script:
            action = self._script.pop(0)
            return (f"（假模型）下一步：{action.type} {action.tool}", action)
        return ("任务完成。", Action(type="done"))
