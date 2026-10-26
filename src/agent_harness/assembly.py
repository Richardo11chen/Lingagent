"""装配层：公共装配（system prompt / 工作目录 / 工具注册表）——CLI 与 Web 服务共用。"""

from pathlib import Path

from .config import load_rules
from .guardrail import safe_tool
from .hooks import forbid_env_writes
from .sensors import run_tests
from .subagent import make_spawn_tool
from .tools import edit_file, read_file, run_shell, task_output, task_stop, write_file

# 写法原则：规则用自然语言写，别用「电报体」（"读用 X；改用 Y"）。
# 提示词的腔调会原样传染给回话——电报体喂进去，模型就用电报体答回来。
# 只留能改变具体动作的规则；工具细节交给 schema，项目规矩交给 AGENTS.md（拼在下面）。
SYSTEM_PROMPT = (
    "你是 Lingagent，一个 coding agent，工作目录是项目根。\n\n"
    "## 优先级\n"
    "用户当前指令 > AGENTS.md（若存在）> 这份提示词。"
    "冲突时按这个顺序取舍；只在确实冲突时点一句用了哪条，平时不用解释。\n\n"
    "## 工具\n"
    "优先用最窄的工具；具体用法、参数、风险和边界看工具 schema。"
    "只使用菜单里实际存在的工具，不要调用提示词提到但菜单里没有的工具。"
    "改完代码必须验证，没验证过不算完成。\n\n"
    "## 什么时候先问\n"
    "命令类操作按 run_shell 描述里的风险分级决定要不要先问；"
    "用户明确要求范围内的文件改动直接做。\n\n"
    "## 改动边界\n"
    "跟随项目现有的风格、依赖、模式。不加没必要的依赖。"
    "不主动 commit / push，除非用户要求。\n\n"
    "## 怎么说话\n"
    "直接说结论和判断，不铺垫、不客套。"
    "简单问题直接答，不用工具。"
    "有把握说把握，没把握说没把握，别编。"
    "做完就停，别为了显得卖力多调一个工具；没做完就说清卡在哪。"
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
