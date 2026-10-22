# HANDOFF

> 生成于 2026-10-19（会话交接用；前版 2026-10-12）。配合 `学习地图.md` 的「AI 接管协议」一起读。

项目目标：
亲手造一个 coding agent harness（Agent = LLM + Harness）——LLM 只做「下一步做什么」的决策，其余全是自写的工程。六维（决策/工具/记忆/治理/反馈/配置）都有能跑的最低实现，治理 + 反馈做深；**硬标准：移除真 LLM 后核心机制（工具分发/治理拦截/反馈回灌/记忆读写/停机）可用确定性单测验证**。技术栈：Python 后端 + TypeScript 前端（先终端后画面）。学习档位：「能迁移着做下一个」。

当前分支/commit：
master。本轮已分笔提交（S10 子 agent / run_shell 超时 / S11 风险分级 / 空设备重定向修复 / docs），详见 `git log --oneline`。

工作区状态：
除 `CLAUDE.md` 有一处未提交改动外（2026-10-19 13:20 被清空，非本轮代码所改，待用户确认），工作区干净；117 测试全绿（2026-10-19）。

关键文件与入口：
- 代码（工程真源 = 同目录 `PLAN.md`）：
  - `src/agent_harness/loop.py` 主循环（三道理序：护栏 → 预钩子名单 → 执行；S11 风险判定接入；每步带 `system_prompt`）
  - `src/agent_harness/guardrail.py` 治理判定（S11：`classify_command` 命令风险分级）+ 路径围栏
  - `src/agent_harness/subagent.py` S10 子 agent（`make_spawn_tool`：递归 + depth 1 + 固定模板摘要 + trace `sub` 分层）
  - `src/agent_harness/hooks.py` 策略挂载点（S9：Verdict / run_pre 短路与 fail-closed / run_post）
  - `src/agent_harness/trace.py` 轨迹记录层（S8）；`session.py` 会话存档（meta 行存会话级模型）
  - `src/agent_harness/assembly.py` 公共装配（tools / PRE_HOOKS / SwitchableLLM / `build_session_tools`）
  - `src/agent_harness/settings.py` 多 profile 配置；`tui.py` / `server.py` / `weblaunch.py`；`web/` React 前端
  - `tools.py`（`run_shell` 120s 超时 + Windows `taskkill /T` 树杀；`safe_tool` 路径围栏）/ `schema.py`（工具菜单 = 注册表）/ `compact.py` / `config.py` / `messages.py` / `sensors.py`

- 入口命令：TUI `uv run agent-harness`；网页后端 `uv run agent-harness-web`（8000）；前端 `cd web && npm run dev`（5173；TUI 启动时会自动拉起两者）

已完成：
- S1–S5：主循环 / 反馈 / 治理（危险表 + HITL）/ 记忆 / 真 LLM 适配器
- S6a TUI、S6b Web、S7 多模型、S8 tracer、S9 hooks
- S10 子 agent「分身」：`spawn_subagent`（上下文隔离、depth 1、摘要模板、治理同一套、trace 分层；工具全量 + 提示词默认只读）
- S11 命令风险分级「换锁」：黑名单 → 只读放行 / 非只读与未知问人 / 极危险直拒；`run_shell` 与 approval 事件记 `risk`
- 尾巴批次：会话级模型 / mm:think 转轨 / 空占位打扫 / 分页会话选择器 / 实时流拦截行 / 两 bug 修复（`system_prompt` 死参数、`run_shell` 无超时）
- 2026-10-22 收尾清扫（审查后按 D/E 清单修）：
  - `README.md` 重写（原来只有 10 字节）；`memory.py`（取消的提取器残留、且语法错误）、`demo.py`、`test_demo.py`、`web/src/assets/*` 删除
  - 结果长度分流：`loop.py` 把工具结果**全文**给 trace、`cap_result` 截断到 32k 给 history/session（一次 `find /c` 曾灌出 91.8MB）
  - 读侧护栏：`trace.load_trace` 跳过 >2MB 单条事件、`tui.show_trace_event` 单字段截 4k、网页 trace 单字段截 2k
  - `llm.py`：`_parse_args` 容错（坏 JSON 不再穿透 REPL，改由 loop 拒绝执行并回灌）、请求超时 120s、连接异常包成 `LLMError`
  - 双端失败兜底：TUI 每轮 try/except（Ctrl+C 只中断本轮）、网页推 `error` 事件 + WS 自动重连 + `VITE_WS_PORT`
  - `server.main` 前置校验密钥；`sensors.run_tests` 改用 `sys.executable -m pytest`；`wcwidth` 写进依赖
  - 存档布局迁移（用户批准）：`session.py` 改为 `~/.agent_harness_sessions/<项目名>-<身份前8位>/`，身份 = 工作目录里的 `.agent-harness-id`（uuid，已 gitignore）→ **项目改名/移动不再丢历史**；老的路径派生目录首次访问自动搬过来（本机已迁：53 个会话 → `AgentHarness-d619b9f5`）；新增 3 条测试覆盖身份稳定 / 改名后仍可读 / 旧目录迁移
  - 审批带判定原因（用户批准，改接口）：`approver` 签名 `(command, reason)`；TUI 先印「⚠ 未知命令：find」再印命令全文，网页审批卡改成 原因 / 命令 / 按钮 三行；`loop` 把 `risk.reason` 递出去；新增 1 条测试 + 8 处测试回调跟着改签名
  - 体验一致性批次（审美审查后）：`show_note` 分 info/warn/error 三档（失败终于不是洋红色）；TUI 与网页共用 `TRACE_FIELD_CHARS` + `ellipsis_note`（同一句截断说明），工具详情两端都截 50；网页错误改独立 `.err-line`（左线 + 红字）；等第一个字时两端都有「⋯」；网页底部加 `/model` `/trace` 一行提示；断线清残留审批卡；网页加「停止」（`loop.should_stop` 每步检查 + `interrupt` 消息，正在跑的那一步会跑完）；`/model` 输入非法时不再静默吞

未完成：

- 已知边界（未收口）：OS 级沙箱 / shell 绕过；子 agent 中间过程不进实时流（只进 trace；用户拍板不做）；子 agent 只读靠提示词（用户拍板）；**网页审批真机已验证**（用户，2026-10-19）；**审批无超时 = 有意设计**（人说了算，不自动驳回；用户拍板不修）；流式 chunk 未节流；微压缩占位文案可加恢复指引；`/model` 列表未显示「profile → 实际模型名」

重要决策及原因：
- 主循环 / LLM 抽象层必须自实现；机制必须是代码不是提示词（规格硬要求）
- 只接 OpenAI 兼容面；私有参数三态、默认不传；压缩 = 投影式微压缩
- trace 与 session 分家（两个读者）；hooks 三道理序、首个 deny 短路、异常默认拦截
- S10：子 agent = 上下文隔离（Isolate）；深度 1；固定模板摘要；治理同一套；**全量工具 + 只读靠提示词**（用户改判，翻案记档）
- S11：命令风险分级——只读放行 / 非只读与未知问人（fail closed）/ 极危险直拒；复合命令按最严段算
- 细节与翻案记录在 `PLAN.md` 决策记录 + 各阶段学习记录

坑 / 禁忌 / 不能改的东西：
- **git 提交不署 AI 名**
- 协议红线：N 个 tool_calls → N 条 tool 回灌；assistant 带 tool_calls 必须原样重发（含 reasoning_content）；流解析容忍 `delta=None` / 空 choices / tool_calls 分片 / mm:think 标签被切断
- 密钥：**别把真 key 写进 `harness.example.toml`**；用户的 `~/.agent_harness.toml` 允许明文 `api_key`
- 测试不许污染真实存档（test_s6b 的 SESSION_ROOT 隔离 fixture 不能删）
- Windows 环境（shell = cmd.exe）；函数名英文 + 注释中文
- S11 教训：判定失败方向必须朝安全侧（判不出 = 问人）；`2>&1` 先剔除；白名单按能力面判（find / sed / awk / xargs / tee / sort -o 不收）
- S10 教训：`system_prompt` 曾是死参数（接口参数存在 ≠ 生效）；Windows 上 shell 超时要 `taskkill /T` 杀树

复现命令：
- 全量测试：`uv run pytest -q`（117 条）
- 前端类型检查：`cd web && npx tsc --noEmit`
- TUI：`uv run agent-harness`（会自启网页端；非终端 / 管道模式不触发）
- 存档位置：`C:\Users\ypchen\.agent_harness_sessions\D--workspace-self-study-AgentHarness\`（`<id>.jsonl` + `<id>.trace.jsonl`）
- 配置：`C:\Users\ypchen\.agent_harness.toml`（profiles：deepseek-flash / deepseek-v4-pro / MiniMax-M3）

失败测试 / 错误栈：
无——117/117 全绿（最后运行 2026-10-19）。

项目状态：**工程侧收口（2026-10-19）**——S1–S11 全部跑通·成文✓、117 测试全绿、PLAN 四条剧本真机通过、网页审批已验证。

可选余项（不阻塞）：
1. 小件：微压缩占位文案加恢复指引 / 流式 chunk 节流 / `/model` 列表显示 profile → 实际模型名

验收标准：
- 每阶段：部件图所有部件完工 + 整体跑通 + **用户亲眼看过真机现象**；`uv run pytest` 全绿
- 硬标准：移除真 LLM 后核心机制可确定性单测验证
- 学习收口：用户对没教过的新需求能答「学会四问」（动哪个部件 / 照哪个形状 / 踩哪个坑 / 怎么验）

新会话必须先读哪些文件（按序）：

1. `D:\workspace\self-study\AgentHarness\CLAUDE.md`（协作规矩）
2. 本文件 `D:\workspace\self-study\AgentHarness\HANDOFF.md`
3. `D:\workspace\self-study\AgentHarness\PLAN.md`（工程真源）

