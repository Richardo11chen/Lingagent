# Agent Harness

一个自己转圈干活的 coding agent 引擎：LLM 只做「下一步做什么」的决策，其余（工具分发、治理拦截、反馈回灌、记忆读写、停机）全是自己写的工程。

核心等式：**Agent = LLM + Harness**。

- 终端界面（TUI）+ 网页界面（React，实时流 + 网页审批）
- 危险动作走风险分级：只读放行 / 改动静默问人 / 极危险直拒
- 每步动作留轨迹（trace），会话可恢复（session）
- 硬标准：**移除真 LLM 后核心机制仍能用确定性单测验证**（117 条测试）

## 环境要求

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | 3.14+ | `.python-version` 已锁 3.14；用 `uv` 托管，不碰系统环境 |
| uv | 0.12+ | 包管理与运行入口 |
| Node.js | 20+ | 只有网页前端需要（`web/`） |

## 安装

```bash
git clone <repo> && cd AgentHarness
uv sync
```

## 配置

复制示例配置到用户目录（**不在仓库里**，也不该提交）：

```bash
mkdir -p ~/.agent-harness && cp harness.example.toml ~/.agent-harness/config.toml
# Windows: mkdir %USERPROFILE%\.agent-harness && copy harness.example.toml %USERPROFILE%\.agent-harness\config.toml
```

最小可用配置：

```toml
[model]
default = "deepseek-flash"

[models.deepseek-flash]
name = "deepseek-flash"
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"    # 从环境变量读（推荐）
# api_key = "sk-..."                # 或直写明文（优先于上面的 env；别提交进仓库）
```

密钥两种写法二选一，**都没有就在启动时直接报错退出**（TUI 和后端都会前置校验）。

其余可调项（`[loop] max_steps`、`[context] compact_threshold`、`[server] port`、`[web] autostart`）见 `harness.example.toml` 里的注释。

## 运行

**终端界面：**

```bash
uv run agent-harness                          # 用配置里的 default profile
uv run agent-harness --model deepseek-v4-pro  # 临时指定 profile
```

首次启动会：列出历史会话让你选（`0` 开新会话、`n`/`p` 翻页）→ 自动拉起网页端（`[web] autostart = false` 可关）。

界面内命令：输入 `/` 会弹出命令菜单——`/model` 切模型、`/trace` 看本轮轨迹（`/trace N` 钻取单条）。
退出：`Ctrl+C` 连按两次，或 `Ctrl+D`。

**网页界面：**

```bash
# 终端 1：后端（默认 127.0.0.1:8000）
uv run agent-harness-web

# 终端 2：前端（默认 5173）
cd web && npm install && npm run dev
```

打开 <http://localhost:5173>。由 TUI 自启时这两步都不用手动做。

> 改了 `[server] port` 的话前端也要跟上：在 `web/.env` 里写 `VITE_WS_PORT=新端口`（不写默认 8000）。

## 测试

```bash
uv run pytest                  # 全量（117 条）
cd web && npx tsc --noEmit     # 前端类型检查
```

## 数据都在哪

| 内容 | 位置 | 说明 |
|---|---|---|
| 配置 | `~/.agent-harness/config.toml` | 含密钥，**不要提交** |
| 会话存档 | `~/.agent-harness/sessions/<项目名-身份前8位>/<会话id>.jsonl` | 一行一条消息，append-only |
| 轨迹 | 同目录 `<会话id>.trace.jsonl` | 一步一条事件，含工具结果**全文**，只增不减 |
| 项目身份 | 工作目录下的 `.agent-harness-id` | 一行 uuid，决定历史归谁 |

旧版本把这两样放在 `~/.agent_harness.toml` 和 `~/.agent_harness_sessions/`，首次运行会自动搬到新位置。

注意两件事：

1. **历史归属靠工作目录里的 `.agent-harness-id`**（一个 uuid 文件，已 gitignore）。所以项目**改名、移动目录都不会丢历史**——文件跟着文件夹走，身份不变。反过来说：**删掉这个文件等于换身份**，旧历史不会消失，但不会在这个目录下再列出来（把旧的 id 内容写回去就接上了）。旧布局（按绝对路径命名的目录）会在首次访问时自动迁移过来。
2. **trace 会一直长**（工具结果全文，审计账不截断）。攒久了清一下：

   ```bash
   uv run agent-harness --clean-trace 30   # 删 30 天前的轨迹（0 = 全删）
   ```

   删轨迹不影响会话本身。要彻底不写，配置里 `[trace] enabled = false`——session 照常，只是没有"给人看的账"。TUI 启动时如果发现轨迹一个月没人看过，会提醒一句。

真发生压缩时会往对话里插一行提示（`⟳ 上下文偏长…`），不会静默改小上下文。

关于结果长度：进模型上下文（history / session）的那份截断到 32k 字符（头 24k + 尾 8k，中间注明省略）；**trace 里保留全文**。读取侧也有护栏——单条超过 2MB 的事件不解析（只显示摘要），网页端单字段最多 2k 字符。

## 已知边界（有意不做的）

- **没有 OS 级沙箱**：`run_shell` 靠风险分级 + 人工审批，不是容器隔离。别拿它跑不可信的任务。
- **审批会超时**：默认 5 分钟没人答复就按驳回处理（`[loop] approval_timeout`，设 0 = 一直等）。
- **网页端的「停止」只在本步结束后生效**：正在跑的命令 / 子 agent 会跑完，之后不再发起新动作（不硬杀进程）。TUI 用 Ctrl+C。
- **子 agent 的「只读」靠提示词**，工具是全量的；它不能再派生孙 agent（深度上限 1）。
- **子 agent 的中间过程不进实时流**，只进 trace。
- 只接 OpenAI 兼容协议面；DeepSeek 私有参数（`thinking` / `reasoning_effort`）默认不传。

## 审批怎么答

危险动作会停下来问你一句，只认三个键：

| 键 | 含义 |
|---|---|
| `Enter` | 执行这一次 |
| `Esc` | 驳回（也可以打 `n` 回车） |
| `a` + `Enter` | 执行，并且**本次会话不再问这个程序** |

网页端是三个按钮：批准 / 驳回 / 本会话不再问。

记住的范围是"程序名"（`git`、`uv`、`del`），不是整条命令；只存在内存里，**重启就忘**。想让某些命令永远不问，写进配置：

```toml
[guardrail]
allow = ["git *", "uv *", "rg *"]   # 前缀语义：以这些词开头的命令不再问
```

`catastrophic` 档（`format` / `mkfs` / `dd` / `rm -rf /`）**不受 allow 影响**——写 `allow = ["*"]` 也放不出来，这是刻意留的地板。

## 常见坑

- **同名命令不同物**：PATH 里 Git 的 unix 工具（`find` / `sort` / `awk`）可能排在 `System32` 之前。Windows 的 `find` 是逐行文本搜索，GNU 的 `find` 是文件查找——`find /c /v "" README.md` 在 GNU 语义下会把 `C:\` 整个遍历一遍（2026-10-19 就这么灌出过 91MB 输出）。不确定就先 `where find`。
- **`write_file` 覆盖整个文件**，改已有文件用 `edit_file`。
- 项目约定写在 `AGENTS.md`，启动时自动注入 system prompt。

## 目录结构

```
src/agent_harness/
  __init__.py   CLI 主入口（选会话 / REPL / 落盘）
  loop.py       主循环：护栏 → 预钩子 → 执行 → 回灌 → 停机
  guardrail.py  命令风险分级 + 路径围栏
  llm.py        OpenAI 兼容流式适配器（含 413 反应式压缩、思考流拆分）
  tools.py      工具实现（读 / 写 / 编辑 / shell / pytest）+ 结果截断
  schema.py     工具 schema（= 模型看到的菜单）
  compact.py    投影式微压缩
  session.py    会话存档（JSONL）
  trace.py      轨迹记录（给人看的账）
  hooks.py      策略挂载点
  subagent.py   子 agent（上下文隔离）
  settings.py   多 profile 配置
  assembly.py   公共装配 + SwitchableLLM
  tui.py        终端界面
  server.py     FastAPI + WebSocket 后端
  weblaunch.py  TUI 启动时拉起网页端
tests/          test_s1 ~ test_s11（117 条）
web/            React + TS + Vite 前端
docs/PLAN.md    工程真源（决策记录）
docs/HANDOFF.md 会话交接单
```
