"""反馈校验器：跑测试 → 判红绿 → 生成报告（反馈必须是代码机制，规格 A.4(B)）。"""

import subprocess


def run_tests(path: str = ".") -> str:
    """工具化的判定：跑 pytest 拿红绿报告（触发自觉、判定代码化——决策 B）。"""
    return run_sensors(["pytest", "-q", path])


def _section(lines: list[str], marker: str) -> list[str]:
    """按 ===== 标题行分段：取标题含 marker 那段的正文（标题行不含）。

    pytest 输出结构：===== 标题 ===== 行分隔各段。找不到 marker 返回空。
    """
    starts = [i for i, line in enumerate(lines) if line.startswith("=")]
    for k, i in enumerate(starts):
        if marker in lines[i]:
            end = starts[k + 1] if k + 1 < len(starts) else len(lines)
            return lines[i + 1 : end]
    return []


def run_sensors(test_command: list[str]) -> str:
    """跑命令判红绿，生成报告字符串（回灌给模型）。

    绿：简短通过语。红：结构化提取——失败清单（全局）+ 失败详情（为什么），
    丢弃头部噪声；解析不到段（非 pytest 输出）时兜底截尾 20 行。
    """
    result = subprocess.run(test_command, capture_output=True, text=True)
    if result.returncode == 0:
        return "测试全部通过。"

    lines = (result.stdout + result.stderr).splitlines()
    failures = _section(lines, "FAILURES")
    summary = _section(lines, "short test summary info")

    parts: list[str] = []
    if summary:
        parts.append("失败清单：\n" + "\n".join(summary[:15]))  # 一行一个失败，超 15 个截前
    if failures:
        parts.append("失败详情（关键在前）：\n" + "\n".join(failures[:25]))  # 详情块开头是定位/断言

    if parts:
        body = "\n\n".join(parts)
    else:
        body = "\n".join(lines[-20:])  # 兜底：非 pytest 格式，截尾

    return f"测试失败（退出码 {result.returncode}）：\n{body}"
