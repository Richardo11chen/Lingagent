# PLAN · Agent Harness

> 产品与工程现状以本文件为准。决策翻案在此记条目（旧方案 + 为什么翻）。

## 产品是什么

一个自己转圈干活的 coding agent 引擎：Python 后端把 LLM 封装成会读写文件、跑测试、自我修正、危险动作要人批准的完整系统；TS 前端（后期阶段）实时显示它在干什么。

核心等式：**Agent = LLM + Harness**。LLM 只做「下一步做什么」的决策，其余全是我们写的工程。

## 剧本（场景清单）

1. 你在终端对它说「把登录页这个 bug 修了」，它自己读代码、改文件、跑测试，红了就再改，绿了才停。
2. 它想执行 `rm -rf`、删库这类命令时，先停下来问你「确认吗」，你点头才动。
3. 第二天回来，它还认得项目约定和昨天拍过的决策，不用重新交代。
4. 把真模型换成假模型，整套机制照样能测。

## 做成判据

1. 六个维度（决策 / 工具 / 记忆 / 治理 / 反馈 / 配置）都有能跑的最低实现。
2. 治理 + 反馈闭环两个维度做深（主攻）。
3. 硬标准：移除真实 LLM 后，核心机制（工具分发、治理拦截、反馈回灌、记忆读写、停机）仍能用确定性单测验证。
4. 每阶段在终端看得见完整闭环。

## 架构 / 形状（方向性，施工期细化）

- 后端：Python。前端：TypeScript（后期阶段才上）。
- 界面演进：先终端后画面（S1 终端可见闭环 → S6a 终端 TUI → S6b 浏览器实时画面）。
- 六个维度分工：治理、反馈闭环 = 主攻做深；决策（主循环）= S1 地基，一上来就完整；工具、记忆、配置 = 最低实现，后期可拓展。

## 开发流程（2026-09-22 定 · 后续阶段标准模板）

**七环**：项目目标 → 用户与场景 → 范围与 MVP → 技术需求 → 技术栈候选 → 对比验证 → 决策记录。

- 大选型走全链（候选表 → 实跑验证 → 落决策记录）；小决策一行记录即可
- **对比验证必须实跑**（起最小 demo）——纸上对比区分不了「看起来行」和「真行」
- 决策记录落本文件「决策记录」节（记候选与理由，不只记结果）

### 考古回填（S1-S5 的选型补记）

| 决策 | 候选 | 选了什么 | 理由 |
|---|---|---|---|
| Python 包管理 | pip+venv / poetry / conda / **uv** | uv | 快（Rust）、uv.lock 可复现、`uv run` 一键；用户指定 |
| Python 版本 | Anaconda 3.13 / uv 托管 3.14 | uv 托管 3.14 | 项目环境与 Anaconda 隔离，避免污染 |
| LLM 接入 | 各家原生 SDK / **openai 兼容** | openai SDK + DeepSeek base_url | OpenAI 兼容生态最大、DeepSeek 便宜；换供应商只改 base_url |
| 供应商 | OpenAI / Anthropic / **DeepSeek** | DeepSeek | 成本低、OpenAI 兼容、用户环境已有 key |
| 模型 | v4-pro / **v4-flash** | deepseek-v4-flash | 便宜快，学习项目够用 |
| 前端（立项定） | — | TypeScript | 用户立项指定（先终端后画面） |

### S6 前端选型（需求 → 候选 → 待对比验证）

**技术需求**：① 实时看它每一步（轨迹流）② 网页上批准危险命令（双向 HITL）③ 单机自用（不部署）④ 学习项目（复杂度可控、AI 生成友好）⑤ 核心后端（Python）不重写

**候选对比**：

| 组 | 候选 | 优 | 劣 |
|---|---|---|---|
| 前端框架 | React+TS | 生态最大、AI 样本最多 | 概念多、代码量大 |
| | Vue+TS | 梯度平缓、中文资料多 | 生态次之 |
| | Svelte+TS | 代码最少、响应式简单 | 生态小 |
| | 原生 TS | 零依赖 | 界面手写费工 |
| 通信 | **WebSocket** | 双向一条通道（轨迹 + 审批同管道） | 要管连接状态 |
| | SSE + POST | 流式天然 | 两套机制 |
| | 轮询 | 最简单 | 不实时 |
| 后端 web | **FastAPI + uvicorn** | 标准、WS 支持好、类型友好 | 新依赖 |
| | 标准库 http.server | 零依赖 | WS 要手写 |

**决策（2026-09-22 用户拍板）**：前端 = **React + TS**（AI 样本最多）｜通信 = **WebSocket**（双向单通道：轨迹流 + 审批回传）｜后端 web = **FastAPI + uvicorn**

**验证**：依赖冒烟 ✅（fastapi 0.141.1 / uvicorn 0.53.0 / starlette 1.6.0 装于 Python 3.14.7，WS 端点定义通过）；真握手 + 审批桥接 + 流式均已在 S6a/S6b 落地验证（2026-10-09 补；详见学习记录 S6b）

**已知架构难点**：harness 现在同步阻塞（`approver=input()`、`subprocess`）——web 化要解决 ①approver 跨进程（后端推问题 → 阻塞等前端答案）②agent_loop 进后台线程（不阻塞 web 循环）。建议拆 S6a 只读实时流 → S6b 双向审批

## CE 四策略规格（2026-09-22 定，业界对照后拍板）

| 策略 | 我们的方案 | 业界参照 |
|---|---|---|
| **Write** | S4a 会话存档（JSONL 追加）+ **约定/偏好改走配置形态**（手写 `AGENTS.md` + harness 自动注入 → S4c 配置维）；模型提取器已取消（2026-09-22 转向：继续会话=完全相同、新开=无关，自动记忆无立足点） | Claude Code CLAUDE.md / Codex AGENTS.md；nanobot MEMORY.md |
| **Select** | 记忆全量注入（库小）+ 工具按需读（7 工具，在业界 5-8 上限内）；**不做 RAG**；记忆 >200 行时再评估分文件 + grep | 混合形态：小常载核心 + RAG 其余（LangChain） |
| **Compress** | 见下方细则（并入 S4b 阶段） | Claude Code 五档递进：工具结果预算 → 剪裁 → **微压缩** → 塌缩 → 全量摘要；另有 reactive 413 兜底 |
| **Isolate** | 顺延为后续备选（子 agent）——原标「推迟 S7」，S7 号让位给多模型档（2026-10-09 编号注记） | 多 agent 隔离上下文（Anthropic：性能更好但 token 15×） |

### Compress 细则（拍板版）

- **作用面 = 投影式**：只在 `to_api_messages` 前插一步压缩（`DeepSeekLLM.__call__` 内），**不改**内存 history、**不动**存档——history 保真、纯函数可测、无副作用
- **判定 = 工具白名单**：`Msg` 加 `tool_name` 字段；可清理工具 = `read_file` / `run_shell` / `run_tests`（大输出类）；write/edit/count 不清（结果短）
- **保留最近 N=5** 条白名单工具结果；更早的 content 替换为占位（带工具名 + 原长度线索，如 `[已清理：read_file 的旧输出，1234 字符]`）——**role / tool_call_id / tool_calls 结构一字不动**（配对不破 = 400 防线）
- **触发**：每轮请求前 `estimate_tokens(messages) > 70% 窗口`（字符数粗估，不做精确 tokenizer）；另加消息数兜底
- **Reactive 兜底**：捕获 413 / context-length 错误 → 加压（N 减半→必要时全清旧结果）→ 重试一次；`has_attempted` 标志防死循环
- **不做**：LLM 摘要化（auto-compact 形态——要一次 LLM 调用 + 配对边界处理，留后）；`cache_edits`（我们无 prompt cache 机制）
- **原文去向**：不可回取（Claude Code 形态）——源头可重取（文件可重读、命令可重跑）；S4a 的 JSONL 存档天然保留完整原文（给人/审计看，不给模型）

## 决策记录

- 学习目标档位：能迁移着做下一个（用户拍板，2026-08-28）。
- 界面：先终端后画面（用户拍板，2026-08-28）。
- 主攻维度：治理 + 反馈闭环两维都主攻（用户拍板 2026-08-28；规格原建议一维深入——用户决定两维，阶梯拆成 S2/S3 两阶段各承重）。
- 实现边界（规格 A.4，用户原文）：主循环、LLM 抽象层（可 mock）必须自己实现；不建在现成 agent 编排框架的高层循环之上；反馈信号与危险动作拦截必须是代码机制（校验器 / 护栏函数），不是提示词。
- S6 落地（2026-10-09）：拆两档——S6a TUI（rich + prompt_toolkit，Live 单通道流式）→ S6b Web（FastAPI + WebSocket 异步桥接 + React 前端）；审批跨进程 = `threading.Event` + 常驻收消息循环（关键架构点）。
- S7 = 多模型档（2026-10-09，配置维做深）：每个模型一个 profile（**不做 provider 层**——YAGNI：连接信息重复两行，等「改一处要改多处」再抽象）；`thinking` / `reasoning_effort` 三态、默认不传（**翻案**：旧版无条件传 DeepSeek 私有参数——兼容面只盖协议骨架，不盖厂商扩展参数）；身份类配置错误 fail fast（`--model` 未知、`default` 不在 `[models.*]` 均当场退出）；双端 `/model` 运行时切换（`SwitchableLLM` 门面）。范围：只接 OpenAI 兼容面（用户拍板）。
- 编号注记（2026-10-09）：S7 原为 Isolate / 子 agent 预留（见 CE 表），实际施工让位给多模型档；子 agent 顺延为后续候选。
- 配置支持 `api_key` 直写（2026-10-09，用户要求「不考虑安全性」）：profile 写 `api_key = "..."` 即用、**优先于 `api_key_env`**；两者都缺 → 启动时 SystemExit（替代裸 KeyError）。明文落盘——示例文件注明「别把 key 提交进仓库」。
- 网页端自启（2026-10-12，用户要求）：进 TUI 自动确保网页端在跑——端口探测**幂等**（已在跑的不重复拉）、进程 **detached**（关 TUI 网页端不受影响）、`[web].autostart` 可关、管道/非交互模式不触发。实现 `weblaunch.py`。
- 会话级模型（2026-10-12，用户要求「符合实际模型 / 每个对话独立 / 退出不恢复默认」）：模型选择落进**会话存档**（meta 行 `save/load_session_model`）——打开会话恢复其模型、`/model` 切换即落盘、`--model` 显式指定优先并落账；新会话用默认。**翻案**：原设计模型选择只活在进程内存（每个 TUI 进程一份、退出即丢、多窗口各记各的——用户实测撞出「两个窗口模型对不上」的困惑；探针实证 mm:think 只属 MiniMax、deepseek 思考走 💭 通道）。顺带：trace 的 model 事件记 `model` 字段（「到底谁在答」按行可查）。
- S8 = tracer（2026-10-12）：两份账分家——session 给模型看（可压缩投影）、trace 给人看（`<会话id>.trace.jsonl`，append-only、永不压缩）；记录点 = **注入式 tracer**（作 `agent_loop` 参数，可关、可换假件）；粒度 = 一动作一条（model / tool / approval）+ `perf_counter` 耗时 + **结果全文**；「回放」= 只看时间线不重演（重演 = 重新执行工具，属治理问题，v1 不做）；查看入口 = TUI `/trace` 总览 + 钻取、网页 overlay（WS 只推最近 1000 条 + total）。
- S9 = hooks（2026-10-12）：策略挂载点（PreToolUse / PostToolUse）；预钩子 `fn(action) -> Verdict | None`（`deny(reason)`、reason 原样回灌）、**首个 deny 短路**、异常 = 默认拦截（fail closed）；后钩子只观察（异常吞掉）；**三道理序**——内置护栏 + 审批**排最前、不可摘**（基线），hooks 在其后叠加（更严的赢）；注册 v1 = 代码（`assembly.PRE_HOOKS`），配置声明等第二个来源；拦 / 放都进 trace，`on_hook` 只在拦下冒实时行；随附策略 `forbid_env_writes`。
- run_shell 硬超时（2026-10-19，S10 真机撞出）：单条 shell 命令 120 秒上限；Windows 上超时必须 `taskkill /F /T` 杀整棵树——`subprocess.run(timeout=)` 只杀 `cmd.exe`，孙进程攥着管道，`communicate` 仍会阻塞到它跑完（实测）。
- S10 子 agent 工具面与只读约束（2026-10-19，用户最终拍板）：子 agent 拿**全量基础工具**（含写类）；只读只在**提示词层**约束——「如果只想让它读，就遵守只读约束」：默认只读，父任务明确要求写入才动手并列出改动。模型跟随父当前 profile。**翻案**：旧案「白名单只读工具集」已撤——用户要能力面完整；代价 = 只读是约定不是机制（OS 级沙箱未收口），残余风险记已知边界。附带修复：`agent_loop` 的 `system_prompt` 原先是死参数（从未使用），现随调用传入 LLM 适配器——子 agent 的提示词才真正送达。
- S11 = 命令风险分级（2026-10-19，用户拍板）：`guardrail` 从 `DANGEROUS` 黑名单翻成 `classify_command`——拆段（未加引号的 `& | ( )` + 换行）→ 剔除 `2>&1` → 重定向（`>`/`>>`）算改动 → 程序名归一（去引号 / `@` / basename / 扩展名）→ 策略表；整条按最严段算。策略：只读白名单放行（dir/type/findstr/git 只读子命令/`--version` 等）；改动 / 执行 / 网络 / **未知一律问人**（fail closed）；极危险（format/mkfs/fdisk/diskpart/dd/shred/wipe、`rm -rf /`、`del /s /q C:\`）**直接拒**。`run_shell` 的 tool 事件与 approval 事件都记 `risk`。边界：分级是「够用级」解析（不是完整 shell parser），判不出就问人——解析粗糙不会变成放行漏洞。
- 审批不设超时（2026-10-19，用户拍板）：HITL 等人说了算——等多久都等，不自动驳回；记为**有意设计**，不是欠账（原「已知边界」里的该项收口方式 = 明确不做）。同意：S10 子 agent 中间过程不进实时流不做；大考弃考、AI 代答（认知未验）。

## 终局参照：agent_loop 伪代码（用户规格材料 2026-08-28 提供）

整台机器的终局形态。阶梯每阶段从中取一段真做：S1 取 while 核心骨架（context 组装 → LLM 一行 → action 分发 → 结果回灌 → done/MAX_STEPS 停机）；S2 取 run_sensors（反馈）；S3 取 guardrail + HITL；S4 取 memory（read/consolidate）；S6a/S6b 取实时界面（TUI / Web 流式）；MCP / skills 渐进披露 / spawn_subagent 顺延为后续备选。

```python
# ---------- ① 启动装配:跑在 while 之前,一次性 ----------
function build_agent(project):
    system_prompt = vendor_system_prompt()  # 厂商内建、你无法修改的"骨架"那部分上下文:产品级 harness 的核心
    rules     = load_rule_files("CLAUDE.md", "AGENTS.md") # 【规则文件】启动自动加载的声明式约束(你来填的内容物)
    skills    = register_skills()        # 【Skill】只登记"名片"(description),不载全文
    tools     = register_builtin_tools() # 【Tool 原语】读写文件/跑 bash……agent 最底层的动作
    mcp       = connect_mcp_servers()    # 【MCP】把外部工具按统一协议接进来(N+M,不为每个工具改代码)
    hooks     = register_hooks()         # 【Hooks】绑到生命周期事件上的脚本
    guardrail = register_guardrail()     # 【权限门控/Guardrail】危险动作的策略闸:产品级 harness 最显眼的部件
    sandbox   = create_sandbox(limits)   # 【架构约束/Sandbox】给一切执行加上隔离与资源上限
    tracer    = open_trace()             # 【可观测性】记录每一步,事后可回放
    memory    = open_memory()            # 【记忆 (Write)】跨会话长期记忆的读写口
    retriever = open_retriever()         # 【RAG / Select】按需把外部知识检索进上下文
    return Harness(system_prompt, rules, skills, tools, mcp,
                   hooks, guardrail, sandbox, tracer, memory, retriever)

# ---------- ② 主体:整个函数体 = harness;LLM 只占其中一行 ----------
function agent_loop(goal, H, depth=0):  # H = 装配好的 harness;depth 给子 agent 防递归过深
    # 【CE 上下文工程】组织"提供给大脑的信息",这是 harness 的另一半工作
    context  = H.system_prompt           # 骨架:厂商内建的系统提示词(范式A 最好的代码级例子,你无法修改)
    context += H.rules                   # 内容物:你在 CLAUDE.md 里填的那部分
    context += H.memory.read(goal)       # 【CE-读记忆】跨会话长期记忆(现代实现里读/写常是模型自己调的工具)
    context += H.retriever.retrieve(goal)# 【CE-Select/RAG】开局先检索一批相关知识(预检索)
    context += goal

    done   = False
    answer = None
    steps  = 0
    while not done and steps < MAX_STEPS:   # guard:除最大轮数,真实系统还设超时/费用预算/重复调用检测
        steps += 1
        if token_count(context) > LIMIT:
            context = compact(context)  # 【熵管理 / CE-Compress】窗口将满则压缩

        menu = H.tools.list() + H.mcp.list_tools() + H.skills.list_descriptions()
        # ========================================================
        text, action = LLM(context, menu)  # ← LLM 唯一一行做"任务决策" ★ 唯一的「任务决策」LLM 调用
        # ========================================================
        context += message("assistant", [text, action])  # 模型这一整轮打包成一条 assistant 消息写回历史
        H.tracer.record(text, action)

        if action.type == "done":
            done   = True
            answer = text               # 停下:最终答案就是模型这轮的输出
            continue

        if not H.guardrail.allow(action):                 # 【Guardrail】危险动作先拦
            context += message("user", "该动作被策略拦截"); continue   # 高风险时停下等人工审批(HITL)

        verdict = run_hooks(H.hooks, "PreToolUse", action) # 【Hooks】执行前触发
        if verdict == "deny":
            context += message("user", "被 Hook 拦截"); continue

        if action.type == "use_skill":
            instructions = H.skills.load(action.name)     # 【Skill 渐进式披露】命中才读全文
            context += message("user", instructions)

        elif action.type == "call_tool":
            try:
                if action.tool in H.mcp:
                    result = H.mcp.call(action.tool, action.args)
                else:
                    result = H.sandbox.run(H.tools[action.tool], action.args)
            except ToolError as e:
                result = "工具失败:" + e.message           # 失败也是客观事实:错误回灌,模型据此改换路径重试
            context += message("user", result)

        elif action.type == "spawn_subagent":             # 【子 Agent】
            if depth >= MAX_DEPTH:
                context += message("user", "子 agent 嵌套已达深度上限,拒绝派发"); continue
            child_H  = H.restrict(action.scope)
            summary  = agent_loop(action.subtask, child_H, depth + 1)
            context += message("user", summary)

        elif action.type == "take_note":
            H.memory.write(action.note)

        run_hooks(H.hooks, "PostToolUse", action)

        if action.changed_code:                           # 【反馈循环】让"机器护栏"告诉 agent 它做错没
            report   = run_sensors(tests, lint, types, CI)
            context += message("user", report)   # 失败信息回灌,agent 据此自我修正

    # ---------- ③ 收尾:循环退出后,跑且只跑一次 ----------
    run_hooks(H.hooks, "SessionEnd", None)
    H.memory.consolidate(context)       # 把零散笔记固化成长期记忆
    H.tracer.flush()
    return answer
```

## 怎么跑（构建 / 部署级）

- 环境：uv 0.12.7（winget 安装，2026-08-28；新开终端 `uv` 直接可用）；Python 3.14.7（uv 托管）；Anaconda Python 3.13.9 在 `D:\Anaconda`（未采用，项目环境与它独立）
- 依赖：pytest（dev）已加；项目依赖在 `pyproject.toml`
- 命令：`uv run pytest` 跑测试；`uv add <包>` / `uv add --dev <包>` 加依赖；`uv run python x.py` 跑脚本
- 入口：`agent-harness = agent_harness:main`（src 布局，src/agent_harness/）
