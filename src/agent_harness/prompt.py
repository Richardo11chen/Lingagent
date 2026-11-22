"""系统提示词：一段文案 + 把项目规则拼上去。

为什么单独一个文件，而不是塞在 `assembly.py` 里：
- 它是**内容**，不是装配。assembly 负责"把哪些东西接起来"，这里负责"接起来的这串话说什么"。
- 它是这个项目里改动最频繁的东西（每次发现模型行为不对，第一反应都是调这里）。
  混在装配文件里，改一句措辞就会让那个文件的 git 历史混进两类互不相干的改动。
- 它会长：多段、按 profile 分叉、可能要双语。留在一堆 import 中间只会越长越难找。

写提示词的判据：**不写它，会错在哪一步？答不上来就别加。**

加之前先看这句话该不该在这里——它有三个更合适的家，都比这里可靠：

| 想说的话 | 该去哪 | 为什么 |
|---|---|---|
| 工具怎么用、参数、风险、什么时候用 background | `schema.py` 的工具描述 | 每次请求都带着，而且贴着那个决定出现 |
| 这个项目的规矩（改完要验证 / 风格 / 别自动 commit） | 仓库根的 `AGENTS.md` | 用户能改；写死在这里等于对所有项目生效 |
| "能不能跑"这类判定 | 代码里的门禁（`guardrail` / 审批） | 机制拦得住，提示词只是请求 |

这里只剩一句身份。这不是偷懒：模型见过的 coding 任务比这份文件能描述的更全，
多写一句，只会稀释真正要紧的话。
"""

from pathlib import Path

from .config import load_rules

SYSTEM_PROMPT = (
    "你是运行在 lingagent（一个 coding agent harness）里的编码助手。"
    "帮用户查看文件、执行命令、修改代码。"
)


def build_system_prompt(workspace: Path) -> str:
    """基础提示词 + 项目规则（每次装配时拼最新）。

    每次都重读规则文件，不缓存：缓存它等于凭空多一条"改了 AGENTS.md 要重启"的隐性规矩，
    而提示词本来就是当轮请求的一部分。
    """
    rules = load_rules(workspace)
    return SYSTEM_PROMPT + "\n\n" + rules if rules else SYSTEM_PROMPT
