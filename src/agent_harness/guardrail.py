"""治理护栏：识别危险动作 → 拦截（规格 A.4(B)：护栏必须是代码机制，提示词不算）。"""
from pathlib import Path

# 危险命令表：命令 → 危险参数特征（空元组 = 命令本身危险）
DANGEROUS = {
    # Linux / Unix 核心
    "rm": {"-r", "-f", "--recursive", "--force", "-rf"},
    "rmdir": {"-p"},          # -p 递归删除父目录（可能误删）
    "chmod": {"-R", "000", "777"},  # 递归或全局权限
    "chown": {"-R"},
    "dd": {"of="},            # 写设备（可只匹配 of=）
    "mkfs": None,             # 本身危险
    "fdisk": None,
    "parted": None,
    "shred": {"-z", "-f", "-n"},
    "wipe": {"-f"},
    "mount": {"-o", "remount", "--bind"},
    "umount": {"-l", "-f"},
    "fsck": {"-y", "-f"},
    "shutdown": {"-h", "-r", "-P", "-H", "/s", "/p"},

    # Windows 原生
    "del": {"/s", "/q", "/f"},
    "erase": {"/s", "/q", "/f"},
    "rmdir": {"/s", "/q"},
    "rd": {"/s", "/q"},
    "format": None,
    "diskpart": {"/s"},
    "cipher": {"/w"},
    "takeown": {"/f"},
    "icacls": {"/grant", "/remove", "/t"},
    "reg": {"delete", "/f"},
    "bcdedit": {"/delete", "/f"},

    # macOS 特有
    "diskutil": {"eraseDisk", "eraseVolume"},
    "hdiutil": {"-readwrite"},  # 结合 attach 使用

    # 跨平台网络破坏（可选）
    "iptables": {"-F", "--flush"},  # 清空防火墙规则，可能断网
    "ip": {"flush"},                # 刷新网络配置
}

def is_dangerous_command(command: str) -> bool: #当前逻辑存在误报可能
    """判断命令是否危险（危险命令表匹配）。"""
    parts = command.strip().split()
    if not parts:
        return False
    prog = parts[0].lower()
    args = parts[1:]
    if prog not in DANGEROUS:
        return False
    if not DANGEROUS[prog]:  # 空元组 = 命令本身危险
        return True
    for arg in args:
        for d in DANGEROUS[prog]:
          if d in arg:      # 特征出现在参数里（子串），不是参数等于特征 (宽匹配)
              return True
    return False


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