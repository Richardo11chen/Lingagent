"""审批的会话记忆：批准过的程序，这一轮聊完之前不再问。

为什么单独一个模块：终端和网页「怎么问」完全不同（按键 vs 卡片 + Event），但
「按什么粒度记、记完怎么判、什么算批准」必须一样。两边各写一遍的话，会出现
「终端第二次不问、网页还问」这种漂移，而 `agent_loop` 只认 True/False，看不见。
它只把 `ask` 注入进来——问答长什么样是界面的事，记忆和判定留在这里，只有一份。
"""

from typing import Callable

from .guardrail import command_head

# decision 的取值只有这几种：approve（这一次）/ always（这次并且本会话不再问）/
# auto（自动批准模式，人根本没见过）/ deny（驳回或超时）
#
# 写成白名单而不是「!= deny」：那是 fail-open——将来界面多返回一种结局（超时 / 出错），
# 谁忘了改这行，它就变成"批准"。现在认不出的值一律按驳回走；驳回只是命令不执行，
# 误伤看得见，静默放行看不见。
APPROVED = ("approve", "always", "auto")


def is_approved(decision: str | None) -> bool:
    """只有明确批准才算批——认不出的取值一律落在「不执行」这一侧。"""
    return decision in APPROVED


class SessionAllow:
    """把「问人」包成「问人 + 记住答案」，顺带管着自动批准这颗开关。

    ask(command, reason) -> decision     界面实现：怎么问、怎么等
    on_granted(head)                     刚记住某程序时的提示（可空）
    on_reused(head)                      命中记忆、直接放行时的提示（可空）
    on_auto(command, reason)             自动批准放行时的留痕（可空）

    三个提示都做成回调而不是内部打印：终端打一行、网页推一条 notice，只有界面知道
    该往哪写；但「什么时候提示」属于语义，留在这里。

    自动批准（auto）也住在这里而不是各入口一个全局：它和「本会话不再问某程序」是同一类
    状态——都是"这一轮聊完之前，哪些命令不用再征求同意"。放在界面上就成了"一个进程一份"，
    和网页那边"每个连接一份"对不上。
    """

    def __init__(
        self,
        ask: Callable[[str, str], str],
        on_granted: Callable[[str], None] | None = None,
        on_reused: Callable[[str], None] | None = None,
        on_auto: Callable[[str, str], None] | None = None,
    ) -> None:
        self._ask = ask
        self._on_granted = on_granted
        self._on_reused = on_reused
        self._on_auto = on_auto
        self.granted: set[str] = set()  # 程序名（command_head），内存态，退出即忘
        self.auto = False  # 自动批准：不弹卡、直接放行，但每次都要留痕

    def toggle_auto(self) -> bool:
        """切换自动批准（TUI 的 Shift+Tab），返回切换后的状态。"""
        self.auto = not self.auto
        return self.auto

    def __call__(self, command: str, reason: str) -> bool:
        """给 agent_loop 用的 approver：True = 放行。"""
        head = command_head(command)
        if head and head in self.granted:
            if self._on_reused is not None:
                self._on_reused(head)
            return True
        if self.auto:
            if self._on_auto is not None:
                self._on_auto(command, reason)
            return True
        decision = self._ask(command, reason)
        if decision == "always" and head:
            self.granted.add(head)
            if self._on_granted is not None:
                self._on_granted(head)
        return is_approved(decision)
