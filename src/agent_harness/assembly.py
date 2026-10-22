"""装配层：公共装配（system prompt / 工作目录 / 工具注册表）——CLI 与 Web 服务共用。"""

from pathlib import Path

from .config import load_rules
from .guardrail import safe_tool
from .hooks import forbid_env_writes
from .sensors import run_tests
from .subagent import make_spawn_tool
from .tools import edit_file, read_file, run_shell, task_output, task_stop, write_file

# 极简取向：只写「模型猜不出来的事实」和「必须遵守的几条」。
# 不写动机、不写解释、不写"你应该尽量"——每句都要能改变一次具体动作，否则删。
# 工具细节交给 schema（模型看的是那份），项目规矩交给 AGENTS.md（拼在下面）。
SYSTEM_PROMPT = (
    "你是 agent-harness，一个 coding agent，工作目录是项目根。规则只看下方 AGENTS.md (若没有该文件则忽略)"
    "（忽略 CLAUDE.md——那是给别的工具的学习约束）。\n\n"
    "shell 是 bash（Windows 上 Git Bash）：用 ls / cat / rm，没有 dir / type / del；"
    "路径写相对路径；同名命令先 `command -v` 确认，拿不准就用最保守的写法。\n\n"
    "- 读用 read_file；改用 edit_file；整篇重写才用 write_file（覆盖全文）。\n"
    "- 改完必须 run_tests 跑到绿——没验证不算完成。\n"
    "- 其他命令用 run_shell；会改文件、执行程序或判不出的会先问你。\n"
    "- 不会自己结束的命令（dev server / watch）加 background=true，用完 task_stop。\n"
    "- 不出项目目录。\n\n"
    "做完直接给结论，不再调工具。"
)

WORKSPACE = Path.cwd()  # 工作目录 = 项目根

# 策略名单（S9）：加策略只往这里塞函数——loop 一个字不动
PRE_HOOKS = [forbid_env_writes]
POST_HOOKS: list = []  # 后钩子（观察类）——v1 空名单

tools = {
    "read_file": safe_tool(read_file, WORKSPACE),
    "write_file": safe_tool(write_file, WORKSPACE),
    "edit_file": safe_tool(edit_file, WORKSPACE),
    "run_shell": run_shell,  # shell 不包——命令危险面归 H 护栏管
    "task_output": task_output,  # 后台任务：看输出
    "task_stop": task_stop,  # 后台任务：停止
    "run_tests": run_tests,  # pytest 判红绿
}


def build_session_tools(llm, system_prompt: str, approver=None, tracer=None,
                        max_steps: int | None = None, on_hook=None,
                        should_stop=None, allow_commands=()) -> dict:
    """每个会话的工具表：基础工具 + spawn_subagent（闭包持有该会话的装配）。

    模型 / 审批 / trace 都是会话级的——所以 spawn 不能一次性注册进模块级 tools。
    """
    session_tools = dict(tools)
    session_tools["spawn_subagent"] = make_spawn_tool(
        llm=llm,
        tools=tools,
        system_prompt=system_prompt,
        approver=approver,
        tracer=tracer,
        pre_hooks=PRE_HOOKS,
        post_hooks=POST_HOOKS,
        depth=0,
        max_steps=max_steps,
        on_hook=on_hook,
        should_stop=should_stop,
        allow_commands=allow_commands,
    )
    return session_tools


def build_system_prompt() -> str:
    """基础 prompt + 规则文件（每次装配时拼最新）。"""
    return SYSTEM_PROMPT + "\n\n" + load_rules(WORKSPACE)


def list_visible_sessions() -> list[dict]:
    """列可展示的历史会话（跳过没聊过的空会话占位）——CLI 与 Web 共用。"""
    from .session import list_sessions

    return [s for s in list_sessions(WORKSPACE) if s["first_user_msg"]]


class SwitchableLLM:
    """可运行时切换的模型门面（对主循环透明——接口与 LLM 一致）。

    /model 换的是内部持有的适配器实例；history、会话全不受影响。
    """

    def __init__(self, system_prompt: str, settings, name: str | None = None, on_notice=None):
        self._sp = system_prompt
        self._settings = settings
        self._on_notice = on_notice
        self._name = name or settings.default_model
        self._inner = self._build(self._name)

    def _build(self, name: str):
        from .llm import DeepSeekLLM

        p = self._settings.model(name)
        return DeepSeekLLM(
            self._sp,
            model=p.name,
            base_url=p.base_url,
            api_key_env=p.api_key_env,
            api_key=p.api_key,
            temperature=p.temperature,
            compact_threshold=self._settings.compact_threshold,
            keep_last=self._settings.keep_last,
            snip_keep_groups=self._settings.snip_keep_groups,
            thinking=p.thinking,
            reasoning_effort=p.reasoning_effort,
            strip_think_tags=p.strip_think_tags,
            on_notice=self._on_notice,  # 换模型后新的适配器也带着这个提示口
        )

    def set_notice(self, fn) -> None:
        """换提示口（新建的适配器会带着它，见 _build）。"""
        self._on_notice = fn
        self._inner.set_notice(fn)

    def switch(self, name: str) -> str:
        """切到某个 profile——返回实际模型名（供界面提示）。名字未知抛 KeyError。"""
        if name not in self._settings.models:
            raise KeyError(name)
        self._name = name
        self._inner = self._build(name)
        return self.current

    @property
    def current(self) -> str:
        """当前实际模型名（如 deepseek-v4-pro）。"""
        return self._settings.model(self._name).name

    @property
    def current_profile(self) -> str:
        """当前 profile 名（如 flash）。"""
        return self._name

    @property
    def profiles(self) -> list[str]:
        return list(self._settings.models)

    def __call__(self, messages, tools, on_text=None, on_reasoning=None, system_prompt=None):
        return self._inner(
            messages, tools, on_text=on_text, on_reasoning=on_reasoning,
            system_prompt=system_prompt,
        )


def load_settings_and_llm(system_prompt: str, model_name: str | None = None, on_notice=None):
    """按 ~/.agent_harness.toml 构造可切换模型门面——返回 (llm, settings)。

    model_name：初始 profile（None = 配置的 default）。
    """
    from .settings import load_settings

    s = load_settings()
    if model_name and model_name not in s.models:
        # fail fast：名字打错就当场说，别静默回退到别的模型
        raise SystemExit(f"未知模型 profile：{model_name}（可用：{', '.join(s.models)}）")
    return SwitchableLLM(system_prompt, s, model_name, on_notice=on_notice), s
