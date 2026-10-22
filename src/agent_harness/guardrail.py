"""治理护栏：命令风险判定 + 路径围栏。

S11 换锁：判定从「危险命令黑名单」翻成「风险分级 + 默认审批」——
只对确认只读的命令放行；改动 / 执行 / 网络 / 未知一律问人；极危险直接拒。
方向变了：黑名单默认信任（不在表里就放行），风险分级默认不信（判不出安全就问）。

规格硬标准（A.4）：护栏是代码机制，不是提示词——判定可确定性单测。
"""
import ntpath
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# ---- 风险等级（只读放行；中间四档问人；catastrophic 直接拒）----

READ_ONLY = "read_only"
MUTATING = "mutating"
EXECUTING = "executing"
NETWORK = "network"
UNKNOWN = "unknown"
CATASTROPHIC = "catastrophic"

_SEVERITY = {READ_ONLY: 0, UNKNOWN: 1, NETWORK: 2, EXECUTING: 3, MUTATING: 4, CATASTROPHIC: 5}

CATASTROPHIC_PROGRAMS = {
    "format", "fdisk", "parted", "diskpart", "dd", "shred", "wipe",
    "mkfs", "mkfs.ext2", "mkfs.ext3", "mkfs.ext4", "mkfs.xfs", "mkfs.vfat",
    "mkfs.ntfs", "mkfs.btrfs",
}

# 下面几张表**不是安全边界**，只是"省一次审批"的默认值：
# 删掉一个名字不会漏（它会落到 unknown，照样问人），只会让那条命令从"直接跑"
# 变成"先问你"。所以列宽没有代价，列窄了要你多点几次回车。
# 分组按「这个名字在哪个 shell 里真的存在」——三种词汇混在一起（bash coreutils /
# Windows 自带 exe / cmd 与 PowerShell 的内建和别名）是它读起来臃肿的真正原因。
READ_ONLY_PROGRAMS = {
    # bash / coreutils——现在真正常用的那一批
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "stat", "pwd", "basename",
    "dirname", "realpath", "md5sum", "sha1sum", "sha256sum", "which", "uname", "du",
    "diff", "cmp", "echo", "cd",
    # Windows 自带的 exe——在 Git Bash 里照样能调（实测 tasklist / attrib 都跑得动）
    "where", "findstr", "tasklist", "more", "fc", "comp", "tree", "hostname", "whoami",
    # cmd 内建 / PowerShell 别名——bash 里没有，pwsh 兜底时才真能跑
    # （del→Remove-Item、type→Get-Content、cls→Clear-Host 这些在 pwsh5 里都是别名）
    "dir", "type", "ver", "vol", "title", "color", "mode", "path", "cls", "chdir", "set", "date",
}

# 故意不在这张表里：sed / awk / xargs / tee（能写文件或执行）。find 单独处理——
# 默认只读，带 -delete / -exec / -fprint 才升档（见 _classify_tokens）。

# find 的危险开关：命中即从只读升到执行/改动
FIND_WRITE_FLAGS = (
    "-delete", "-exec", "-execdir", "-ok", "-okdir",
    "-fprint", "-fprintf", "-fls",
)

# cmd / bash 的语法词：它们不是可执行程序，出现在命令行里不代表"未知命令"
SYNTAX_WORDS = {
    "for", "if", "do", "else", "then", "elif", "fi", "while", "until", "done",
    "in", "not", "rem", "setlocal", "endlocal", "goto", "exit",
}

# 允句尾出现语法词：`for %f in (*.py)` 被切开后剩「%f in」，尾巴上这个 in 要剥掉
TRAILING_SYNTAX_WORDS = {"in", "do"}

# 只有一个版本旗标时算只读的程序：`python --version` 不该问人，`python x.py` 必须问。
# 这些名字本来就在 EXECUTING_PROGRAMS 里，这张表只负责「版本例外」，不重复登记风险。
VERSION_PROGRAMS = {"python", "py", "python3", "pytest", "node", "npm", "uv", "pip", "pip3"}
VERSION_FLAGS = {"--version", "-version", "-V", "-v"}

MUTATING_PROGRAMS = {
    # bash / coreutils——换到 Git Bash 之后这些才是常见形态
    "rm", "rmdir", "mkdir", "cp", "mv", "touch", "ln", "chmod", "chown",
    "tee", "truncate", "rsync", "install", "unlink",
    # 压缩与解包（两种 shell 下都能调）
    "tar", "unzip", "7z", "expand", "compact",
    # Windows 自带的管理类 exe——Git Bash 里也调得动，但都要问：改权限 / 改注册表 / 动机器
    "icacls", "takeown", "robocopy", "xcopy", "attrib", "fsutil", "reg", "schtasks",
    "sc", "net", "wmic", "chkdsk", "defrag", "cipher", "bcdedit", "shutdown", "taskkill",
    # cmd 内建 / PowerShell 别名——pwsh 兜底时才真能跑（bash 里是 command not found）
    "del", "erase", "rd", "md", "copy", "move", "ren", "rename", "mklink", "assoc", "ftype",
}

# dd / shred 归 catastrophic，别在这里重复列（重复了也不会出错，但两处真相会各自漂）

# sed 默认只读；带 -i 才改文件（`sed -n '1,10p'` 是最常见的看代码方式）
SED_WRITE_FLAGS = ("-i", "--in-place")

EXECUTING_PROGRAMS = {
    "python", "py", "python3", "pytest", "powershell", "pwsh", "cmd", "command",
    "bash", "sh", "zsh", "wsl", "node", "npm", "npx", "yarn", "pnpm", "uv", "pip",
    "pip3", "conda", "java", "javac", "go", "cargo", "rustc", "dotnet", "ruby",
    "perl", "php", "cscript", "wscript", "mshta", "rundll32", "regsvr32", "start", "call",
}

NETWORK_PROGRAMS = {
    "curl", "wget", "certutil", "bitsadmin", "ftp", "sftp", "scp", "ssh",
    "telnet", "nc", "ncat", "ping", "tracert", "nslookup",
}

# 程序名 → 风险 + 理由前缀。**顺序即优先级**：先命中先返回。
# 做成一张表而不是五段 if，是因为优先级原来藏在语句顺序里——把 NETWORK 那段
# 挪到 EXECUTING 上面，行为就悄悄变了，而没人会注意到。表里一眼看得见。
# （同一程序不得同时出现在两张表里；有测试守着，否则结果会取决于表序。）
# catastrophic 不在这张表里：它由上面那道显式判断处理（还要看目标是不是根路径）。
_PROGRAM_TABLE: tuple[tuple[set[str], str, str], ...] = (
    (READ_ONLY_PROGRAMS, READ_ONLY, "只读"),
    (MUTATING_PROGRAMS, MUTATING, "改动文件"),
    (EXECUTING_PROGRAMS, EXECUTING, "执行程序"),
    (NETWORK_PROGRAMS, NETWORK, "网络命令"),
)

GIT_READ_SUBCOMMANDS = {
    "status", "log", "diff", "show", "rev-parse", "blame", "ls-files", "shortlog",
    "describe", "grep", "show-ref", "for-each-ref", "cat-file", "merge-base",
}

ROOT_TARGETS = {"/", "/*", "~", "~/*", "\\", "*", "*.*", "./*", ".\\*",
                "c:\\", "c:/", "d:\\", "d:/"}


@dataclass
class CommandRisk:
    """判定结果：risk 决定放行 / 问人 / 直拒；reason 进回灌与 trace。"""

    risk: str
    reason: str = ""


def classify_command(command: str, allow: Sequence[str] = ()) -> CommandRisk:
    """把一条 shell 命令拆段、分类——整条取最严的一段（复合命令按最严算）。

    allow：人写的允许规则（前缀语义，见 matches_allow）。顺序是
    **灾难 > 规则 > 分类**——写明 `allow = ["*"]` 也放不出 `format c:`，
    这是刻意留的地板。
    """
    worst = _classify_segments(command)
    if worst.risk == CATASTROPHIC:
        return worst
    hit = matches_allow(command, allow)
    if hit:
        return CommandRisk(READ_ONLY, f"已允许：{hit}")
    return worst


def matches_allow(command: str, patterns: Sequence[str] = ()) -> str | None:
    """命令是否命中允许规则；返回命中的那条（给 trace / 提示用）。

    语义刻意做成「前缀」而不是正则/glob 全匹配：写 `git *` 就是"以 git 开头的任何命令"，
    写 `git status` 就是"只这一条"。规则是给人写的，不该要求记通配符语法。
    """
    cmd = " ".join(command.split()).lower()
    if not cmd:
        return None
    for raw in patterns or ():
        pat = " ".join(str(raw).split()).lower()
        if not pat:
            continue
        if pat.endswith("*"):
            if cmd.startswith(pat[:-1]):
                return raw
        elif cmd == pat:
            return raw
    return None


def command_head(command: str) -> str:
    """取整条命令的第一个「真程序名」——会话内"不再问"按它归类。

    跳过语法词与残片（`for %f in` / `*.py`），所以对
    `for %f in (*.py) do @del %f` 得到的是 `del` 而不是 `for`。
    """
    segments, _balanced = _split_segments(_strip_fd_redirects(command))
    for seg in segments:
        tokens = _command_tokens(seg)
        if tokens:
            return _normalize_prog(tokens[0])
    return ""


def _strip_fd_redirects(command: str) -> str:
    """剔除 fd→fd 重定向与写空设备：它们既不是命令，也不改工作区。"""
    command = re.sub(r"\d?>&\d", "", command)
    return re.sub(r"\d?>\s*(?:nul|/dev/null)\b", "", command, flags=re.IGNORECASE)


def _classify_segments(command: str) -> CommandRisk:
    """纯分类（不含 allow 规则）——拆段 + 每段取最严。"""
    segments, balanced = _split_segments(_strip_fd_redirects(command))
    if not balanced:
        return CommandRisk(UNKNOWN, "引号不闭合，无法解析")
    if not segments:
        return CommandRisk(UNKNOWN, "空命令")

    worst = CommandRisk(READ_ONLY, "只读")
    for seg in segments:
        if _has_file_write(seg):  # 重定向先判：语法残片里也可能带 `>`
            worst = _worse(worst, CommandRisk(MUTATING, "含输出重定向（> / >>）"))
            continue
        tokens = _command_tokens(seg)
        if not tokens:  # 整段都是语法残片（for %f in / *.py）——不是命令，不该去问人
            continue
        worst = _worse(worst, _classify_tokens(tokens))
        if worst.risk == CATASTROPHIC:
            return worst
    return worst


def _command_tokens(segment: str) -> list[str]:
    """剥掉语法词之后剩下的「真命令」token；整段都是残片时返回空列表。

    `for %f in (*.py) do @del %f` 按 ( ) & 拆开后剩下 `do @del %f`——
    只剥掉开头的 `do`、保留 `del`，否则真命令就被语法词一起吞了。

    变量开头的段只在「整段就一个变量」时才当残片（`for %f in` 里的 `%f`）。
    带参数的 `%x% /q file` 必须继续往下判——变量里可能藏着命令（`set x=del & %x% /q a`）。
    """
    tokens = _tokens(segment)
    while tokens and tokens[0].lower() in SYNTAX_WORDS:
        tokens = tokens[1:]
    while tokens and tokens[-1].lower() in TRAILING_SYNTAX_WORDS:
        tokens = tokens[:-1]
    if not tokens:
        return []
    head = tokens[0].lower()
    # 程序位不可能含通配符（`*.py`）
    if "*" in head or "?" in head:
        return []
    # 纯变量（`%f` / `%%i` / `$x`）是 for 的循环变量，不是命令
    if head.startswith(("%", "$")) and len(tokens) == 1:
        return []
    return tokens


def _split_segments(command: str) -> tuple[list[str], bool]:
    """按未加引号的 & | ( ) ; 和换行拆段；返回 (段列表, 引号是否闭合)。

    `;` 必须算分隔符：bash 里 `echo a; rm -rf x` 是两条命令，漏掉它就会
    只看第一段（echo）而放过第二段——那是 fail open。
    """
    segs: list[str] = []
    buf: list[str] = []
    quote = False
    for c in command:
        if c == '"':
            quote = not quote
            buf.append(c)
        elif not quote and (c in "&|();" or c in "\r\n"):
            seg = "".join(buf).strip()
            if seg:
                segs.append(seg)
            buf = []
        else:
            buf.append(c)
    seg = "".join(buf).strip()
    if seg:
        segs.append(seg)
    return segs, not quote


def _has_file_write(segment: str) -> bool:
    """未加引号的 > 即「写文件」（调用前已剔除 2>&1 这类 fd→fd 重定向）。"""
    quote = False
    for c in segment:
        if c == '"':
            quote = not quote
        elif c == ">" and not quote:
            return True
    return False


def _tokens(segment: str) -> list[str]:
    """按空白切 token（双引号内不切）——够用于取程序名与判断参数。"""
    out: list[str] = []
    buf: list[str] = []
    quote = False
    for c in segment:
        if c == '"':
            quote = not quote
            buf.append(c)
        elif c.isspace() and not quote:
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(c)
    if buf:
        out.append("".join(buf))
    return out


def _normalize_prog(token: str) -> str:
    """取程序名：去引号 / 去 @ 前缀 / 取 basename / 去扩展名 / 小写."""
    name = token.strip().lstrip("@").strip('"')
    name = ntpath.basename(name).lower()
    for ext in (".exe", ".com", ".bat", ".cmd", ".ps1"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return name


def _classify_tokens(tokens: list[str]) -> CommandRisk:
    prog = _normalize_prog(tokens[0])
    args = tokens[1:]
    if not prog:
        return CommandRisk(UNKNOWN, "取不到程序名")

    if prog in CATASTROPHIC_PROGRAMS or _is_catastrophic_target(prog, args):
        return CommandRisk(CATASTROPHIC, f"极危险命令：{prog}")
    if prog == "git":
        return _classify_git(args)
    if prog == "find":
        # 默认只读；带 -delete / -exec 才升档（Windows 的 find 没有这些开关，天然只读）
        if any(a.lower().startswith(FIND_WRITE_FLAGS) for a in args):
            return CommandRisk(EXECUTING, "find 带执行/删除参数")
        return CommandRisk(READ_ONLY, "只读：find")
    if prog == "sed":
        if any(a.lower().startswith(SED_WRITE_FLAGS) for a in args):
            return CommandRisk(MUTATING, "sed 就地改文件（-i）")
        return CommandRisk(READ_ONLY, "只读：sed")
    if prog in VERSION_PROGRAMS and len(args) == 1 and args[0].lower() in VERSION_FLAGS:
        return CommandRisk(READ_ONLY, f"只读：{prog} {args[0]}")  # 只问「跑什么」，版本号不算跑
    for programs, risk, label in _PROGRAM_TABLE:
        if prog in programs:
            return CommandRisk(risk, f"{label}：{prog}")
    return CommandRisk(UNKNOWN, f"未知命令：{prog}")


def _is_catastrophic_target(prog: str, args: list[str]) -> bool:
    """rm -rf / 与 del /s /q C:\\ 这类「根目标 + 递归/强制」升级为直拒。"""
    if prog not in {"rm", "del", "erase", "rd", "rmdir"}:
        return False
    targets = []
    for a in args:
        if a.startswith("-"):
            continue
        if prog in {"del", "erase", "rd", "rmdir"} and re.fullmatch(r"/[A-Za-z]", a):
            continue  # Windows 旗标（/s /q /f）——不是目标
        targets.append(a)
    rootish = any(t.strip('"').lower() in ROOT_TARGETS for t in targets)
    flags = " ".join(args).lower()
    recursive = any(f in flags for f in ("-r", "-rf", "-fr", "--recursive", "/s", "/q", "/f"))
    return rootish and recursive


def _classify_git(args: list[str]) -> CommandRisk:
    if not args:
        return CommandRisk(READ_ONLY, "只读：git（空参数打印帮助）")
    sub = args[0].lower()
    rest = args[1:]
    if sub in GIT_READ_SUBCOMMANDS:
        return CommandRisk(READ_ONLY, f"只读：git {sub}")
    if sub == "branch":
        mutating = any(a.lower().startswith(("-d", "-m", "-c", "--delete", "--move", "--copy")) for a in rest)
        return CommandRisk(MUTATING if mutating else READ_ONLY, "git branch")
    if sub == "remote":
        read = not rest or all(a in ("-v", "--verbose", "show") for a in rest)
        return CommandRisk(READ_ONLY if read else MUTATING, "git remote")
    if sub == "config":
        read = any(a in ("--get", "--get-all", "--get-regexp", "--list", "-l") for a in rest)
        return CommandRisk(READ_ONLY if read else MUTATING, "git config")
    return CommandRisk(MUTATING, f"git {sub}（会改仓库）")


def _worse(a: CommandRisk, b: CommandRisk) -> CommandRisk:
    return b if _SEVERITY.get(b.risk, 0) > _SEVERITY.get(a.risk, 0) else a


# ---- 路径围栏（S3，未变）----

def in_workspace(path: str, workspace: Path) -> bool:
    """路径是否在工作目录内：先 resolve（解析 .. 和符号链接），再比前缀。

    相对路径按**传进来的 workspace** 解析，不是按进程 cwd——签名里有这个参数，
    行为就该跟它走。靠"反正启动时 cwd 就是项目根"是隐式耦合：换个入口（比如以后
    给某个会话指定别的根）就会静默判错，而这是个安全边界，判错不该是静默的。
    """
    ws = workspace.resolve()
    p = Path(path)
    resolved = p.resolve() if p.is_absolute() else (ws / p).resolve()
    return resolved == ws or ws in resolved.parents


def safe_tool(fn, workspace: Path):
    """包装文件工具：执行前校验路径，越界拦截。工具本身不知道边界（闭包）。"""
    def wrapped(path: str, **kwargs):
        if not in_workspace(path, workspace):
            return f"越界拦截：{path} 不在工作目录内。"
        return fn(path, **kwargs)
    return wrapped
