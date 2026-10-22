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
        f"需要细节请用带范围的命令重新取（如 head / tail / findstr 过滤）...]\n\n"
        f"{text[-_TAIL_CHARS:]}"
    )


def run_shell(command: str, timeout: float = SHELL_TIMEOUT) -> str:
    """跑一条 shell 命令——超时杀整棵进程树，输出合并成一个流。

    三个刻意的选择：

    1. **stderr 并入 stdout**：模型关心"发生了什么、按什么顺序"，不关心走哪个通道。
       分开收集再拼接会把时序打乱（编译错误的上下文就错位了）。
    2. **stdin 接空设备**：等 stdin 的命令（`git commit` 不带 `-m`、`more`）不该
       白挂到超时才结束，直接让它们读到 EOF 自己退出。
    3. **抓字节自己解码**，不用 `text=True`：父进程按 locale 解、子进程按控制台代码页写，
       两者可以不一致（`PYTHONUTF8=1` + `chcp 936` 就是典型），一旦不一致就是乱码或
       `UnicodeDecodeError`。
    """
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        shell=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        return f"退出码 {proc.returncode}\n{_decode(out)}"
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
        tail = b""
        try:
            tail, _ = proc.communicate(timeout=2)  # 收尸，并拿到超时前已产生的输出
        except subprocess.TimeoutExpired:
            pass  # 极端情况下不再等（管道泄漏好过冻住）
        msg = f"命令超时（>{timeout:g} 秒）已被终止：{command}"
        partial = _decode(tail)
        if partial.strip():  # 超时前的输出有诊断价值，别丢
            msg += f"\n（下面是超时前已产生的输出，可能不完整）\n{partial}"
        return msg

_ENCODINGS = ("utf-8", "utf-8-sig", "gbk")  # Windows 项目里 GBK 文件不罕见


def _decode(raw: bytes) -> str:
    """字节 → 文本：utf-8 → utf-8-sig → gbk，都不行就宽松解码（坏字符变 ?）。

    文件读取和 shell 输出共用这一条——它们踩的是同一个坑（写入方按自己的编码写，
    读取方按自己的编码解）。
    """
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_text(p: Path) -> str:
    """按常见编码依次尝试；都失败了宽松解码（坏字符变 ?，至少能读能改）。

    死认 utf-8 的代价很实在：碰到 GBK 文件工具直接抛异常，而模型改不了文件编码，
    只能绕开——等于这个文件对它不可读。
    """
    return _decode(p.read_bytes())


def read_file(path: str) -> str:
    """读取文件内容"""
    return _read_text(Path(path))

def write_file(path: str, content: str):
    """写入文件内容"""
    Path(path).write_text(content, encoding="utf-8")
    return f'写入文件 {path} {len(content)} 字符。'

def edit_file(path: str, old: str, new: str) -> str:
    """精确替换文件里的一段文本（保留其余内容）。

    old 必须在文件中唯一出现——0 次（不匹配）或多次（歧义）都拒绝并回灌原因。
    """
    p = Path(path)
    content = _read_text(p)
    count = content.count(old)
    if count == 0:
        return "编辑失败：未找到要替换的文本（old 与文件当前内容不匹配，请先读文件确认）。"
    if count > 1:
        return f"编辑失败：要替换的文本出现 {count} 次（不唯一），请给出更多上下文使其唯一。"
    p.write_text(content.replace(old, new), encoding="utf-8")
    return f"已编辑 {path}（替换 {len(old)} 字符 → {len(new)} 字符）。"

