from pathlib import Path
from dataclasses import dataclass
import os
import subprocess

SHELL_TIMEOUT = 120  # 秒：一条 shell 命令的硬上限（S10 真机撞出的墙——卡住的命令不许冻住整台机器）

# 回灌给模型的结果上限（字符）。trace 存全文（审计账，人看），history / session 走截断版
# （模型看）——三份拷贝各按各的读者定规格，别让一次命令把上下文和存档撑爆。
MAX_RESULT_CHARS = 32_000
_HEAD_CHARS = 24_000  # 截断时保留头尾：开头是定位信息，结尾是报错摘要
_TAIL_CHARS = 8_000


def cap_result(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    """把工具结果压到上限内（头 + 尾 + 省略说明）——只用于进 history 的那一份。

    为什么保留尾部：命令失败的原因、traceback 结尾、pytest 总结都在最后几行，
    只留开头会让模型看不到红在哪。
    """
    if len(text) <= limit:
        return text
    omitted = len(text) - _HEAD_CHARS - _TAIL_CHARS
    return (
        f"{text[:_HEAD_CHARS]}\n\n"
        f"[... 已省略中间 {omitted} 字符（原始输出共 {len(text)} 字符）。"
        f"完整输出见 trace；需要细节请用带范围的命令重新取（如 head / tail / findstr 过滤）...]\n\n"
        f"{text[-_TAIL_CHARS:]}"
    )


def run_shell(command: str, timeout: float = SHELL_TIMEOUT) -> str:
    proc = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=True
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            # Windows：shell=True 时 kill 只杀 cmd.exe，孙进程还攥着管道——communicate 会等到它跑完。
            # taskkill /T 杀整棵树，管道才会关。
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, text=True,
            )
        else:
            proc.kill()
        try:
            proc.communicate(timeout=2)  # 收尸；极端情况下不再等（管道泄漏好过冻住）
        except subprocess.TimeoutExpired:
            pass
        return f"命令超时（>{timeout:g} 秒）已被终止：{command}"
    return f"退出码 {proc.returncode}\n{stdout}{stderr}"

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

