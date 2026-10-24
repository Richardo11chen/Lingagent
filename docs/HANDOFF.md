# HANDOFF

> 生成于 2026-10-19（会话交接用；前版 2026-10-12）。配合 `学习地图.md` 的「AI 接管协议」一起读。

项目目标：
亲手造一个 coding agent harness（Agent = LLM + Harness）——LLM 只做「下一步做什么」的决策，其余全是自写的工程。六维（决策/工具/记忆/治理/反馈/配置）都有能跑的最低实现，治理 + 反馈做深；**硬标准：移除真 LLM 后核心机制（工具分发/治理拦截/反馈回灌/记忆读写/停机）可用确定性单测验证**。技术栈：Python 后端 + TypeScript 前端（先终端后画面）。学习档位：「能迁移着做下一个」。

当前分支/commit：
master。本轮已分笔提交（S10 子 agent / run_shell 超时 / S11 风险分级 / 空设备重定向修复 / docs），详见 `git log --oneline`。

工作区状态：
除 `CLAUDE.md` 有一处未提交改动外（2026-10-19 13:20 被清空，非本轮代码所改，待用户确认），工作区干净；117 测试全绿（2026-10-19）。

关键文件与入口：
- 代码（工程真源 = `docs/PLAN.md`）：
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
  - 存档布局迁移（用户批准）：`session.py` 改为 `~/.agent-harness/sessions/<项目名>-<身份前8位>/`，身份 = 工作目录里的 `.agent-harness-id`（uuid，已 gitignore）→ **项目改名/移动不再丢历史**；老的路径派生目录首次访问自动搬过来（本机已迁：53 个会话 → `AgentHarness-d619b9f5`）；新增 3 条测试覆盖身份稳定 / 改名后仍可读 / 旧目录迁移
  - 审批带判定原因（用户批准，改接口）：`approver` 签名 `(command, reason)`；TUI 先印「⚠ 未知命令：find」再印命令全文，网页审批卡改成 原因 / 命令 / 按钮 三行；`loop` 把 `risk.reason` 递出去；新增 1 条测试 + 8 处测试回调跟着改签名
  - 简化批次（用户第二轮审美意见）：
    - 删 `count_lines` 工具（`read_file` 就够，多一个菜单项只添乱）；连带 schema/装配/3 个测试
    - 根目录分类：`PLAN.md` / `HANDOFF.md` → `docs/`（README/AGENTS/CLAUDE 必须留根：pyproject 与规则注入要读）
    - `max_steps` 默认 20 → 40（20 个「模型回合」真活不够用），停机文案改成可操作的「想接着做就再发一句」
    - 配置与会话收进 `~/.agent-harness/`（`config.toml` + `sessions/`），老位置首次运行自动迁移；只搬默认路径，测试/自定义根不碰
    - 退出：`Ctrl+C` 两次或 `Ctrl+D`，都不打 traceback；banner 不再教人敲 exit
    - 输入 `/` 自动弹命令菜单（`prompt_toolkit` completer，`/model` `/trace`）
    - `list_sessions` 不再把 `*.trace.jsonl` 当会话（数字终于对得上：46 会话 / 7 轨迹）
  - 与 Pi 对齐批次（用户逐条拍板）：
    - `ThinkSplitter` 变成 profile 开关 `strip_think_tags`（只有 MiniMax 开）——别家不在每条流上付这份延迟
    - 工具菜单/注册表加了守门测试（`test_tool_menu_matches_registry`），漏一边直接红
    - 补四条"不补就回不来"的：`load_session` 自愈悬空 tool_calls（否则恢复后 400 打不开）、停止信号透传子 agent、`read_file`/`edit_file` 认 GBK/UTF-8-sig、WS 断开时释放审批线程
    - 审批改为**超时驳回**（`[loop] approval_timeout`，默认 300s，0 = 一直等），TUI 用 `pre_run` 定时 `app.exit`，网页用 `Event.wait(timeout)` + `notice` 事件
    - 系统提示**不再断言环境**（"shell 是 cmd.exe" 是那次 91MB 的根）→ 改成"先 `ver` / `where` 确认，同名程序可能是两个东西"
    - 轨迹可清理：`--clean-trace DAYS`；`[trace] enabled` 一键关；`/trace` 记查看时间，30 天没看启动时提醒一次
    - 压缩会出声：`llm.py` 加 `on_notice(text, **fields)`（构造参数，不变量协议），真压了才提示（没压不吵）；413 重试也提示。TUI 用 `⟳` 一行插入流里，网页复用 `notice` 事件；同时写一条 `kind: compact` 的 trace（`reason` / `before` / `after`），`/trace` 表格显示「81234 → 21005 字符」
    - A+ 审批模型（2026-10-22 晚）：`[guardrail] allow` 前缀规则（`git *`）+ 审批三键（Enter 批准 / Esc 驳回 / `a` 记住本会话）+ 网页第三个按钮。`classify_command(cmd, allow)` 顺序是**灾难 > 规则 > 分类**；会话内记忆按 `command_head()`（跳过语法词）归类，只在内存里。`show_approval` 返回值从原始输入改成 `approve/deny/always`
    - 护栏精度：`for / do / in` 等语法词先剥再分类（不能整段丢弃，否则 `do @del %f` 的 del 被吞）；程序位含 `*`/`?` 或以 `%`/`$` 开头视为残片；`find` 默认只读、带 `-delete/-exec/-ok/-fprint/-fls` 才升档
    - **换 shell（2026-10-22 晚，用户拍板）**：新增 `shell.py`，顺序 bash（Git Bash）→ pwsh 7 → pwsh 5 → cmd 兜底；跳过 `System32\bash.exe`（WSL 启动器）；`AGENT_HARNESS_SHELL` 可覆盖。`run_shell` 不再用 `shell=True`，改为 `[shell, 旗标, command]`；横幅显示当前 shell
    - 换 shell 的连带改动：护栏加 `;` 分隔符（bash 的 `echo a; rm -rf x`，漏掉就是 fail open）；`MUTATING_PROGRAMS` 补 `cp/mv/touch/ln/chmod/chown/tee/truncate/rsync/install/unlink`；`sed` 带 `-i` 才算改文件；系统提示改写成 bash 语法（`ls/cat/rm`，`command -v`）
    - `run_shell` 也补了 timeout 参数（schema 暴露，默认 120 上限 600）与三处输出修复（stderr 合并 / 字节自解码 / stdin=DEVNULL）
    - 顺带修一个真 hang：`run_user` 里任何没被接住的异常（比如假模型没有 `set_notice`）会让前端永远等 `assistant_done`——现在给 task 挂了兜底回调，异常一律变成 `error` 事件
  - 后台任务批次（2026-10-22 晚，对齐 Claude Code 的 `run_in_background`）：
    - `run_shell(background=true)` 立刻返回任务 id（实测 0.03s），输出重定向到 `~/.agent-harness/tasks/<时间戳>-tN.log`，不进内存（日志能刷几小时）
    - 新增 `task_output(task_id)`：状态（运行中/已结束+退出码）+ 末尾 64KB（按行对齐读取，避免从半个字符开始解）
    - 新增 `task_stop(task_id)`：杀整棵进程树；登记簿保留记录，停完还能回看最后的输出
    - `CREATE_NEW_PROCESS_GROUP` / `start_new_session`：TUI 里 Ctrl+C 只中断这一轮，不连坐 dev server；`atexit` 统一收尸
    - `tasks.py` 只做登记（id/进程/路径/时间），启动与杀树留在 `tools.py`（避免和它互相 import）
    - 测试 3 条：立刻返回且有输出 / 停止后保留末段输出 / 未知 id 有明确提示
    - 超限结果落盘（用户选 B）：`tools.cap_result` 把被省略的全文写到工作目录 `.agent-harness-out/`（gitignore，7 天自动清），提示里给出路径 + `sed -n` 示例；原来只说"用带范围的命令重新取"，等于让模型把最贵的命令（全量 grep / 构建日志）再跑一遍。放工作目录、不放 `~/.agent-harness`，是为了不放松 `read_file` 的路径围栏（那是唯一的真边界）；代价是仓库里多一个隐藏目录
  - 自动批准（用户要的"auto 模式"，2026-10-22 晚）：
    - `Shift+Tab` 切换（`tui._auto`，只在内存，重启回手动）；toolbar 平时是灰横线 + `Shift+Tab 自动批准`，开着时整条变黄 + `⚡ 自动批准 · Shift+Tab 关闭`——状态必须一直看得见
    - 键绑定用 `~has_completions` 过滤：补全菜单开着时 Shift+Tab 是"往回选"，不抢那个键；自定义绑定在 `merge_key_bindings` 里排在最后，`key_processor` 取 `matches[-1]`，所以优先于默认的 `menu-complete-backward`
    - `show_approval` 命中 auto 时不弹提示、不动 Live，返回 `"auto"`；`approver` 据此写一条 `kind: auto` 的 trace（TUI 表格与网页都渲染成 `⚡ 自动批准`）
    - **`catastrophic` 碰不到这条路**：`loop` 在执行前就直拒，连审批都不进（有测试 `test_loop_catastrophic_blocked_without_asking` 守着）
    - 顺手把"往流里插一行"的四份重复收成 `tui._stream_line`（工具行 / 提示 / 策略拦截 / 自动批准）
    - 审批结果改成白名单 `_is_approved()`（`approve` / `always` / `auto`）：原来是 `decision != "deny"`，属 fail-open——将来 `show_approval` 多返回一种结局（超时 / 界面出错）就会被当成批准。现在未知值一律驳回，有测试守着
  - `run_tests` 不再写死 pytest（用户问"只对 py 项目有用要不要留"，答：留，但泛化）：
    - `sensors.detect_runner()` 按项目根标记认入口：pyproject/pytest.ini/tox.ini/setup.cfg → pytest；package.json 且**有 `scripts.test`** → npm；go.mod → go；Cargo.toml → cargo；Makefile 且有 `test:` → make
    - 认不出就明说"没找到测试入口，用 run_shell 跑本项目的命令"——不伪造成一条红的测试报告（原来在非 Python 项目上会跑 pytest 然后报红，等于骗人）
    - pytest 保留结构化提取（失败清单 + 详情）；其余 runner 格式各异，只给尾部 20 行——所有框架都把结论放最后
    - pytest 退出码 5（没收集到测试）单独报"没有测试"，不算失败
    - 顺手修同一个坑：不再用 `text=True`，改抓字节走 `_decode`（子进程按自己编码写，父进程按 locale 解会乱码）
    - 顺手删掉 `loop.py` 里没人用的 `run_sensors` 导入
  - 体验一致性批次（审美审查后）：`show_note` 分 info/warn/error 三档（失败终于不是洋红色）；TUI 与网页共用 `TRACE_FIELD_CHARS` + `ellipsis_note`（同一句截断说明），工具详情两端都截 50；网页错误改独立 `.err-line`（左线 + 红字）；等第一个字时两端都有「⋯」；网页底部加 `/model` `/trace` 一行提示；断线清残留审批卡；网页加「停止」（`loop.should_stop` 每步检查 + `interrupt` 消息，正在跑的那一步会跑完）；`/model` 输入非法时不再静默吞

  - 网页前端重做（2026-10-24，参照 dsh-transition-webui 的实测尺寸与 token，不搬它的脚手架）：
    - 四块骨架：side(260/56) · main(topbar + timeline + composer + 底部抽屉) · details(320)；`styles/tokens.css` 收敛掉约 40 处硬编码色值，深色跟 `prefers-color-scheme`
    - assistant 去掉气泡（只有 user 是 pill）；工具调用是可折叠行；会话列表进左栏、trace 进底部抽屉
    - 修两个只有真机/真浏览器才看得出的：①会话列表只在「没有 sid」时请求，错过一次就永久空——改成每次连接都问 + 每轮结束刷一次；②`.session-list` 是列向 flex，41 条时条目被 `flex-shrink` 压到 15px、标题塌成 0 高——加 `flex: none`
    - 会话行只留标题（原来第二行印完整 id，41 条纯噪音）；顶栏时间来自会话 id 的日期前缀，写「10-24 15:08 开始」，不带「开始」会被当成当前时钟
  - 工具结果接回前端（网页第一次能看见"它改了什么"）：
    - `loop.agent_loop(..., on_tool_result=fn)`：工具结束后回一次 `{name, status, ms, result, diff}`
    - diff 由写文件的工具自己算（`tools._record_diff` → `difflib.unified_diff`），用 `ContextVar` 传给 loop——不放返回值（那会进 history 和 token），也不放模块全局（网页多连接会串）
    - `server` 转成 `tool_result` 事件（结果截 8k、diff 截 12k，全文仍在 trace），前端按最后一个同名未回填的行就地更新：折叠态显示 `+2 −1` 与状态徽章，展开看 diff，右侧详情看全文
    - 验证方式：假 WS 服务重放「工具开始 → 结束（带 diff）」，无头 Edge + CDP 截图确认（不烧模型额度）

未完成：

- 已知边界（未收口）：OS 级沙箱 / shell 绕过；子 agent 中间过程不进实时流（只进 trace；用户拍板不做）；子 agent 只读靠提示词（用户拍板）；**网页审批真机已验证**（用户，2026-10-19）；**审批默认 300 秒超时驳回**（`[loop] approval_timeout`，0 = 一直等；原「不设超时」已于 2026-10-22 翻案）；流式 chunk 未节流；微压缩占位文案可加恢复指引；`/model` 列表未显示「profile → 实际模型名」

重要决策及原因：
- 主循环 / LLM 抽象层必须自实现；机制必须是代码不是提示词（规格硬要求）
- 只接 OpenAI 兼容面；私有参数三态、默认不传；压缩 = 投影式微压缩
- trace 与 session 分家（两个读者）；hooks 三道理序、首个 deny 短路、异常默认拦截
- S10：子 agent = 上下文隔离（Isolate）；深度 1；固定模板摘要；治理同一套；**全量工具 + 只读靠提示词**（用户改判，翻案记档）
- S11：命令风险分级——只读放行 / 非只读与未知问人（fail closed）/ 极危险直拒；复合命令按最严段算
- 细节与翻案记录在 `docs/PLAN.md` 决策记录 + 各阶段学习记录

坑 / 禁忌 / 不能改的东西：
- **git 提交不署 AI 名**
- 协议红线：N 个 tool_calls → N 条 tool 回灌；assistant 带 tool_calls 必须原样重发（含 reasoning_content）；流解析容忍 `delta=None` / 空 choices / tool_calls 分片 / mm:think 标签被切断
- 密钥：**别把真 key 写进 `harness.example.toml`**；用户的 `~/.agent-harness/config.toml` 允许明文 `api_key`
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

1. `docs/PLAN.md`（工程真源：决策记录 + 翻案）
2. 本文件 `docs/HANDOFF.md`
3. `README.md`（怎么装、怎么跑、边界在哪）

（`CLAUDE.md` 与 `AGENTS.md` 已于 2026-10-22 删除，不再有项目规则文件；系统提示词在 `assembly.SYSTEM_PROMPT`。）

