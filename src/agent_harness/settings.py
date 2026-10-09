"""配置（配置维·运行时超参）：读 ~/.agent_harness.toml，缺省回落到默认值。

密钥不落盘——配置只写「从哪个环境变量读」（api_key_env）。
装配层加载后注入各组件（依赖注入：模块不读全局配置）。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path.home() / ".agent_harness.toml"


@dataclass
class Settings:
    # [model]
    model_name: str = "deepseek-v4-flash"
    model_base_url: str = "https://api.deepseek.com"
    model_api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 1.0
    thinking: bool = True
    reasoning_effort: str = "high"
    # [loop]
    max_steps: int = 20
    # [context]
    compact_threshold: int = 70_000
    keep_last: int = 5
    snip_keep_groups: int = 3
    # [server]
    server_host: str = "127.0.0.1"
    server_port: int = 8000


def load_settings(path: Path = CONFIG_PATH) -> Settings:
    """读配置文件；不存在或字段缺失 → 用默认值。"""
    s = Settings()
    if not path.exists():
        return s
    data = tomllib.loads(path.read_text(encoding="utf-8"))

    m = data.get("model", {})
    s.model_name = str(m.get("name", s.model_name))
    s.model_base_url = str(m.get("base_url", s.model_base_url))
    s.model_api_key_env = str(m.get("api_key_env", s.model_api_key_env))
    s.temperature = float(m.get("temperature", s.temperature))
    s.thinking = bool(m.get("thinking", s.thinking))
    eff = str(m.get("reasoning_effort", s.reasoning_effort))
    # 强度只管强度（开关归 thinking）——非法值回退 high（含 none）
    s.reasoning_effort = eff if eff in ("low", "high", "max") else "high"

    lp = data.get("loop", {})
    s.max_steps = int(lp.get("max_steps", s.max_steps))

    ctx = data.get("context", {})
    s.compact_threshold = int(ctx.get("compact_threshold", s.compact_threshold))
    s.keep_last = int(ctx.get("keep_last", s.keep_last))
    s.snip_keep_groups = int(ctx.get("snip_keep_groups", s.snip_keep_groups))

    sv = data.get("server", {})
    s.server_host = str(sv.get("host", s.server_host))
    s.server_port = int(sv.get("port", s.server_port))
    return s
