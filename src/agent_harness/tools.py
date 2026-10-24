from pathlib import Path
from contextvars import ContextVar
from dataclasses import dataclass
import atexit
import difflib
import os
import subprocess
import time

from .shell import detect as _shell

SHELL_TIMEOUT = 120  # 秒：一条 shell 命令的默认上限（S10 真机撞出的墙——卡住的命令不许冻住整台机器）
MAX_SHELL_TIMEOUT = 600  # 秒：模型自己能调到的上限（装包 / 全量构建偶尔要多几分钟）

# 回灌给模型的结果上限（字符）。trace 存全文（审计账，人看），history / session 走截断版
# （模型看）——三份拷贝各按各的读者定规格，别让一次命令把上下文和存档撑爆。
MAX_RESULT_CHARS = 32_000
_HEAD_CHARS = 24_000  # 截断时保留头尾：开头是定位信息，结尾是报错摘要
_TAIL_CHARS = 8_000

# 超限结果的全文落这里（工作目录内，已 gitignore）。为什么不放 ~/.agent-harness：
# 模型手里的 read_file 只认项目内路径——路径围栏是这套东西里唯一的真边界，
# 不为了省一次命令去放宽它。dump 留 7 天，和后台任务日志一个规矩。
DUMP_DIR = ".agent-harness-out"
DUMP_KEEP_DAYS = 7


def cap_result(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    """把工具结果压到上限内（头 + 尾 + 省略说明）——只用于进 history 的那一份。

    为什么保留尾部：命令失败的原因、traceback 结尾、pytest 总结都在最后几行，
    只留开头会让模型看不到红在哪。

    中间那段不是丢掉而是**落盘并给出路径**：原来只写"请用带范围的命令重新取"，
    等于让模型把最贵的那条命令（全量 grep / 整份构建日志）再跑一遍；有了路径，
    它第一次就能按需读那一段。
    """
    if len(text) <= limit:
        return text
    omitted = len(text) - _HEAD_CHARS - _TAIL_CHARS
    where = _dump_full(text)
    if where:
        hint = f"全文在 {where}（相对项目根）——用 read_file 或 `sed -n '1,200p' {where}` 分段看"
    else:
        hint = "需要细节请用带范围的命令重新取（如 head / tail / grep 过滤）"
    return (
        f"{text[:_HEAD_CHARS]}\n\n"
        f"[... 已省略中间 {omitted} 字符（原始输出共 {len(text)} 字符）。{hint}]\n\n"
        f"{text[-_TAIL_CHARS:]}"
    )


def _dump_full(text: str) -> str:
    """把全文写到工作目录的 `.agent-harness-out/`，返回相对路径；失败返回 ""。

    落盘只是便利，不该让一次工具调用失败——写不进去就退回"重新取"那句提示。
    """
    try:
        root = Path.cwd()
        d = root / DUMP_DIR
        d.mkdir(exist_ok=True)
        _prune_dumps(d)
        p = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}.txt"
        p.write_text(text, encoding="utf-8")
        return p.relative_to(root).as_posix()
    except OSError:
        return ""


def _prune_dumps(d: Path) -> None:
    """顺手清掉过期 dump——临时文件不该攒成垃圾场。"""
    cutoff = time.time() - DUMP_KEEP_DAYS * 86400
    for p in d.glob("*.txt"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            continue


def _clamp_timeout(value) -> float:
    """模型给的值当心点用：非数字 / 非正数退回默认，超过上限就截到上限。"""
    try:
        t = float(value)
    except (TypeError, ValueError):
        return float(SHELL_TIMEOUT)
    if t <= 0:
        return float(SHELL_TIMEOUT)
    return min(t, float(MAX_SHELL_TIMEOUT))


def run_shell(command: str, timeout: float = SHELL_TIMEOUT, background: bool = False) -> str:
    """跑一条 shell 命令——超时杀整棵进程树，输出合并成一个流。

    用哪个 shell 由 `shell.detect()` 决定（bash 优先，其次 pwsh，cmd 只兜底）：
    命令以 `[shell, <旗标>, command]` 的形式直接交给它，不经 `shell=True` 绕一层 cmd。

    另外三个刻意的选择：

    1. **stderr 并入 stdout**：模型关心"发生了什么、按什么顺序"，不关心走哪个通道。
       分开收集再拼接会把时序打乱（编译错误的上下文就错位了）。
    2. **stdin 接空设备**：等 stdin 的命令（`git commit` 不带 `-m`、`more`）不该
       白挂到超时才结束，直接让它们读到 EOF 自己退出。
    3. **抓字节自己解码**，不用 `text=True`：父进程按 locale 解、子进程按自己的编码写，
       两者可以不一致（`PYTHONUTF8=1` + GBK 控制台就是典型），一旦不一致就是乱码或
        `UnicodeDecodeError`。

    `background=True` 立刻返回任务 id，不在这里等——dev server / watch 这类命令**没有
    「跑完」这个状态**，前台等它们只有超时被杀或堵住会话两个结局。
    """
    if str(background).strip().lower() in ("1", "true", "yes"):  # 模型可能给字符串
        return _run_background(command)
    timeout = _clamp_timeout(timeout)
    proc = subprocess.Popen(
        [*_shell().argv, command],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        return f"退出码 {proc.returncode}\n{_decode(out)}"
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
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


def _kill_tree(proc) -> None:
    """杀整棵进程树。

    Windows 上 `proc.kill()` 只杀 shell，孙进程还攥着管道——`communicate` 会一直等到它
    自己跑完，等于没超时。`taskkill /T` 才是连子孙一起收。
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, text=True,
        )
    else:
        proc.kill()


def _run_background(command: str) -> str:
    """后台启动：输出直接落文件，父进程不等。

    `CREATE_NEW_PROCESS_GROUP` / `start_new_session` 是为了让它在 TUI 里被 Ctrl+C 时
    **不连坐**——中断的是这一轮，不是模型替你起的那个 dev server。真要停，用 task_stop。
    """
    from . import tasks

    task_id = tasks.next_id()
    path = tasks.output_path(task_id)
    extra = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    with path.open("wb") as f:  # 子进程拿到句柄后父进程这份就该关，别一直占着
        proc = subprocess.Popen(
            [*_shell().argv, command],
            stdout=f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **extra,
        )
    tasks.register(task_id, command, proc, path)
    return (
        f"已在后台启动 {task_id}\n"
        f"命令：{command}\n"
        f"输出：{path}\n"
        f"看输出用 task_output(\"{task_id}\")，停掉用 task_stop(\"{task_id}\")\n"
        f"提醒：后台命令不会等你，也不会自己结束（dev server 这类）——用完记得停。"
    )


_TAIL_BYTES = 64_000  # 读输出只取末尾这么多：后台日志可以是几百 MB，整读会先把内存吃光


def _read_tail(path: Path, limit: int = _TAIL_BYTES) -> tuple[str, int]:
    """读文件末尾（按行对齐，避免从半个字符开始解）。返回 (文本, 总字节数)。"""
    try:
        size = path.stat().st_size
    except OSError:
        return "", 0
    with path.open("rb") as f:
        if size > limit:
            f.seek(size - limit)
            f.readline()  # 丢掉可能被截断的那一行
        raw = f.read()
    return _decode(raw), size


def task_output(task_id: str) -> str:
    """看后台任务的输出与状态。给末尾一段，不整读——日志可以无限长。"""
    from . import tasks

    t = tasks.get(task_id)
    if t is None:
        known = ", ".join(x.id for x in tasks.all_tasks())
        return f"没有任务 {task_id}。已知任务：{known or '（无）'}"

    if t.alive and not _size(t.path):  # 刚启动，输出还没落盘——给它半秒
        time.sleep(0.5)

    text, size = _read_tail(t.path)
    status = (
        f"运行中（已 {int(t.elapsed)} 秒）"
        if t.alive
        else f"已结束（退出码 {t.exit_code}）"
    )
    head = f"任务 {task_id}：{t.command}\n状态：{status}"
    if size > _TAIL_BYTES:
        head += f"\n（只显示末尾 {_TAIL_BYTES // 1000}k 字节，完整输出在 {t.path}）"
    body = text.rstrip() or "（还没有输出）"
    return f"{head}\n{'─' * 24}\n{body}"


def task_stop(task_id: str) -> str:
    """停掉后台任务（连子孙进程）。登记簿里留着记录，之后还能看它最后的输出。"""
    from . import tasks

    t = tasks.get(task_id)
    if t is None:
        return f"没有任务 {task_id}。"
    if not t.alive:
        return f"任务 {task_id} 已经结束了（退出码 {t.exit_code}），不需要停。"
    _kill_tree(t.proc)
    try:
        t.proc.wait(timeout=5)  # 收尸，顺手让退出码确定下来
    except subprocess.TimeoutExpired:
        pass
    return f"已停止 {task_id}（{t.command}）。"


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _reap_background() -> None:
    """程序退出时收掉所有后台任务——没人管的 dev server 会一直占着端口。"""
    from . import tasks

    for t in tasks.running():
        _kill_tree(t.proc)


atexit.register(_reap_background)


def tool_detail(args: dict | None) -> str:
    """工具调用里「最有信息量的那个参数」——界面一行显示用。

    command / path / task_id / subtask 四选一：`task_output` 既没有命令也没有路径、
    `spawn_subagent` 只有一段派活说明——只显示 `⚙ task_output` 等于什么都没说。
    这条规则放一处，TUI 与网页不会再各写一遍。

    空白压成单空格：派活说明通常分几行写（目标 / 范围 / 完成标准），带进 Live 会
    把一行提示撑成三行。界面只需要「大概在干什么」，全文在 /trace 里。
    """
    a = args or {}
    raw = a.get("command") or a.get("path") or a.get("task_id") or a.get("subtask") or ""
    return " ".join(str(raw).split())


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

# ── 改动留痕 ──────────────────────────────────────────────────────
# 写文件的工具顺手算出 diff，loop 在调用后取走一次。
#
# 为什么不让工具把 diff 拼进返回值：返回值要原样回灌给模型（进 history 就按 32k 截断、
# 还要算 token）。diff 是给人看的，混进模型那一份纯属浪费。
# 为什么不用模块全局变量：网页端每个连接各跑一轮，全局会串到别的会话。
MAX_DIFF_CHARS = 20_000  # 超大文件的整篇替换能产出几十万行 diff，先在源头封顶

_LAST_DIFF: ContextVar[str] = ContextVar("last_diff", default="")


def take_diff() -> str:
    """取走并清空本次调用产生的 diff（loop 每次调用后取一次）。"""
    diff = _LAST_DIFF.get()
    _LAST_DIFF.set("")
    return diff


def _record_diff(old: str, new: str, path: str) -> None:
    """「改前 / 改后」→ unified diff；没有变化就记空。"""
    if old == new:
        _LAST_DIFF.set("")
        return
    text = "\n".join(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=f"a/{path}", tofile=f"b/{path}", n=3, lineterm="",
        )
    )
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + f"\n…（diff 共 {len(text)} 字符，已截断）"
    _LAST_DIFF.set(text)


def write_file(path: str, content: str):
    """写入文件内容"""
    p = Path(path)
    old = _read_text(p) if p.exists() else ""  # 覆盖前先留一份，界面才能显示"改了什么"
    p.write_text(content, encoding="utf-8")
    _record_diff(old, content, path)
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
    _record_diff(content, content.replace(old, new), path)
    return f"已编辑 {path}（替换 {len(old)} 字符 → {len(new)} 字符）。"

