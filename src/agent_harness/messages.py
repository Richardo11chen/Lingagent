"""核心数据结构：历史消息 Msg。

独立模块的理由（依赖单向）：Msg 被 llm / compact / session / loop 多方依赖——
放在 llm.py 会形成「compact 依赖 llm、llm 依赖 compact」的环（2026-10-09 撞过）。
本模块谁都不依赖，谁都可以依赖它。
"""

from dataclasses import dataclass


@dataclass
class Msg:
    """一条历史消息。role: user / assistant / tool。tool 消息必须带 tool_call_id。"""

    role: str
    content: str = ""
    tool_call_id: str | None = None  # tool 消息：关联到那次模型调用
    tool_calls: list | None = None  # assistant 消息：模型发出的原始 tool_calls（重发时原样带回）
    tool_name: str | None = None  # tool 消息：哪个工具的结果（压缩判定用）
