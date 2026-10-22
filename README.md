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
uv run pytest                  # 全量（170 条）
cd web && npx tsc --noEmit     # 前端类型检查
```

agent 自己的 `run_tests` 工具按项目根的标记文件认测试入口，所以换个项目也能用：

| 标记文件 | 跑什么 |
|---|---|
| `pyproject.toml` / `pytest.ini` / `tox.ini` / `setup.cfg` | `python -m pytest -q`（红了按段提取失败清单 + 详情） |
| `package.json` 且 `scripts.test` 存在 | `npm test` |
| `go.mod` | `go test ./...` |
| `Cargo.toml` | `cargo test` |
| `Makefile` 且有 `test:` 目标 | `make test` |

一个都认不出就直说"没找到测试入口"，让模型改用 `run_shell`——不会伪造成一条红的测试报告。它认的是**工作目录**（项目根），子目录里的子包（比如 `web/`）不管。

## 数据都在哪

| 内容 | 位置 | 说明 |
|---|---|---|
| 配置 | `~/.agent-harness/config.toml` | 含密钥，**不要提交** |
| 会话存档 | `~/.agent-harness/sessions/<项目名-身份前8位>/<会话id>.jsonl` | 一行一条消息，append-only |
| 轨迹 | 同目录 `<会话id>.trace.jsonl` | 一步一条事件，含工具结果**全文**，只增不减 |
| 项目身份 | 工作目录下的 `.agent-harness-id` | 一行 uuid，决定历史归谁 |
| 超长输出的全文 | 工作目录下的 `.agent-harness-out/` | 已 gitignore，留 7 天；给模型分段读用 |
| 后台任务日志 | `~/.agent-harness/tasks/<时间戳>-tN.log` | 留 7 天 |

旧版本把这两样放在 `~/.agent_harness.toml` 和 `~/.agent_harness_sessions/`，首次运行会自动搬到新位置。

注意两件事：

1. **历史归属靠工作目录里的 `.agent-harness-id`**（一个 uuid 文件，已 gitignore）。所以项目**改名、移动目录都不会丢历史**——文件跟着文件夹走，身份不变。反过来说：**删掉这个文件等于换身份**，旧历史不会消失，但不会在这个目录下再列出来（把旧的 id 内容写回去就接上了）。旧布局（按绝对路径命名的目录）会在首次访问时自动迁移过来。
2. **trace 会一直长**（工具结果全文，审计账不截断）。攒久了清一下：

   ```bash
   uv run agent-harness --clean-trace 30   # 删 30 天前的轨迹（0 = 全删）
   ```

   删轨迹不影响会话本身。要彻底不写，配置里 `[trace] enabled = false`——session 照常，只是没有"给人看的账"。TUI 启动时如果发现轨迹一个月没人看过，会提醒一句。

真发生压缩时会往对话里插一行提示（`⟳ 上下文偏长…`），不会静默改小上下文。

关于结果长度：进模型上下文（history / session）的那份截断到 32k 字符（头 24k + 尾 8k，中间注明省略）；**被省略的中间那段不丢**——落在项目根的 `.agent-harness-out/`（已 gitignore，留 7 天）并把路径写进提示，模型可以直接 `read_file` 或 `sed -n` 读那一段，不用把原命令重跑一遍。trace 里保留全文。读取侧也有护栏——单条超过 2MB 的事件不解析（只显示摘要），网页端单字段最多 2k 字符。

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

### 自动批准（Shift+Tab）

在提示行按 `Shift+Tab`：底部那条横线整条变黄，写着 `⚡ 自动批准 · Shift+Tab 关闭`。这时所有审批自动通过，屏幕上留一行 `⚡ 自动批准 · 改动文件：rm`，`/trace` 里也有一条 `⚡ 自动批准`。

三条边界：

- **`catastrophic` 不受影响**：`format` / `mkfs` / `dd` / `rm -rf /` 仍然直接拒——它由主循环在执行前拦掉，连审批都不会进，自动批准碰不到那条路。
- **只活在内存里**：重启回到手动，不写配置、不跨会话。
- **要在提示行按**：运行中屏幕归流式渲染，`Shift+Tab` 到不了输入行；正在等你审批时也不行（那三个键只管这一条命令）。

网页端没有这个开关——那边是按钮式审批。

底部那条横线还会显示上下文占比：

```
────────────── 上下文 12% · Shift+Tab 自动批准
```

分母是压缩阈值（`[context] compact_threshold`），所以读到 100% 就是"下一次请求会被压缩"的时刻。超过 70% 转黄、95% 转红——不必等它变成"这一轮怎么这么慢"才发现。

## 命令在哪个 shell 里跑

`run_shell` 按这个顺序挑一个，用第一个能用的：

| 顺序 | shell | 说明 |
|---|---|---|
| 1 | **Git Bash**（`bash -c`） | 首选。引号/管道/`for` 都是标准 bash，模型最熟 |
| 2 | **PowerShell 7**（`pwsh -Command`） | 没装 Git Bash 时 |
| 3 | **Windows PowerShell 5** | 再退一步 |
| 4 | cmd.exe | 前面都没有才用（兜底） |

`C:\Windows\System32\bash.exe` 是 **WSL 启动器**，不是 Git Bash，会被跳过（它会把命令扔进 Linux，路径全对不上）。

指定别的 shell：

```bash
set AGENT_HARNESS_SHELL=D:\Git\bin\bash.exe      # Windows cmd
export AGENT_HARNESS_SHELL=/usr/bin/bash         # bash
```

横幅里会显示当前用的是哪个（`会话 … · 模型 … · Git Bash`）。

## 后台任务（dev server 这类）

有些命令**没有「跑完」这个状态**——`npm run dev`、`vite --watch`、`uvicorn --reload`。前台跑它们只有两个结局：等 120 秒被杀，或者把会话堵住。

所以 `run_shell` 多了一个 `background` 参数，配套两个工具：

```
run_shell("npm run dev", background=true)   → 已在后台启动 t1（立刻返回，不等）
task_output("t1")                            → 状态 + 输出末尾
task_stop("t1")                              → 连子进程一起停
```

四件事要知道：

- **输出写文件**：`~/.agent-harness/tasks/<时间戳>-tN.log`，留 7 天自动清。`task_output` 只读末尾 64KB——日志可以无限长，整读会先把内存吃光。
- **Ctrl+C 不连坐**：后台任务在独立进程组里，中断当前这一轮不会顺手把 dev server 杀掉；只有 `task_stop` 或退出程序才收（退出时统一杀）。
- **重启即忘**：任务只活在内存里，和「本会话记住的批准」同一个尺度。
- **有的程序把输出攒在缓冲区**：迟迟看不到日志时，加 `PYTHONUNBUFFERED=1` 或 `stdbuf -oL` 再起一次。

## 常见坑

- **同一台机器上 `find` 有两个**：在 Git Bash 里 `find` 是 GNU 的（找文件），`cmd` 里是 Windows 的（逐行搜文本）。跑命令的是 bash，所以 `find . -name "*.py"` 才对；`find /c /v ""` 那套是 cmd 语法，在这里会把 `*.py` 当路径（2026-10-19 就这么灌出过 91MB 输出）。不确定就 `command -v find` 看一眼。
- **`write_file` 覆盖整个文件**，改已有文件用 `edit_file`。
- 项目约定写在 `AGENTS.md`，启动时自动注入 system prompt。

## 目录结构

```
src/agent_harness/
  __init__.py   CLI 主入口（选会话 / REPL / 落盘）
  loop.py       主循环：护栏 → 预钩子 → 执行 → 回灌 → 停机
  guardrail.py  命令风险分级 + 路径围栏
  llm.py        OpenAI 兼容流式适配器（含 413 反应式压缩、思考流拆分）
  tools.py      工具实现（读 / 写 / 编辑 / shell / 后台任务 / pytest）+ 结果截断
  shell.py      挑一个 shell（bash → pwsh7 → pwsh5 → cmd）
  tasks.py      后台任务登记簿（id / 进程 / 输出文件）
  schema.py     工具 schema（= 模型看到的菜单）
  compact.py    投影式微压缩
  session.py    会话存档（JSONL）
  trace.py      轨迹记录（给人看的账）
  hooks.py      策略挂载点
  subagent.py   子 agent（上下文隔离）
  settings.py   多 profile 配置
  paths.py      应用目录（~/.agent-harness）
  assembly.py   公共装配 + SwitchableLLM
  tui.py        终端界面
  server.py     FastAPI + WebSocket 后端
  weblaunch.py  TUI 启动时拉起网页端
tests/          test_s1 ~ test_s11 + test_tools（155 条）
web/            React + TS + Vite 前端
docs/PLAN.md    工程真源（决策记录）
docs/HANDOFF.md 会话交接单
```
