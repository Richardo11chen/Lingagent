from dataclasses import dataclass, field

@dataclass
class Action:
    type: str
    tool: str = ""
    args: dict = field(default_factory=dict)

class LLM: # ScriptedLLM
    """假模型：按预设剧本依次返回动作，演完自动发 done。"""

    def __init__(self, script: list[Action]):
        self._script = list(script)

    def __call__(self, context, menu):
        if self._script:
            action = self._script.pop(0)
            return (f"(假模型)下一步：{action.type} {action.tool}", action)

        return ("任务完成。",Action(type="done"))