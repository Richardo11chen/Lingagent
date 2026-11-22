import atexit
import difflib
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .shell import detect as _shell

SHELL_TIMEOUT = 120  # 秒：一条 shell 命令的默认上限（S10 真机撞出的墙——卡住的命令不许冻住整台机器）
MAX_SHELL_TIMEOUT = 600  # 秒：模型自己能调到的上限（装包 / 全量构建偶尔要多几分钟）

# 回灌给模型的结果上限（字符）。trace 存全文（审计账，人看），history / session 走截断版
# （模型看）——三份拷贝各按各的读者定规格，别让一次命令把上下文和存档撑爆。
MAX_RESULT_CHARS = 32_000
_HEAD_CHARS = 24_000  # 截断时保留头尾：开头是定位信息，结尾是报错摘要
_TAIL_CHARS = 8_000

# 超限结果的全文落这里（工作目录内，已 gitignore）。为什么不放 ~/.agent-harness：
# 模型手里的**文件工具**（read/write/edit）只认项目内路径——那是这套东西里唯一由代码
# 强制的边界；`run_shell` 不在这条边界内（它靠命令分级 + 人工审批，能 cd 出去）。
# 所以这里不为了省一次命令去放宽它。dump 留 7 天，和后台任务日志一个规矩。
DUMP_DIR = ".agent-harness-out"
DUMP_KEEP_DAYS = 7


def cap_result(
    text: str, limit: int = MAX_RESULT_CHARS, root: Path | None = None
) -> str:
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
    where = _dump_full(text, root)
    if where:
        hint = f"全文在 {where}（相对项目根）——用 read_file 或 `sed -n '1,200p' {where}` 分段看"
    else:
        hint = "需要细节请用带范围的命令重新取（如 head / tail / grep 过滤）"
    return (
        f"{text[:_HEAD_CHARS]}\n\n"
        f"[... 已省略中间 {omitted} 字符（原始输出共 {len(text)} 字符）。{hint}]\n\n"
        f"{text[-_TAIL_CHARS:]}"
    )


def _dump_full(text: str, root: Path | None = None) -> str:
    """把全文写到工作区的 `.agent-harness-out/`，返回相对路径；失败返回 ""。

    root 由调用方给（装配层传工作区）。None 只在没接装配层的单测里出现——
    那里退回当前目录，是为了不让一个便利功能把测试逼成必须造 Workspace。

    落盘只是便利，不该让一次工具调用失败——写不进去就退回"重新取"那句提示。
    """
    try:
        base = root or Path.cwd()
        d = base / DUMP_DIR
        d.mkdir(exist_ok=True)
        _prune_dumps(d)
        p = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}.txt"
        p.write_text(text, encoding="utf-8")
        return p.relative_to(base).as_posix()
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
        **_popen_group(),  # 独立进程组：超时才能把孙进程一起收
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        text = _decode(out)
        if not text.strip():
            # 「跑成功但没输出」和「根本没执行」长得一模一样（Windows 商店的 python
            # 就是个 exit 0 且零输出的转发器），至少别让模型把空白读成"执行过了"
            text = "（命令没有产生任何输出）"
        return f"退出码 {proc.returncode}\n{text}"
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


def _popen_group() -> dict:
    """让这次 spawn 自己当进程组组长——超时收尸时才可能 killpg 整棵树。

    POSIX 上不给这个参数，子进程就和 lingan 同组；而 `proc.kill()` 只杀 shell，
    `npm run build` 起的 node、pytest 起的 worker 全都活下来继续占端口/CPU。
    Windows 那边靠 `taskkill /T` 按父子关系收，不需要这个。
    """
    return {} if os.name == "nt" else {"start_new_session": True}


def _kill_tree(proc) -> None:
    """杀整棵进程树。

    Windows 上 `proc.kill()` 只杀 shell，孙进程还攥着管道——`communicate` 会一直等到它
    自己跑完，等于没超时。`taskkill /T` 才是连子孙一起收。

    POSIX 上同理，做法是杀**进程组**：配合 Popen 时的 `start_new_session`，
    整个 shell 树在自己那一个组里，一个 `killpg` 全收。直接 SIGKILL 不先 SIGTERM——
    超时意味着已经等够了，再给一轮"体面退出"只会多等一次。
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, text=True,
        )
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # 组没了 / 拿不到组：至少把 shell 本身收掉，别因为这一步把整个超时逻辑挂掉
        try:
            proc.kill()
        except OSError:
            pass


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
        return (
            f"没有任务 {task_id}。已知任务：{known or '（无）'}\n"
            "任务簿只在内存里：进程重启就清空；而且 TUI 和网页后端是两个进程、各有各的编号——"
            "在另一个入口起的任务，这里看不到。"
        )

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


def task_snapshot(task_id: str, tail_chars: int = 4000) -> dict | None:
    """后台任务的一条快照（给界面用；没有这个任务返回 None）。

    与 task_output 的区别：那个是给模型读的整段文字，这个是给前端渲染的结构化字段。
    两处都只读末尾——日志可以无限长。
    """
    from . import tasks

    t = tasks.get(task_id)
    if t is None:
        return None
    text, size = _read_tail(t.path)
    return {
        "id": t.id,
        "command": t.command,
        "alive": t.alive,
        "exit_code": t.exit_code,
        "elapsed": round(t.elapsed, 1),
        "started": t.started,
        "tail": text[-tail_chars:],
        "size": size,
    }


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
    """读文本：按常见编码依次尝试，换行统一成 LF。

    死认 utf-8 的代价很实在：碰到 GBK 文件工具直接抛异常，而模型改不了文件编码，
    只能绕开——等于这个文件对它不可读。

    换行归一这条是真踩出来的：Windows 上写出去的是 `\\r\\n`，而模型给的多行 `old`
    是 `\\n`——一字不差也匹配不上，报「未找到」，人只能怀疑自己看错了文件。
    """
    return _decode(p.read_bytes()).replace("\r\n", "\n")


def _write_text(p: Path, text: str) -> None:
    """写文本：一律 LF，父目录不存在就建。

    不指定 newline 的话，`Path.write_text` 在 Windows 上会把每个 `\\n` 翻成 `\\r\\n`
    ——读回来的字节带着 `\\r`，下一次多行编辑又匹配不上，形成"改一次坏一次"的循环。
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


MAX_READ_BYTES = 8_000_000  # 8MB：再大就不是"一个源文件"了，整读会顶到内存


def read_file(path: str, offset: int = 1, limit: int | None = None) -> str:
    """读取文件内容。offset/limit 按行切（1 起始），不给就是全文。

    上下文那头有 `cap_result` 兜着（32k 截断 + 全文落盘），所以这里的闸是给**内存**的：
    `read_bytes()` 会把整个文件读进来，再解码成 str 又是 1.5~2 倍——一个 500MB 的日志
    足以让进程被 OOM 杀掉，而那看起来像"工具莫名消失"。

    不做流式按行读：那一小段代码要处理跨块的多字节字符和总行数统计，而这个场景
    shell 本来就能做（`sed -n '100,200p'` 天生流式）。提示比实现更有用。
    """
    p = Path(path)
    if p.is_file():
        size = p.stat().st_size
        if size > MAX_READ_BYTES:
            mb = size / 1024 / 1024
            return (
                f"文件太大（{mb:.0f}MB，上限 8MB），没有读——整读会把内存吃光。\n"
                f"要看其中一段，用 run_shell：sed -n '1,200p' \"{path}\""
                "（或 rg 先定位关键词，再取那几行）。"
            )
    text = _read_text(p)
    if offset <= 1 and limit is None:
        return text
    lines = text.splitlines(keepends=True)
    start = max(int(offset), 1) - 1
    end = start + int(limit) if limit else len(lines)
    end = min(end, len(lines))
    # 头部说明让模型知道"还有多少没看"——否则它会把一段当成全文，改错地方
    return f"（第 {start + 1}-{end} 行，共 {len(lines)} 行）\n" + "".join(lines[start:end])

# ── 改动留痕 ──────────────────────────────────────────────────────
# 写盘只有一个出口，三个角色分工：
#   write_file / edit_file  →  校验并返回**意图**（FileChange），自己不落盘
#   apply_change            →  唯一的写盘点：写盘 + 算 diff + 给出确认文案
#   loop.agent_loop         →  拿到意图就 apply，把 diff 同时送进 trace 和界面
#
# 为什么不让工具自己写（P0）：diff 原先走 ContextVar 旁路——写工具时顺手记下、
# loop 事后 take_diff() 取走。可后钩子排在这两步中间，钩子改的文件就被算到
# 上一个工具头上，一个纯读工具会报出 `+1 −1`；而且事件在 timed 块退出时就落盘了，
# trace 里从来没有 diff 字段。改成「工具只表态、执行点落盘」之后，diff 与写盘同源，
# 工具也没有能力"写盘但忘了算 diff"。
#
# 为什么 diff 跟着返回值走，而不是再搞一个旁路：网页端每个连接各跑一轮，
# 任何模块级 / ContextVar 状态都有机会串到别的会话——旁路只要存在就迟早被踩。
MAX_DIFF_CHARS = 20_000  # 超大文件的整篇替换能产出几十万行 diff，先在源头封顶


@dataclass(frozen=True)
class FileChange:
    """一次改动的**意图**——还没写盘。old=None 表示新建（磁盘上没有旧内容）。"""

    path: str
    old: str | None
    new: str
    # 工具自己那句确认话（"写入文件 x 12 字符"）：模型看到的文本仍由工具决定，
    # 换执行点不改模型视角的任何一句话——否则回归时分不清是重构坏了还是措辞变了。
    summary: str


class ToolResult(NamedTuple):
    """执行点的产出：给模型的那句话 + 给人看的 diff（同一份改动，两个读者）。"""

    content: str
    diff: str = ""


def _make_diff(old: str, new: str, path: str) -> str:
    """「改前 / 改后」→ unified diff；没有变化就是空串。

    纯函数：没有隐藏状态，所以能单独测——这是拆掉 ContextVar 之后白送的。
    """
    if old == new:
        return ""
    text = "\n".join(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=f"a/{path}", tofile=f"b/{path}", n=3, lineterm="",
        )
    )
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + f"\n…（diff 共 {len(text)} 字符，已截断）"
    return text


def apply_change(change: FileChange) -> ToolResult:
    """唯一写盘点：落盘 + 算 diff。写完才算"改动发生过"。

    写失败（权限 / 磁盘满）直接抛给调用方——loop 的 try 把它变成一句回灌给模型的
    错误，和工具自己抛异常走同一条路。不在这里吞掉：吞了就成了"界面说改好了、
    文件其实没变"。

    `_write_text` 只在这里被调用，架构测试（tests/test_tools.py）盯着这一点：
    新增写盘路径必须经过这里，否则 diff 就又有机会缺席。
    """
    _write_text(Path(change.path), change.new)
    return ToolResult(change.summary, _make_diff(change.old or "", change.new, change.path))


def write_file(path: str, content: str) -> FileChange:
    """准备写入文件内容（父目录不存在会建；换行统一成 LF）——只返回意图，不落盘。

    覆盖前先留一份旧内容：界面要显示"改了什么"，而这时候文件还没动。
    """
    p = Path(path)
    content = content.replace("\r\n", "\n")  # 落盘前归一，diff 才不会显示整篇都变了
    old = _read_text(p) if p.exists() else None
    return FileChange(path, old, content, f"写入文件 {path} {len(content)} 字符。")


def _line_no(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _occurrence_lines(content: str, old: str) -> list[int]:
    """old 在文件里出现的每一处，换算成行号。"""
    out: list[int] = []
    start = 0
    while True:
        i = content.find(old, start)
        if i < 0:
            return out
        out.append(_line_no(content, i))
        start = i + len(old)


def _nearest_line(content: str, old: str) -> tuple[int, float, str] | None:
    """文件里最像 old 首行的行——「未找到」时唯一有用的线索。"""
    first = next((ln.strip() for ln in old.splitlines() if ln.strip()), "")
    if not first:
        return None
    best: tuple[int, float, str] | None = None
    for i, line in enumerate(content.splitlines(), 1):
        ratio = difflib.SequenceMatcher(None, first, line.strip()).ratio()
        if best is None or ratio > best[1]:
            best = (i, ratio, line.strip())
    return best


def edit_file(path: str, old: str, new: str) -> FileChange | str:
    """精确替换文件里的一段文本（保留其余内容）——成功返回意图，不落盘。

    替换成功时返回 `FileChange`；**校验失败时返回 str**（那句给模型看的失败原因），
    因为失败不是一个"改动"。调用方按类型分派：拿到的不是意图，就没什么可落盘的。

    old 必须在文件中唯一出现。0 次和多次都拒绝，但要**分清是哪一种**：前者要改 old，
    后者要缩小范围——报同一句话等于让人自己猜，而猜错的代价是又一轮试错。

    换行两边都归一：模型从别处复制来的 old 可能带 \\r\\n，文件里可能是 \\n。
    """
    p = Path(path)
    content = _read_text(p)
    old = old.replace("\r\n", "\n")
    new = new.replace("\r\n", "\n")

    if old not in content:
        near = _nearest_line(content, old)
        hint = ""
        if near is not None:
            line_no, ratio, line = near
            hint = (
                f"\n最接近的是第 {line_no} 行（相似度 {ratio:.0%}）：{line[:80]}"
                if ratio >= 0.6
                else "\n文件里没有相近的行——先 read_file 确认目标在哪个文件。"
            )
        return f"编辑失败：未找到要替换的文本（old 与文件当前内容不匹配）。{hint}"

    lines = _occurrence_lines(content, old)
    if len(lines) > 1:
        where = "、".join(str(n) for n in lines[:6]) + ("…" if len(lines) > 6 else "")
        return (
            f"编辑失败：要替换的文本出现 {len(lines)} 次（第 {where} 行），"
            "请带上更多上下文，让它唯一。"
        )

    updated = content.replace(old, new, 1)
    return FileChange(
        path, content, updated, f"已编辑 {path}（替换 {len(old)} 字符 → {len(new)} 字符）。"
    )

