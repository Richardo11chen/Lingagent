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


# 单条消息的上限：用户粘的日志、模型写的大段正文都可能比整个工具预算还大。
# 50k 字符 ≈ 一整份中等长度的源码——再长的东西，人也不会指望模型逐字读完。
MAX_MESSAGE_CHARS = 50_000
_HEAD = 30_000
_TAIL = 10_000


def cap_long_messages(
    messages: list[Msg], limit: int = MAX_MESSAGE_CHARS
) -> tuple[list[Msg], int]:
    """把超长消息留头留尾，中间标出省略了多少——返回 (新列表, 被截条数)。

    为什么在**发送前**做，而不是进 history 时：用户粘进来的是他手上的证据，删掉就
    真没了。投影式处理（不动原 Msg）和 `microcompact` 同一套语义，翻历史仍看得到全文。

    tool 消息跳过：它们已经被 `cap_result` 限到 32k，再截一遍没有意义。
    只截正文，不碰 `tool_calls`——那玩意儿一改配对就断，换来的是 400。
    """
    out: list[Msg] = []
    capped = 0
    for m in messages:
        text = m.content or ""
        if m.role == "tool" or len(text) <= limit:
            out.append(m)
            continue
        kept = (
            text[:_HEAD]
            + f"\n…[中间省略 {len(text) - _HEAD - _TAIL} 字符，原文共 {len(text)}]…\n"
            + text[-_TAIL:]
        )
        out.append(replace(m, content=kept))
        capped += 1
    return out, capped


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
