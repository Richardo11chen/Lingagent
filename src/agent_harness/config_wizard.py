"""`ah --init`：首启向导——问三句，生成 `~/.agent-harness/config.toml`。

为什么单独一个模块：`main()` 已经够长；而且它只在人第一次跑的时候用一次，
不该占着主入口的行数。写文件这件事必须可靠 + 可测（见 tests/test_config_wizard.py）。

安全边界：
- 已有配置**绝不覆盖**——要重来，让用户自己删（或改名备份），向导不碰已有文件。
- key 用 getpass 读（不回显），直接明文写入时给文件 0600（POSIX）。
- 任何异常都不半途写半个文件：先拼好字符串，再一次性 write。
"""

import os
import sys
from pathlib import Path

from .settings import CONFIG_PATH

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_KEY_ENV = "DEEPSEEK_API_KEY"


def _ask(prompt: str, default: str) -> str:
    raw = input(f"{prompt} [{default}]: ").strip()
    return raw or default


def _secret(prompt: str) -> str:
    """读 key。真人在终端里不回显（getpass）；管道/CI 退化成 input——getpass 在无 TTY 下会
    去开控制台，把整个进程挂住（`printf … | ah --init` 就会卡死）。"""
    if sys.stdin.isatty():
        try:
            import getpass

            return getpass.getpass(prompt) or ""
        except Exception:
            pass  # 拿不到控制台就退化成明读
    return input(prompt).strip()


def _toml_str(s: str) -> str:
    """最小 TOML 基本串转义——base_url / 模型名 / key 只要不炸引号反斜杠就够。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_config(name: str, base_url: str, key: str | None, key_env: str | None) -> str:
    lines = [
        "# Agent Harness 配置（由 `ah --init` 生成）",
        "",
        "[model]",
        "default = \"main\"",
        "",
        "[models.main]",
        f"name = {_toml_str(name)}",
        f"base_url = {_toml_str(base_url)}",
    ]
    if key:
        lines.append(f"api_key = {_toml_str(key)}")
    else:
        lines.append(f"api_key_env = {_toml_str(key_env or DEFAULT_KEY_ENV)}")
    return "\n".join(lines) + "\n"


def run_init() -> int:
    """问三句 → 写配置。返回进程退出码。"""
    if CONFIG_PATH.exists():
        print(f"已有配置：{CONFIG_PATH}")
        print("`ah --init` 不会覆盖它。想重新来过，先手动删掉（或改名备份）再跑一次。")
        return 0

    print("Agent Harness 首次配置（回车用默认值，Ctrl+C 随时退出、不会写任何东西）")
    try:
        base_url = _ask("模型 API 地址", DEFAULT_BASE_URL)
        name = _ask("模型名（发给 API 的 model 字段）", DEFAULT_MODEL)
        key = _secret(f"API key（直接粘贴会明文写入；留空 = 从环境变量 {DEFAULT_KEY_ENV} 读）: ")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消，什么都没写。")
        return 130

    cfg = _render_config(name, base_url, key or None, None if key else DEFAULT_KEY_ENV)
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(cfg, encoding="utf-8")
        if key and os.name != "nt":  # POSIX 上尽量 0600；Windows 走 ACL，这里不折腾
            os.chmod(CONFIG_PATH, 0o600)
    except OSError as e:
        print(f"写配置失败：{e}")
        return 1

    print(f"\n配置已写好：{CONFIG_PATH}")
    if key:
        print("（key 已明文写入，文件权限 0600。别把这个文件提交进版本库。）")
    else:
        print(f"（用环境变量读 key——记得设 {DEFAULT_KEY_ENV}，否则启动会报缺 key。）")
    print("\n现在在任意目录敲 `ah` 就能开工。")
    return 0


if __name__ == "__main__":
    sys.exit(run_init())
