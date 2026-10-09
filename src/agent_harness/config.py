"""配置维：规则文件自动加载——声明式约束的机制化（不靠模型自觉读）。"""
from pathlib import Path

RULE_FILES = ("AGENTS.md",)    

def load_rules(workspace: Path) -> str:
    # 逐个读存在的规则文件 → 拼成一段文本；都没有 → 返回空串
    parts: list[str] = []                          # ① 收集容器
    for name in RULE_FILES:                        # ② 逐个文件名
        p = workspace / name                       # ③ Path 的 / 拼路径
        if p.exists():                             # ④ 存在才读
            text = p.read_text(encoding="utf-8")
            parts.append(f"# {name}\n\n{text}")    # ⑤ 带来源标题
    return "\n\n".join(parts)                      # ⑥ 一次 join