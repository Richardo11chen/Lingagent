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
- 界面演进：先终端后画面（S1 终端可见闭环 → S5 浏览器实时画面）。
- 六个维度分工：治理、反馈闭环 = 主攻做深；决策（主循环）= S1 地基，一上来就完整；工具、记忆、配置 = 最低实现，后期可拓展。

## 决策记录

- 学习目标档位：能迁移着做下一个（用户拍板，2026-08-28）。
- 界面：先终端后画面（用户拍板，2026-08-28）。
- 主攻维度：治理 + 反馈闭环两维都主攻（用户拍板 2026-08-28；规格原建议一维深入——用户决定两维，阶梯拆成 S2/S3 两阶段各承重）。
- 实现边界（规格 A.4，用户原文）：主循环、LLM 抽象层（可 mock）必须自己实现；不建在现成 agent 编排框架的高层循环之上；反馈信号与危险动作拦截必须是代码机制（校验器 / 护栏函数），不是提示词。

## 终局参照：agent_loop 伪代码（用户规格材料 2026-08-28 提供）

整台机器的终局形态。阶梯每阶段从中取一段真做：S1 取 while 核心骨架（context 组装 → LLM 一行 → action 分发 → 结果回灌 → done/MAX_STEPS 停机）；S2 取 run_sensors（反馈）；S3 取 guardrail + HITL；S4 取 memory（read/consolidate）；S6 取 MCP / skills 渐进披露 / spawn_subagent。

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
