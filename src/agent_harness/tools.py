from pathlib import Path
from dataclasses import dataclass
import subprocess

def run_shell(command: str) -> str:
    result = subprocess.run(command, capture_output=True, text=True, shell=True)
    return f"退出码 {result.returncode}\n{result.stdout}{result.stderr}"

def read_file(path: str) -> str:
    """读取文件内容"""
    return Path(path).read_text(encoding="utf-8")

def write_file(path: str, content: str):
    """写入文件内容"""
    Path(path).write_text(content, encoding="utf-8")
    return f'写入文件 {path} {len(content)} 字符。'

def edit_file(path: str, old: str, new: str) -> str:
    """精确替换文件里的一段文本（保留其余内容）。

    old 必须在文件中唯一出现——0 次（不匹配）或多次（歧义）都拒绝并回灌原因。
    """
    p = Path(path)
    content = p.read_text(encoding="utf-8")
    count = content.count(old)
    if count == 0:
        return "编辑失败：未找到要替换的文本（old 与文件当前内容不匹配，请先读文件确认）。"
    if count > 1:
        return f"编辑失败：要替换的文本出现 {count} 次（不唯一），请给出更多上下文使其唯一。"
    p.write_text(content.replace(old, new), encoding="utf-8")
    return f"已编辑 {path}（替换 {len(old)} 字符 → {len(new)} 字符）。"

def count_lines(path: str) -> str:
    """统计文件行数"""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return f"共 {len(lines)} 行"

