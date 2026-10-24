"""配置（配置维·运行时超参）：读 ~/.agent-harness/config.toml。

模型：多套 profile（[models.*] + [model].default）——只覆盖 OpenAI 兼容面
（换 name/base_url/api_key_env 即可接厂商或中转站）；老格式单个 [model] 节自动兼容。
密钥不落盘——只写「从哪个环境变量读」（api_key_env）。
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .paths import APP_DIR

# 默认 ~/.agent-harness/config.toml；AGENT_HARNESS_CONFIG 可覆盖（测试/多环境）
CONFIG_PATH = Path(os.environ.get("AGENT_HARNESS_CONFIG") or (APP_DIR / "config.toml"))
LEGACY_CONFIG_PATH = Path.home() / ".agent_harness.toml"  # 老位置（迁移用）


def _migrate_legacy_config(path: Path) -> None:
    """老位置的文件搬到新目录：只搬默认路径，自定义路径不碰，失败就算了。"""
    if path != CONFIG_PATH or path.exists() or not LEGACY_CONFIG_PATH.exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        LEGACY_CONFIG_PATH.rename(path)
    except OSError:
        pass  # 搬不动就继续用默认值——用户手动挪也行


@dataclass
class ModelProfile:
    """一套模型连接配置（OpenAI 兼容）。

    thinking / reasoning_effort 是 DeepSeek 私有参数——**省略或 "off" = 不传**
    （跨厂商安全：别的 OpenAI 兼容厂商不会被塞私有参数）。
    """

    name: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"
    api_key: str = ""  # 直写密钥（优先于 api_key_env；明文落盘——示例文件别提交进仓库）
    strip_think_tags: bool = False  # 正文里包 <mm:think>…</mm:think> 的网关才开（MiniMax 形态）
    temperature: float = 1.0
    thinking: str = "off"  # "enabled" / "disabled" / "off"（off = 不传）
    reasoning_effort: str = "off"  # "low" / "high" / "max" / "off"（off = 不传）


@dataclass
class Settings:
    default_model: str = "default"
    models: dict[str, ModelProfile] = field(default_factory=lambda: {"default": ModelProfile()})
    # 全局（与模型无关）
    max_steps: int = 40
    approval_timeout: float = 300.0  # 秒：没人答复就按驳回处理（0 = 一直等）
    compact_threshold: int = 70_000
    keep_last: int = 5
    snip_keep_groups: int = 3
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    web_autostart: bool = True  # 进 TUI 时自动把网页端（后端 + 前端 dev server）拉起来
    trace_enabled: bool = True  # 写轨迹文件（关掉 = 只留 session，省磁盘）
    # 人写的允许规则：`["git *", "uv *"]` = 这些命令不再问（前缀语义，灾难命令不受影响）
    guardrail_allow: list[str] = field(default_factory=list)

    def model(self, name: str | None = None) -> ModelProfile:
        """取 profile（指定名 或 default）；名字不存在回退 default。"""
        return self.models.get(name or self.default_model) or self.models[self.default_model]


def _profile_from(data: dict) -> ModelProfile:
    p = ModelProfile()
    p.name = str(data.get("name", p.name))
    p.base_url = str(data.get("base_url", p.base_url))
    p.api_key_env = str(data.get("api_key_env", p.api_key_env))
    p.api_key = str(data.get("api_key", p.api_key))
    p.temperature = float(data.get("temperature", p.temperature))
    p.strip_think_tags = bool(data.get("strip_think_tags", p.strip_think_tags))
    th = str(data.get("thinking", p.thinking)).lower()
    p.thinking = th if th in ("enabled", "disabled", "off") else "off"  # 非法回退 off（不传）
    eff = str(data.get("reasoning_effort", p.reasoning_effort)).lower()
    p.reasoning_effort = eff if eff in ("low", "high", "max", "off") else "off"
    return p


def load_settings(path: Path = CONFIG_PATH) -> Settings:
    """读配置文件；不存在或字段缺失 → 用默认值。"""
    _migrate_legacy_config(path)
    s = Settings()
    if not path.exists():
        return s
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:  # 语法错（漏引号等）——给一行干净报错，别甩 traceback
        raise SystemExit(f"配置文件语法错误：{path}\n{e}")

    # 模型 profiles：新格式 [models.*]；老格式 [model] 单个节自动兼容
    if "models" in data:
        s.models = {k: _profile_from(v) for k, v in data["models"].items()}
        s.default_model = str(data.get("model", {}).get("default", next(iter(s.models))))
    elif "model" in data:
        s.models = {"default": _profile_from(data["model"])}
        s.default_model = "default"

    lp = data.get("loop", {})
    s.max_steps = int(lp.get("max_steps", s.max_steps))
    s.approval_timeout = float(lp.get("approval_timeout", s.approval_timeout))

    ctx = data.get("context", {})
    s.compact_threshold = int(ctx.get("compact_threshold", s.compact_threshold))
    s.keep_last = int(ctx.get("keep_last", s.keep_last))
    s.snip_keep_groups = int(ctx.get("snip_keep_groups", s.snip_keep_groups))

    sv = data.get("server", {})
    s.server_host = str(sv.get("host", s.server_host))
    s.server_port = int(sv.get("port", s.server_port))

    wb = data.get("web", {})
    s.web_autostart = bool(wb.get("autostart", s.web_autostart))

    tr = data.get("trace", {})
    s.trace_enabled = bool(tr.get("enabled", s.trace_enabled))

    gr = data.get("guardrail", {})
    allow = gr.get("allow", s.guardrail_allow)
    if isinstance(allow, str):  # 允许写单条字符串
        allow = [allow]
    s.guardrail_allow = [str(x) for x in allow]

    if s.default_model not in s.models:  # default 指向不存在的 profile → 当场说清楚（别等 KeyError）
        raise SystemExit(
            f"配置错误：[model].default = \"{s.default_model}\" 不在 [models.*] 里"
            f"（可用：{', '.join(s.models)}）"
        )
    return s
