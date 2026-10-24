"""上下文压缩（Compress 策略）：投影式微压缩——不改 history，只压发给 API 的副本。

设计定案（2026-09-22）：白名单判定 / 保留最近 N / 占位替换（结构不动·配对不破）/
软估阈值触发。reactive 兜底在适配器层（后续接）。
"""

from dataclasses import replace

from .messages import Msg

# 大输出类工具——其 tool 结果可清理（write/edit/count 结果短，不清）
COMPACTABLE = {"read_file", "run_shell", "run_tests"}

KEEP_LAST = 5

# 软阈值（字符数当量，约等于 token 数）——与 settings.Settings.compact_threshold 保持一致
DEFAULT_THRESHOLD = 300_000


def estimate_tokens(messages: list[Msg]) -> int:
    """粗估 token 数：字符数当量（中文≈1:1，英文保守高估）。不做精确 tokenizer。"""
    total = 0
    for m in messages:
        total += len(m.content or "")
        if m.tool_calls:
            total += len(str(m.tool_calls))  # 结构本身也占量；str 兜底 pydantic 对象
    return total


def microcompact(messages: list[Msg], keep_last: int = KEEP_LAST) -> list[Msg]:
    """投影式微压缩：最近 keep_last 条白名单工具结果保留，更早的 content 换占位。

    role / tool_call_id / tool_calls 一字不动——配对不破（400 防线）。
    返回新列表且不清的对象复用引用——原 messages 完全不受影响。
    """
    # 从后往前数白名单 tool 消息，超出的下标标记为「要清」
    targets: set[int] = set()
    seen = 0
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.role == "tool" and m.tool_name in COMPACTABLE:
            seen += 1
            if seen > keep_last:
                targets.add(i)

    if not targets:
        return list(messages)  # 无需清理——返回浅壳（调用方照常使用）

    out: list[Msg] = []
    for i, m in enumerate(messages):
        if i in targets:
            # dataclasses.replace 产新对象——不动原 Msg（真投影）
            out.append(
                replace(m, content=f"[已清理：{m.tool_name} 的旧输出，{len(m.content or '')} 字符]")
            )
        else:
            out.append(m)
    return out


def microcompact_if_needed(
    messages: list[Msg], threshold: int = DEFAULT_THRESHOLD, keep_last: int = KEEP_LAST
) -> list[Msg]:
    """超阈值才压（软估）——否则原样返回。"""
    if estimate_tokens(messages) <= threshold:
        return messages
    return microcompact(messages, keep_last)


def snip(messages: list[Msg], keep_groups: int = 3) -> list[Msg]:
    """激进档（reactive 用）：保首条 user + 最近 keep_groups 组，中间整段省略。

    按「组」切（assistant[tool_calls] + 紧随 tool 消息 = 原子单位）——配对不破。
    省略标记并入首条 user 的 content——不新增消息（避免连续同 role 的 provider 风险）。
    """
    # 划分组：带 tool_calls 的 assistant + 紧随的 tool 消息 = 一组；其余各自一组
    groups: list[list[int]] = []
    i = 0
    while i < len(messages):
        idxs = [i]
        if messages[i].role == "assistant" and messages[i].tool_calls:
            j = i + 1
            while j < len(messages) and messages[j].role == "tool":
                idxs.append(j)
                j += 1
            i = j
        else:
            i += 1
        groups.append(idxs)

    keep: set[int] = set(groups[0])  # 首组（含任务）
    if keep_groups > 0:
        for g in groups[-keep_groups:]:
            keep.update(g)

    kept = [m for idx, m in enumerate(messages) if idx in keep]
    omitted = len(messages) - len(kept)
    if omitted == 0:
        return list(messages)

    out: list[Msg] = []
    marked = False
    for m in kept:
        if not marked and m.role == "user":
            out.append(
                replace(
                    m,
                    content=f"{m.content}\n\n[注：为压缩请求体积，已省略中间 {omitted} 条历史消息]",
                )
            )
            marked = True
        else:
            out.append(m)
    return out
