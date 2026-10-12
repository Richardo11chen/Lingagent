"""策略挂载点（S9·装插座）：PreToolUse / PostToolUse 两个事件点 + 函数名单。

- 预钩子：`fn(action) -> Verdict | None`——None = 放行（没意见）；`deny(reason)` = 拦
- 后钩子：`fn(action, result) -> None`——只观察（v1 不能改结果）
- 内置护栏 + 审批**不在此**——它们在名单之前先跑（基线不可摘，S9 决策 3）
- 失败语义：预钩子抛异常 = 默认拦截（fail closed，S9 决策 2）；后钩子抛异常吞掉（观察类不拖垮主流程）
"""

from dataclasses import dataclass
from typing import Callable

from .llm import Action


@dataclass
class Verdict:
    """预钩子的判定——deny 时 reason 原样回灌给模型（让它知道为什么、怎么换路）。"""

    decision: str = "allow"  # "allow" / "deny"
    reason: str = ""


def deny(reason: str = "") -> Verdict:
    return Verdict(decision="deny", reason=reason)


@dataclass
class PreResult:
    """跑完 Pre 名单的汇总（loop 用：拦没拦、谁拦的、为什么）。"""

    denied: bool = False
    by: str = ""
    reason: str = ""


def run_pre(hooks: list, action: Action) -> PreResult:
    """按注册序跑预钩子：首个 deny 短路；抛异常 = 默认拦截（决策 2）。"""
    for h in hooks:
        name = getattr(h, "__name__", repr(h))
        try:
            v = h(action)
        except Exception as e:  # fail closed：策略坏了，宁可停
            return PreResult(True, name, f"策略异常，按默认拦截：{type(e).__name__}: {e}")
        if v is not None and v.decision == "deny":
            return PreResult(True, name, v.reason)
    return PreResult()


def run_post(hooks: list, action: Action, result: str) -> None:
    """跑后钩子（观察类）：异常吞掉——不许拖垮主流程。"""
    for h in hooks:
        try:
            h(action, result)
        except Exception:
            pass


# ---- 随附策略（装配层注册进名单）——既是机制演示，也是真护栏 ----

ENV_NAMES_PREFIX = ".env"


def forbid_env_writes(action: Action) -> Verdict | None:
    """敏感文件（.env 家族）不许经工具改——需要改动让用户手动来。"""
    if action.tool not in ("write_file", "edit_file"):
        return None
    name = str(action.args.get("path", "")).replace("\\", "/").rsplit("/", 1)[-1]
    if name == ENV_NAMES_PREFIX or name.startswith(ENV_NAMES_PREFIX + "."):
        return deny(f"敏感文件 {name} 不允许经工具改动——请让用户手动处理")
    return None
