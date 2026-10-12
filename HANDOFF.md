# HANDOFF

> 生成于 2026-10-12（会话交接用）。配合 `学习地图.md` 的「AI 接管协议」一起读。

项目目标：
亲手造一个 coding agent harness（Agent = LLM + Harness）——LLM 只做「下一步做什么」的决策，其余全是自写的工程。六维（决策/工具/记忆/治理/反馈/配置）都有能跑的最低实现，治理 + 反馈做深；**硬标准：移除真 LLM 后核心机制（工具分发/治理拦截/反馈回灌/记忆读写/停机）可用确定性单测验证**。技术栈：Python 后端 + TypeScript 前端（先终端后画面）。学习档位：「能迁移着做下一个」。

当前分支/commit：
master @ `b6ad200`（hooks 实时流拦截行）。本文件随其后一笔 docs 提交。

工作区状态：
干净（97 测试全绿）。

关键文件与入口：
- 代码（工程真源 = 同目录 `PLAN.md`）：
  - `src/agent_harness/loop.py` 主循环（三道理序：护栏 → 预钩子名单 → 执行；回调注入）
  - `src/agent_harness/llm.py` 模型适配层（OpenAI 兼容；流式；`ThinkSplitter` 剥 mm:think；reactive 413）
  - `src/agent_harness/hooks.py` 策略挂载点（S9：`Verdict` / `run_pre`（短路 + fail-closed）/ `run_post` + 随附策略 `forbid_env_writes`）
  - `src/agent_harness/trace.py` 轨迹记录层（S8：事件 schema + `timed` 上下文管理器 + 读回）
  - `src/agent_harness/session.py` 会话存档（JSONL；meta 行存**会话级模型**；`prune_empty_sessions`）
  - `src/agent_harness/assembly.py` 公共装配（tools 注册表 + `PRE_HOOKS` 名单 + `SwitchableLLM` 门面）
  - `src/agent_harness/settings.py` 配置（`~/.agent_harness.toml` 多 profile；`api_key` / `api_key_env`）
  - `src/agent_harness/tui.py` 终端界面；`server.py` 网页后端（FastAPI + WS）；`weblaunch.py` 网页端自启
  - `guardrail.py`（危险命令表 + 路径围栏）/ `sensors.py`（run_tests）/ `compact.py`（微压缩 + snip）/ `tools.py` / `schema.py` / `config.py`（AGENTS.md 注入）/ `messages.py`（Msg）
  - `web/` React 前端（Vite；WS 客户端；`/trace` overlay；模型/会话选择器；思考行）
- 学习侧（AI 负责落盘）：`C:\Users\ypchen\Documents\notes\build-to-learn\AgentHarness\`（`学习地图.md` + `学习记录/`）
- 接管用 skill：`C:\Users\ypchen\.claude\skills\build-to-learn\`（SKILL.md + references/）
- 入口命令：TUI `uv run agent-harness`；网页后端 `uv run agent-harness-web`（8000）；前端 `cd web && npm run dev`（5173；TUI 启动时会自动拉起两者）

已完成：
- S1–S5：主循环 / 反馈（run_tests 判红绿 + 失败回灌）/ 治理（危险表 + HITL 审批）/ 记忆（会话存档 + 压缩 + AGENTS.md 配置）/ 真 LLM 适配器
- S6a TUI、S6b Web（流式 / 审批跨进程 / 会话选择 / 思考行 / md 表格）
- S7 多模型（profiles + `/model` 运行时切换 + 私有参数三态默认不传 + fail-fast）
- S8 tracer（三处 emit → `<会话id>.trace.jsonl`；TUI `/trace` 总览 + 钻取；网页 overlay；静默自启网页端）
- S9 hooks（PreToolUse/PostToolUse；首个 deny 短路；异常默认拦截；`forbid_env_writes`；拦/放进 trace；**实时流拦截行**）
- 尾巴批次：会话级模型（meta 落盘、打开恢复、`--model` 优先）/ mm:think 转轨思考通道（含孤立闭标签）+ 缩进事故修复 / 空占位打扫 / 分页会话选择器 / 审批提示 Live 让位与残影修复

未完成：
- S6a–S9 的通关大考 / 学习记录成文化 / 能力库登记（用户「不考了」——**欠账，可随时补**）
- S10 候选：**子 agent（Isolate）**（`spawn_subagent` + depth 上限 + 受限工具集 + 摘要回灌）——阶梯上最后一个候选
- 已知边界（未收口）：护栏黑名单洞（裸 `del 文件` 不带 `/s|/q|/f` 不过闸——S3 遗留，实锤过）；shell 绕过 / OS 沙箱；网页审批真机未验（单测覆盖）；`/model` 列表未显示「profile → 实际模型名」

重要决策及原因：
- 主循环 / LLM 抽象层必须自实现（不建在框架上）；机制必须是代码不是提示词——规格硬要求
- 只接 OpenAI 兼容面；私有参数（thinking / reasoning_effort）三态、默认**不传**（跨厂商安全）
- 压缩 = 投影式微压缩（不改内存 history；占位替换保 tool_call 配对）；413 → 退避 + 加压 + 只重试一次
- trace 与 session 分家（两个读者：模型看 → 可压缩；人看 → 不可丢）；一动作一条、结果全文
- hooks：预钩子 `Verdict | None`、首个 deny 短路、异常 = 默认拦截（fail closed）；**内置护栏不可摘、排在名单前**（三道理序）
- 模型选择**跟着会话走**（meta 行落盘）；`--model` 显式指定优先并落账
- 细节与翻案记录在 `PLAN.md` 决策记录 + 各阶段学习记录

坑 / 禁忌 / 不能改的东西：
- **协作规矩（CLAUDE.md）**：禁止直接给完整实现（除非用户说「给参考实现」）；用户先交方案只评审（行+问题+为什么）；解释落 CPython 语义；结尾给验证问题；**git 提交不署 AI 名**
- **build-to-learn 流程**：手是用户的手（跑命令 / 看现象是用户的）；施工期零设问；**落盘（学习记录 / 学习地图 / PLAN）是 AI 的活，且排在回话之后**
- 协议红线：N 个 tool_calls → N 条 tool 回灌；assistant 带 tool_calls 必须原样重发（含 reasoning_content）；流解析必须容忍 `delta=None` / 空 choices / tool_calls 分片 / mm:think 标签被切断（含孤立闭标签）
- 密钥：**别把真 key 写进 `harness.example.toml`**（仓库内）；用户的 `~/.agent_harness.toml` 允许明文 `api_key`
- 测试不许污染真实存档（test_s6b 的 SESSION_ROOT 隔离 fixture 不能删）
- Windows 环境（shell = cmd.exe）；函数名英文 + 注释中文

复现命令：
- 全量测试：`uv run pytest -q`（97 条）
- 前端类型检查：`cd web && npx tsc --noEmit`
- TUI：`uv run agent-harness`（会自启网页端；非终端 / 管道模式不触发）
- 存档位置：`C:\Users\ypchen\.agent_harness_sessions\D--workspace-self-study-AgentHarness\`（`<id>.jsonl` + `<id>.trace.jsonl`）
- 配置：`C:\Users\ypchen\.agent_harness.toml`（profiles：deepseek-flash / deepseek-v4-pro / MiniMax-M3）

失败测试 / 错误栈：
无——97/97 全绿（最后运行 2026-10-12）。近期修过的三个真 bug（供参考）：流 `delta=null` 崩溃 / 「缩进错位把 tool_calls 块吞进 flush 循环」（已补回归测试）/ 审批提示与 Live 抢终端。

下一步计划（候选，按用户选择）：
1. **S10 子 agent（Isolate）**——阶梯上最后一档；或
2. 收尾欠账：S6a–S9 成文 / 大考 / 能力库；或
3. 已知边界挑一个收口（如护栏黑名单洞：裸 `del 文件` 不过闸）

验收标准：
- 每阶段：部件图所有部件完工 + 整体跑通 + **用户亲眼看过真机现象**；`uv run pytest` 全绿
- 硬标准：移除真 LLM 后核心机制可确定性单测验证
- 学习收口：用户对没教过的新需求能答「学会四问」（动哪个部件 / 照哪个形状 / 踩哪个坑 / 怎么验）

新会话必须先读哪些文件（按序）：
1. `C:\Users\ypchen\Documents\notes\build-to-learn\AgentHarness\学习地图.md`（**接管协议 + 「现在在哪」**——先按它接上）
2. 本文件 `D:\workspace\self-study\AgentHarness\HANDOFF.md`
3. `D:\workspace\self-study\AgentHarness\PLAN.md`（工程真源）
4. skill `C:\Users\ypchen\.claude\skills\build-to-learn\SKILL.md` + `references/`（按路由表；落盘规矩在 `4-笔记.md`）
5. 最近的阶段学习记录：`学习记录/S9 · 装插座.md`、`S8 · 留痕.md`
