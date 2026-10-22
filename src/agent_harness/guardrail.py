"""治理护栏：命令风险判定 + 路径围栏。

S11 换锁：判定从「危险命令黑名单」翻成「风险分级 + 默认审批」——
只对确认只读的命令放行；改动 / 执行 / 网络 / 未知一律问人；极危险直接拒。
方向变了：黑名单默认信任（不在表里就放行），风险分级默认不信（判不出安全就问）。

规格硬标准（A.4）：护栏是代码机制，不是提示词——判定可确定性单测。
"""
import ntpath
import re
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

# 只读命令：确认不改工作区（重定向另判——`>` 出现即按改动算）。
# 注意：sed / awk / xargs / tee 能写或执行，故意不入白名单。
# find 单独处理：它默认只读，但带 -delete / -exec 就是写/执行（见 _classify_tokens）。
READ_ONLY_PROGRAMS = {
    "dir", "type", "findstr", "where", "ver", "cd", "chdir", "echo", "set",
    "whoami", "hostname", "tasklist", "vol", "date", "title", "cls", "path",
    "color", "mode", "more", "fc", "comp", "tree", "diff", "cmp",
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "stat", "pwd", "basename",
    "dirname", "realpath", "md5sum", "sha1sum", "sha256sum", "which", "uname", "du",
}

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

# 只打印版本号的程序：仅当唯一参数是版本旗标时算只读（其余一律按执行算）
VERSION_PROGRAMS = {"python", "py", "python3", "pytest", "node", "npm", "uv", "pip", "pip3"}
VERSION_FLAGS = {"--version", "-version", "-V", "-v"}

MUTATING_PROGRAMS = {
    "del", "erase", "rm", "rmdir", "rd", "mkdir", "md", "copy", "move", "ren",
    "rename", "attrib", "icacls", "takeown", "xcopy", "robocopy", "mklink",
    "fsutil", "reg", "schtasks", "sc", "net", "wmic", "tar", "unzip", "7z",
    "expand", "compact", "chkdsk", "defrag", "cipher", "bcdedit", "shutdown",
    "taskkill", "assoc", "ftype",
}

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


def classify_command(command: str) -> CommandRisk:
    """把一条 shell 命令拆段、分类——整条取最严的一段（复合命令按最严算）。"""
    # fd→fd 重定向（2>&1）里的 & 不是命令分隔符，也不写文件——先剔除
    command = re.sub(r"\d?>&\d", "", command)
    # 写空设备（>nul / 2>nul / >/dev/null）不改工作区——同样先剔除（Windows 常用它吞 stderr）
    command = re.sub(r"\d?>\s*(?:nul|/dev/null)\b", "", command, flags=re.IGNORECASE)
    segments, balanced = _split_segments(command)
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
    """
    tokens = _tokens(segment)
    while tokens and tokens[0].lower() in SYNTAX_WORDS:
        tokens = tokens[1:]
    if not tokens:
        return []
    head = tokens[0].lower()
    # 程序位不可能含通配符（`*.py`）、也不可能以变量符开头（`%f` / `%%f` / `%~dp0` / `$x`）
    if "*" in head or "?" in head or head.startswith(("%", "$")):
        return []
    return tokens


def _split_segments(command: str) -> tuple[list[str], bool]:
    """按未加引号的 & | ( ) 和换行拆段；返回 (段列表, 引号是否闭合)."""
    segs: list[str] = []
    buf: list[str] = []
    quote = False
    for c in command:
        if c == '"':
            quote = not quote
            buf.append(c)
        elif not quote and (c in "&|()" or c in "\r\n"):
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
    if prog in VERSION_PROGRAMS:
        if len(args) == 1 and args[0].lower() in VERSION_FLAGS:
            return CommandRisk(READ_ONLY, f"只读：{prog} {args[0]}")
        return CommandRisk(EXECUTING, f"执行程序：{prog}")
    if prog in READ_ONLY_PROGRAMS:
        return CommandRisk(READ_ONLY, f"只读：{prog}")
    if prog in MUTATING_PROGRAMS:
        return CommandRisk(MUTATING, f"改动文件：{prog}")
    if prog in EXECUTING_PROGRAMS:
        return CommandRisk(EXECUTING, f"执行程序：{prog}")
    if prog in NETWORK_PROGRAMS:
        return CommandRisk(NETWORK, f"网络命令：{prog}")
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
    """路径是否在工作目录内：先 resolve（解析 .. 和符号链接），再比前缀。"""
    resolved = Path(path).resolve()
    return resolved == workspace or workspace in resolved.parents


def safe_tool(fn, workspace: Path):
    """包装文件工具：执行前校验路径，越界拦截。工具本身不知道边界（闭包）。"""
    def wrapped(path: str, **kwargs):
        if not in_workspace(path, workspace):
            return f"越界拦截：{path} 不在工作目录内。"
        return fn(path, **kwargs)
    return wrapped
