"""策略挂载点（S9·装插座）：PreToolUse / PostToolUse 两个事件点 + 函数名单。

- 预钩子：`fn(action) -> Verdict | None`——None = 放行（没意见）；`deny(reason)` = 拦
- 后钩子：`fn(action, result) -> None`——只观察（v1 不能改结果）
- 内置护栏 + 审批**不在此**——它们在名单之前先跑（基线不可摘，S9 决策 3）
- 失败语义：预钩子抛异常 = 默认拦截（fail closed，S9 决策 2）；后钩子抛异常吞掉（观察类不拖垮主流程）
"""

import re
from dataclasses import dataclass

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


def run_post(hooks: list, action: Action, result: str) -> str:
    """跑后钩子，返回（可能被改写的）结果。

    v1 是"只观察"：看到密钥也只能干看着。但工具输出会同时进 trace（不截断的审计账）、
    界面、以及回灌给模型的上下文——三处都会把密钥留下来。所以 v2 允许钩子返回替换值：
    返回 None = 没意见；返回 str = 用它替换（后面的钩子看到的是替换后的）。

    异常仍然吞掉：观察类策略不许拖垮主流程。
    """
    for h in hooks:
        try:
            out = h(action, result)
        except Exception:
            continue
        if isinstance(out, str):
            result = out
    return result


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


# 密钥形状：只认**高置信度前缀 + 足够长度**。宁可漏一个，也不要把正常文本改成星号——
# 误伤一次，用户就再也不敢信这个打码了。
_SECRET_SHAPES = (
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "sk-***"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "gh*_***"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "xox*-***"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA***"),
)


def redact_secrets(action: Action, result: str) -> str | None:
    """工具输出里的密钥打码，再往下走（trace / 界面 / 回灌模型都用这份）。

    真实的泄漏路径不是"模型故意偷 key"，是它随手跑了个 `env` 或 `cat ~/.npmrc`：
    整份环境变量就同时进了上下文和 trace——trace 还是不截断的审计账，
    等于把密钥永久留在磁盘上。打码一次，三处都干净了。
    """
    out = result
    for pattern, repl in _SECRET_SHAPES:
        out = pattern.sub(repl, out)
    return out if out != result else None
