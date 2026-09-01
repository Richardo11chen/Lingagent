"""反馈校验器：跑测试 → 判红绿 → 生成报告（反馈必须是代码机制，规格 A.4(B)）。"""

import subprocess

def run_sensors(test_command: list[str]) -> str:
    """跑测试命令，解析输出判红绿，生成报告字符串（回灌给模型）。"""
    # 1. subprocess.run(test_command, capture_output=True, text=True)  # 参数列表，不 shell
    # 2. returncode == 0 → 返回 "✅ 测试全部通过。"
    # 3. 否则提取 FAILED / "E " 开头行（最多 10 行），拼报告
    result = subprocess.run(test_command, capture_output=True, text=True)
    if result.returncode == 0:
        return "测试全部通过。"
    else:
        lines = (result.stdout + result.stderr).splitlines()
        if len(lines) <= 20:
            tail = "\n".join(lines)          # 短输出：全部给
        else:
            tail = "\n".join(lines[-20:])    # 长输出：尾部 20 行
        return f"测试失败，退出码 {result.returncode}，输出尾部 20 行：\n{tail}"