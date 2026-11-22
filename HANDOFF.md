# 交接文档

写给接手这个项目的下一个模型/人。读完这份应该能：跑起来、改对地方、不破坏已经踩过坑才立住的约定。

面向用户的说明在 `README.md`（很长，按功能组织）。这份文档讲的是**为什么这么写**和**接下来该做什么**。

---

## 1. 三十秒上手

```bash
uv sync                          # 装依赖（Python >= 3.11，本机 .python-version 是 3.14）
uv run pytest -q                 # 全量测试（当前 323 passed + 1 skipped）
uv run ruff check src tests      # 静态检查
uv run lingan                    # 启动（TUI + 自动拉起网页端）
uv run lingan -web               # 只起后端（http://127.0.0.1:8000）
```

跑测试不需要 API key——测试注入假 LLM，不发真请求。**这是硬规矩**（见 §5.1）。

## 2. 这是什么

一个本机运行的 coding agent：终端界面 + 网页界面，共用一套引擎。设计立场是"**护栏是代码机制，不是提示词**"——所有安全判断都可确定性单测，不靠模型自觉。

开发机是 Windows（shell 检测、taskkill、MSYS 路径都是 Windows 语义），但装给别人用的是 Linux/macOS，所以 CI 两个平台都跑。

## 3. 一次 turn 怎么走（先读这几个文件）

```
用户输入
  └─ turn.run_turn()              ← 一轮的编排：模型 → 工具 → 回灌，直到 done
       └─ loop.agent_loop()       ← 真正的循环（唯一实现，两个界面共用）
            ├─ guardrail.classify_command()   命令风险分级（灾难 > 凭据 > allow > 分类）
            ├─ hooks.run_pre()                预钩子（能拦）
            ├─ 执行工具
            ├─ hooks.run_post()               后钩子（能改写结果）
            ├─ cap_result()                   截断 + 溢出落盘
            └─ 回灌 history → 下一轮
```

**`turn.py` 是抽得最好的部分，别推倒重来。** 它是一段直线：给定 goal 和一组回调，跑完返回。所以两个界面都能用。

`turn.py` 只做"一轮"，会话的生命周期在上面一层：`runtime.SessionRuntime`（新建 / 切换模型 / 存档 / 回放 / "能不能跑"）。两个界面都不许自己写这些——见 §5.11。

## 4. 模块地图（只列容易找错的）

| 文件 | 职责 | 注意 |
|---|---|---|
| `assembly.py` | 装配：system prompt / 工作区 / 工具注册表 | `WORKSPACE` 的**唯一**构造点 |
| `runtime.py` | 会话运行时：新建 / 切换模型 / 存档 / 回放 / 跑一轮 | 两个界面只调它，见 §5.11 |
| `guardrail.py` | 命令风险分级 + 路径围栏 | 三条防线在这里排位 |
| `hooks.py` | 策略挂载点 + 内置策略 | 前钩子能拦、后钩子能改 |
| `compact.py` | 投影式压缩 | 不改 history，只改发给 API 的副本 |
| `session.py` | JSONL 存档 | 存在 `~/.agent-harness/sessions`，不在项目目录 |
| `trace.py` | 轨迹（给人看的账） | 存全文、不截断 |
| `workspace.py` | 工作区值对象 | 新增，见 §5.6 |
| `webapp.py` | HTTP 面：探活 + 静态托管 + token | **token 端点必须注册在 SPA 通配路由之前** |
| `server.py` | WS 协议（一个连接 = 一个会话） | 握手要 token + Origin |
| `cli.py` / `tui.py` | 终端界面 | 同步 REPL |
| `subagent.py` | 子 agent（上下文隔离） | 深度上限 1 |

## 5. 不可破坏的约定

### 5.1 测试不许依赖真 LLM

`test_s1` 的 docstring 写死了这条：**移除真 LLM 后核心机制仍能用确定性单测验证**。需要"模型说什么"的地方用 `ScriptLLM` / `RouteLLM`（在 `llm.py` 和测试里）。加新机制时先想"怎么用假模型测"。

### 5.2 三条防线的顺序

`classify_command` 里的顺序是 **灾难 > 凭据 > allow 规则 > 分类**，不要调换：

- 灾难（`format`/`dd`/`mkfs`）永远直接拒，`allow = ["*"]` 也放不出来
- 凭据（`.ssh/`、`id_rsa`、`.aws/credentials`）只读也要问人
- `allow` 是省审批次数的，不是安全边界

### 5.3 钩子的失败语义是两个方向

- 预钩子抛异常 = **默认拦截**（fail closed，策略坏了宁可停）
- 后钩子抛异常 = **吞掉**（观察类不许拖垮主流程）
- 后钩子可以**返回字符串替换结果**（脱敏用），返回 `None` = 没意见

### 5.4 `tool` 结果在 history 里是 `role="user"` + `tool_name`

这是这个项目的老约定，不是 bug。按 `m.role == "tool"` 过滤是找不到的，要用 `m.tool_name`。

### 5.5 契约测试守着 schema

`tests/test_contract.py` 逐条比对「schema 暴露的参数」和「函数真实签名」。因此：

- 给工具函数加参数，必须同步改 `schema.py`，否则测试红
- **不要用 `functools.partial` 注入参数**——partial 会把绑定参数留在签名里，契约测试会报"签名与菜单不符"。要注入就用薄包装函数（见 `assembly.run_tests_here`）

### 5.6 不许到处 `Path.cwd()`

工作区是 `workspace.Workspace`，全项目只在 `assembly.py` 构造一次。其余模块接受传进来的 `root: Path`。现在只剩两处兜底（`sensors.run_tests`、`tools._dump_full` 的 `root or Path.cwd()`），注释里说明了为什么。

想换目录：造一个 `Workspace(其他路径)`，**不要 `chdir`**，也不要新增 `Path.cwd()`。

### 5.7 WS 两道门缺一不可

- **Origin** 挡浏览器：WS 不受同源策略约束，任何网页都能连 `ws://127.0.0.1`
- **token** 挡非浏览器：`0.0.0.0` 下 curl 不带 Origin

`GET /api/token` **没有 Origin 一律 403**——否则一条 curl 就把钥匙拿走了。dev 模式前端在 5173、后端在 8000，所以对白名单来源回显 ACAO。

### 5.8 压缩是投影

`microcompact` / `snip` / `cap_long_messages` 都只改**发给 API 的副本**，history 原样保留。改这块时不要顺手改 history，用户翻历史应该还能看到全文。

### 5.9 别推翻这些已经论证过的决定

| 不做 | 理由 |
|---|---|
| LLM 摘要旧历史 | 缓存命中 0.02 元/M、未命中 1 元/M，**差 50 倍**。摘要重写前缀 = 每轮全量重算 |
| 精确 token 计数（tiktoken） | 那是 OpenAI 的 BPE，中文误差 30%。真正的问题是"哪段该丢" |
| mypy | 8k 行一次性严格检查只产出噪音，先把 ruff 用起来 |

### 5.10 写盘只走 `apply_change`

`write_file` / `edit_file` 返回的是**意图**（`FileChange`），自己不落盘；`loop.py` 的执行点负责 `apply_change`（写盘 + 算 diff），diff 在同一处进 trace 和界面。

这条由 `tests/test_tools.py::test_only_apply_change_touches_the_disk` 用 AST 盯着。新增工具**不要**自己写盘——绕过这条就等于又把 diff 弄丢一次，而这次连 ContextVar 都没有，只会静静少一行显示。

同理，**后钩子拿不到 `apply_change`**：改文件这件事对它是结构性做不到的（决策见 §8 P0）。

### 5.11 会话状态迁移只走 `SessionRuntime`

新建 / 恢复 / 切模型 / 落盘 / 轨迹，只在 `runtime.py` 里有实现。`cli.py` 和 `server.py`
**不许 import `Session`（`.session` 存档模块）**——`tests/test_runtime.py` 用 AST 盯着。

这条不是洁癖：同一份编排抄两遍之后，网页端收到新指令时静默丢弃、终端却提示"这一轮还在跑"，
同一个文件里两个答案，而且没有任何测试会红。现在"能不能"由 `_claim()` 一处回答，
"这句话怎么说"只有 `BUSY_MESSAGE` 一个常量。

顺带一句边界：`llm` 和 `approver` 是**连接级**的（由 UI 造、传给 runtime，切会话不重建）；
`session_id` / `history` / `tracer` / 该用哪个 profile 是**会话级**的。画错了，表现是
"切会话把模型也重置了"。

## 6. 这个环境里的坑（会重复踩）

**heredoc 追加测试会写两遍。** 用 `cat >> file <<EOF` 追加时，内容有时会写进两份，产生重复定义（ruff `F811` 能抓到，但人不一定看）。**追加完立刻 `grep -c "def test_xxx"` 数一下。** 这个坑踩过三次，两次导致了"测试从没跑过"的假绿。

**`git push` 的代理。** `~/.gitconfig` 里给 github.com 钉了 `127.0.0.1:7897`（Clash 默认端口），是 URL 级配置，优先级高于 `-c http.proxy=`。会话代理端口每次启动都变，所以：

```bash
git -c "http.https://github.com.proxy=$HTTPS_PROXY" push origin main
```

不要改用户的 `.gitconfig`。

**推送"成功"后可能报"命令被安全策略拦截"。** 这是误报（遇到过四次），推送实际已成功。**不要重试**，用 API 核实：

```bash
curl -s https://api.github.com/repos/Richardo11chen/Lingagent/commits/main
```

**GitHub API 偶发 504 或空 body。** 别当成"没数据"，重试一次。

**PyPI 的 JSON 有 CDN 缓存。** 发布成功后查 `https://pypi.org/pypi/lingagent/json` 可能还返回旧版本，查具体版本端点 `/pypi/lingagent/0.1.6/json` 才准。

**CI 矩阵里的 `.python-version`。** 仓库根有 `.python-version = 3.14`，`uv sync` 会照它建 venv。所以 workflow 里必须写 `env: UV_PYTHON: ${{ matrix.python-version }}`，否则 3.11 那个 job 实际跑的是 3.14，矩阵是摆设。

## 7. 当前状态

- 已发布：`0.1.6`（PyPI，`Requires-Python: >=3.11`）
- 已提交未发布：`a742db7`（WS token + 工作区实体化）、`5b2da04`（P0 diff 意图化）、本条（P1 会话运行时）
- 测试：337 passed + 1 skipped（skipped 是仅 POSIX 有效的进程树测试）
- CI：`.github/workflows/publish.yml`，4 个 test job（windows/ubuntu × 3.11/3.14）+ 1 个 publish

## 8. 待做项

### P0 ✅ 已完成：diff 旁路改成"意图化"

**原来的问题**（已实测复现）：`tools._LAST_DIFF` 是个 ContextVar 旁路，`take_diff()` 在每步之后取走。而 `run_post` 排在 `take_diff` 之前，所以后钩子改的文件，diff 会被算到上一个工具头上——一个纯读工具会报出 `+1 −1`。另外 trace 里根本没有 diff 字段，`/trace` 回放看不到改了哪几行。

**现在的形状**

1. `tools.FileChange(path, old|None, new, summary)`：一次改动的意图，还没写盘
2. `write_file` / `edit_file` 只校验并返回 `FileChange`（`edit_file` 失败仍返回 str——失败不是一个改动）
3. `apply_change(change) -> ToolResult(content, diff)`：唯一写盘点，写盘 + 算 diff
4. `loop.py` 执行点：`isinstance(res, FileChange)` 就 apply；diff 在同一处进 `ev.fields["diff"]` 和 `on_tool_result`
5. `_LAST_DIFF` / `take_diff` / `_record_diff` 全部删掉；diff 计算成了纯函数 `_make_diff`

**三个实现时才知道的坑**（都写进代码注释了）

- `ev.fields["diff"]` 必须记在 `timed` 块**内**。事件在 `__exit__` 就落盘，放块外 trace 里永远没有它
- `apply_change` 必须在 `tool_func()` 那个 `try` 里。放外面的话磁盘满 / 权限错会冒到 `turn.run_turn()` 之外，整轮崩
- 越界拦截**留在 `safe_tool`**，没搬去执行点：它返回 str，loop 按类型分派即可；搬过去反而让"包了 safe_tool 的任何调用者"这道检查退化成"只有经 loop 的调用"

**决策 A（后钩子不能改文件）**：`run_post` 的签名没动，钩子手里没有 `apply_change`。P0 之前钩子里调 `edit_file` 会写盘并污染 diff；现在只拿到一个 `FileChange` 然后丢掉——**不写盘、不报错**。这是刻意的：后钩子的职责是改写结果，不是改文件。将来真要做自动格式化，得显式给它句柄，别偷偷把写盘能力放回去。

**验收**（`tests/test_s3.py` 三条）：纯读工具 diff 为空 + 钩子没改成文件；trace 里有 diff；写盘失败回灌给模型不崩。

**已知边界**：`run_shell` 的改动统计不到（`echo x > f` 不产生 diff）。shell 不透明，这一点无法根治，文档里说明即可。

### P1 ✅ 已完成：会话管理去重（"两个店员，没有店长"）

**原来的问题**：`cli.py` 和 `server.py` 各有一份会话编排（新建 / 切换模型 / 存档 / 回放），已经开始分叉——网页端收到新指令时**静默 `continue`**，而新建会话那条分支却会提示"这一轮还在跑"：同一个文件里两个答案。

**现在的形状**：`runtime.SessionRuntime` 收掉了这些编排，两个 UI 只调它。

- `run(goal, callbacks, should_stop)` —— 跑一轮 + **成功才落盘**（`history[old_len:]` 也搬进来了）
- `new_session()` / `resume(sid)` / `switch_model(name)` —— 会话级命令，跑着的时候都抛 `SessionBusy`
- `restore_model(explicit?)` —— "打开会话用哪个模型"（含"原模型已不在配置里"这句提示）
- `usage()` / `trace_records()` / `record_auto()` —— 两个界面原来各算一遍的东西
- 模块级 `choices()` / `prune()` / `clean_trace()` / `trace_age_days()` —— 会话列表与打扫

**"能不能跑"只有一处**：`_claim()` 是个非阻塞锁的上下文管理器。做成"先问再跑"不行——问和跑之间有空隙，网页端两条消息挨着到达时正好撞这个，两边各自问一次就双双通过。

**顺手修的一处**：`switch_model` 原来在跑着的时候也允许，会把一轮拆成"前半段用 A、后半段用 B"，账还记成 B。现在和其他会话级命令一样被拒。

**验收**（都做到）：全仓库搜「还在跑」只剩 `BUSY_MESSAGE` 一处；`cli.py` / `server.py` 不再 import `.session`（`tests/test_runtime.py` 用 AST 盯）；网页端再下指令会**说出口**，不再静默丢。

**已知边界**：`SessionRuntime` 持有 `_lock`，所以"一轮"的最小粒度是整个 turn。真要做"打断当前轮并立刻开新会话"得等 P2 的 mailbox。

### P2：actor 化（"连店面也改成一个柜台"）

把 `SessionRuntime` 变成 mailbox + 事件流：UI 投递命令、渲染事件，不再"问能不能做"。

**现在不做。** 它买的是"加第三个入口零成本"，而现在只有两个入口。

**触发条件**（满足任一）：真出现第三个入口；或者 P1 做完后，又在某个 UI 里写下了「如果正在跑……」。

**风险**：同步循环和 asyncio 的阻抗、事件顺序、丢消息、异常被吞。这些 bug 的表现是"偶尔卡住"，最难 debug。

### P3：知道但不急的小口子

| 口子 | 说明 |
|---|---|
| 前端零测试 | 2.3k 行 TSX，一行测试都没有。要补先选框架（vitest） |
| 后台任务无并发上限 | 模型可以连起 N 个 dev server，`task_list` 只是列出来 |
| `remember` 前缀批准会放大 | 批准过 `git *` 之后 `git push --force` 也自动过 |
| 敏感路径是启发式 | `cat $(echo ~)/.ssh/x` 能绕过。文档里写的是"拦照做的直白写法"，不是边界——**不要把它说成边界** |

## 9. 发布流程

```bash
uv run python scripts/release.py 0.1.7 --no-publish   # 跑测试 → 构建前端 → bump → 打包
git add -A && git commit
git -c "http.https://github.com.proxy=$HTTPS_PROXY" push origin main
# 等 CI 四个组合绿
git tag -a v0.1.7 -m "…" && git -c "http.https://github.com.proxy=$HTTPS_PROXY" push origin v0.1.7
# tag 触发 publish job，用 trusted publishing 发到 PyPI
```

验证：`curl -s https://pypi.org/pypi/lingagent/0.1.7/json`（注意缓存，看具体版本端点）。

## 10. 别做的事

- 写盘只许走 `apply_change`——`tests/test_tools.py::test_only_apply_change_touches_the_disk` 用 AST 盯着，新增写盘路径会红
- 不要让 UI 自己决定"能不能做"——那是 P1 之后要收掉的
- 不要把 `run_shell` 当成沙箱。它的定位是"审批门禁"，没有 OS 级隔离，文档里一直是这么写的
- 不要在文档或注释里把启发式检查说成"边界"。这个项目对这一点很计较：**能被绕过的检查如果冒充边界，比没有更危险**
- 不要为了"顺手"把两个界面共用的逻辑写进其中一个界面

## 11. 沟通约定

报告结论要带证据：测试数、实跑的输出片段、文件行号。不要用"应该没问题""理论上"这类说法。改动前说清楚"改哪里、为什么"，改完说清楚"验证了什么、没验证什么"（比如 UI 改动的视觉部分可能没在浏览器里看过）。

本地记录踩坑过程的日志在 `.workbuddy/memory/`（不进 git），最新的两份是 `2026-11-21.md` / `2026-11-22.md`，需要细节可以去翻。
