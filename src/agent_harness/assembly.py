"""装配层：公共装配（system prompt / 工作目录 / 工具注册表）——CLI 与 Web 服务共用。"""

from pathlib import Path

from .config import load_rules
from .guardrail import safe_tool
from .sensors import run_tests
from .tools import count_lines, edit_file, read_file, run_shell, write_file

SYSTEM_PROMPT = (
    "你是 agent-harness，一台会自己干活的 coding agent，工作目录是项目根。\n\n"
    "不必理会项目根目录下的CLAUDE.md 和 README.md。\n\n"
    "运行环境：Windows（shell 是 cmd.exe）——用本平台原生命令。\n\n"
    "工具使用策略：\n"
    "- 读代码/文件用 read_file。\n"
    "- 修改现有文件优先用 edit_file（old→new 精确替换，保留其余内容）；只有新建文件或整体重写才用 write_file（⚠️ 覆盖整个文件，会丢失未包含的内容）。\n"
    "- 改完代码用 run_tests 验证：红了看报告改，绿了才收工——不要没验证就宣称完成。\n"
    "- 跑其他命令用 run_shell；危险命令会被拦截并询问用户。\n"
    "- 只能在项目目录内活动，越界路径会被拦截。\n\n"
    "任务完成后直接回复总结（不再调用工具）。"
)

WORKSPACE = Path.cwd()  # 工作目录 = 项目根

tools = {
    "read_file": safe_tool(read_file, WORKSPACE),
    "write_file": safe_tool(write_file, WORKSPACE),
    "edit_file": safe_tool(edit_file, WORKSPACE),
    "count_lines": safe_tool(count_lines, WORKSPACE),
    "run_shell": run_shell,  # shell 不包——命令危险面归 H 护栏管
    "run_tests": run_tests,  # pytest 判红绿
}


def build_system_prompt() -> str:
    """基础 prompt + 规则文件（每次装配时拼最新）。"""
    return SYSTEM_PROMPT + "\n\n" + load_rules(WORKSPACE)


def list_visible_sessions() -> list[dict]:
    """列可展示的历史会话（跳过没聊过的空会话占位）——CLI 与 Web 共用。"""
    from .session import list_sessions

    return [s for s in list_sessions(WORKSPACE) if s["first_user_msg"]]


def load_settings_and_llm(system_prompt: str):
    """按 ~/.agent_harness.toml 配置构造真模型适配器——返回 (llm, settings)。"""
    from .llm import DeepSeekLLM
    from .settings import load_settings

    s = load_settings()
    llm = DeepSeekLLM(
        system_prompt,
        model=s.model_name,
        base_url=s.model_base_url,
        api_key_env=s.model_api_key_env,
        temperature=s.temperature,
        compact_threshold=s.compact_threshold,
        keep_last=s.keep_last,
        snip_keep_groups=s.snip_keep_groups,
        thinking=s.thinking,
        reasoning_effort=s.reasoning_effort,
    )
    return llm, s
