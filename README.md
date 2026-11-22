# Lingagent

一个自己干活的 coding agent 引擎：LLM 只做「下一步做什么」的决策，其余（工具分发、治理拦截、反馈回灌、记忆读写、停机）全是自己写的工程。

核心等式：**Agent = LLM + Harness**。

- 终端界面（TUI）+ 网页界面（React，实时流 + 网页审批）
- 危险动作走风险分级：只读放行 / 改动静默问人 / 极危险直拒
- 每步动作留轨迹（trace），会话可恢复（session）
- 硬标准：**移除真 LLM 后核心机制仍能用确定性单测验证**（337 条测试）

> 接手开发（人或者模型）先读 [`HANDOFF.md`](HANDOFF.md)：架构地图、不可破坏的约定、踩过的坑、待做项。这份 README 是**用**这个工具的说明。

## 环境要求

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | **3.11+** | 3.11 是真实下限（`tomllib` 进标准库的版本）；`.python-version` 锁的 3.14 只是开发默认值。CI 两个版本都跑 |
| uv | 0.12+ | 包管理与运行入口 |
| Node.js | 20+ | 只有网页前端需要（`web/`） |

## 安装

```bash
git clone <repo> && cd AgentHarness
uv sync
```

## 安装（当产品用：装完在任意目录 `lingan` 就能启动）

不打成「仓库里跑」的形态，而是装成一条命令——像 `claude`、`gh` 那样。

**装上就用（PyPI 上的最新版）：**

```bash
uv tool install lingagent     # 或 pipx install lingagent
```

**从源码装（改了代码、或想跑未发布版本时）：**

```bash
uv run python scripts/build_web.py   # 把前端构建产物打进包里（需要 Node）
uv build                             # 产出 dist/lingagent-0.1.3-py3-none-any.whl
uv tool install dist/lingagent-0.1.3-py3-none-any.whl
```

装完先做一次配置（交互式，问三句就写好 `~/.agent-harness/config.toml`）：

```bash
lingan --init
```

之后随便 `cd` 到哪个目录：

```bash
lingan             # 启动终端界面（会话 / 轨迹按当前目录的项目身份归档）
lingan -tui        # 同上，显式指定（不加参数也是这个）
lingan -web        # 只起后端 + 网页界面
lingan --stop-web  # 关掉后台的后端
```

装好的包**不需要 Node、不需要仓库、不需要 web/**：前端构建产物跟着 wheel 走，后端一个端口
（`http://localhost:8000`）就把界面和 WebSocket 一起发出去。开发时（在仓库里）仍走 vite 5173 热更新。

卸载：`uv tool uninstall lingagent`。

**为什么从源码装要三行**：`web_dist/` 不提交进 git（构建产物不该进版本库），包里得现塞前端，
所以要先 `build_web`、再 `uv build`。PyPI 上那条路不用管这些——wheel 是 CI 打好发上去的。

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

### 全部可调项

完整带注释的示例在 `harness.example.toml`，这里是一张速查表。

**模型 profile（`[models.名字]`，`[model] default` 指定默认用哪个）：**

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | `deepseek-flash` | 发给 API 的 `model` 字段（中转站代理谁就写谁） |
| `base_url` | `https://api.deepseek.com` | OpenAI 兼容端点；换厂商/中转站改这里 |
| `api_key_env` | `DEEPSEEK_API_KEY` | 从哪个环境变量读 key（推荐） |
| `api_key` | 无 | 直写明文，优先于 `api_key_env`；别提交进仓库 |
| `temperature` | `1.0` | 采样温度（思考模式下不生效） |
| `thinking` | `off` | DeepSeek 私有开关：`enabled` / `disabled` / `off`（off = 不传） |
| `reasoning_effort` | `off` | 思考强度：`low` / `high` / `max` / `off`（off = 不传） |
| `strip_think_tags` | 关 | 正文里包 `<mm:think>` 的厂商（如 MiniMax）才开 |

**全局（跟模型无关）：**

| 段 | 字段 | 默认 | 说明 |
|---|---|---|---|
| `[loop]` | `max_steps` | `40` | 一轮最多多少个「模型回合」 |
| | `approval_timeout` | `300` | 审批没人答复的秒数（`0` = 一直等） |
| `[context]` | `compact_threshold` | `300000` | 软阈值（字符当量，非精确 token），超了触发微压缩 |
| | `keep_last` | `5` | 微压缩保留最近 N 条工具结果 |
| | `snip_keep_groups` | `3` | 激进档（413 兜底）保留最近 K 组 |
| `[server]` | `host` | `127.0.0.1` | 后端监听地址 |
| | `port` | `8000` | 后端端口（改了网页端 `VITE_WS_PORT` 要跟上） |
| `[web]` | `autostart` | `true` | 进 TUI 自动拉起网页端 |
| | `stop_on_exit` | `true` | 退 TUI 收掉这次自己拉起的进程 |
| | `stop_wait` | `120` | 收之前先等网页那轮跑完的秒数（`0` = 不等） |
| `[trace]` | `enabled` | `true` | 写轨迹文件（关掉只留 session，省磁盘） |
| `[guardrail]` | `allow` | `[]` | 不再问人的命令（前缀语义，如 `git status *`；写 `git *` 会连 push 一起放行） |

关于 `compact_threshold` 补一句：它按**字符数当量**粗估（中文≈1:1、英文保守高估），不是精确 token。调太低会过早压缩——丢细节、还打断 DeepSeek 的前缀缓存；调太高可能撞上模型真实的上下文窗口上限。默认 `300000` 是个折中。

## 运行

**终端界面：**

```bash
uv run lingan                          # 用配置里的 default profile
uv run lingan --model deepseek-v4-pro  # 临时指定 profile
```

首次启动会：列出历史会话让你选（`0` 开新会话、`n`/`p` 翻页）→ 自动拉起网页端（`[web] autostart = false` 可关）。

界面内命令：输入 `/` 会弹出命令菜单——`/model` 切模型、`/trace` 看本轮轨迹（`/trace N` 钻取单条）。
跑一轮的时候按 `Ctrl+C`：**跑完手里这一步就停**，已经做完的部分照常落盘；再按一次才立刻中断（那一轮不落盘）。
空闲时退出：`Ctrl+C` 连按两次，或 `Ctrl+D`。

**网页界面：**

```bash
# 终端 1：后端（默认 127.0.0.1:8000）
uv run lingan -web

# 终端 2：前端（默认 5173）
cd web && npm install && npm run dev
```

打开 <http://localhost:5173>。由 TUI 自启时这两步都不用手动做。

> 改了 `[server] port` 的话前端也要跟上：在 `web/.env` 里写 `VITE_WS_PORT=新端口`（不写默认 8000）。

## 测试

```bash
uv run pytest                  # 全量（323 条）
uv run ruff check src tests    # 静态检查（E/F/W + import 排序 + pyupgrade + bugbear）
cd web && npx tsc --noEmit     # 前端类型检查
```

ruff 只上了能抓真问题、几乎不误报的那几组，**没上 mypy**——类型标注已经很全，但一次性让 8k 行过严格类型检查只会产出一堆噪音，不如先把 lint 用起来。

`schema.py` 单独豁免了行宽：那里的工具描述是整段中文 prompt，按 120 列硬断会把它拆碎。

CI 在 Windows 和 Ubuntu 上各跑 3.11 / 3.14 四个组合的 `uv run pytest`，静态检查只在一个组合上跑（它跟解释器版本无关）：PR 和推到 main 都跑，打 tag 时**先过测试再发布**
（PyPI 不许重传同名文件，坏包发出去就永久占住那个版本号）。

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
   uv run lingan --clean-trace 30   # 删 30 天前的轨迹（0 = 全删）
   ```

   删轨迹不影响会话本身。要彻底不写，配置里 `[trace] enabled = false`——session 照常，只是没有"给人看的账"。TUI 启动时如果发现轨迹一个月没人看过，会提醒一句。

真发生压缩时会往对话里插一行提示（`⟳ 上下文偏长…`），不会静默改小上下文。

压缩分三档，从轻到重：

| 档 | 何时 | 做什么 | 丢什么 |
|---|---|---|---|
| 单条上限 | 每次发送前 | 超过 50k 字符的**一条**消息留头 30k + 尾 10k，中间标「省略 N 字符」 | 中间那段 |
| 微压缩 | 总量超 `compact_threshold` | 清掉 `read_file` / `run_shell` / `run_tests` 的旧结果（留最近 5 条） | 旧工具输出 |
| 激进档 | 服务端返回 413 | 保首条 + 最近 3 组，中间整段省略，再清空所有旧结果，重试一次 | 整段中间对话 |

三档都是**投影**：只改发给 API 的副本，历史原样保留——翻历史、写 session 都还是全文。

没有做 LLM 摘要旧历史，理由是钱：deepseek-flash 缓存命中 0.02 元/M、未命中 1 元/M，**差 50 倍**。摘要会重写前缀，等于每轮都让缓存失效、全量重算输入——为了省上下文把账单涨两个数量级。

关于结果长度：进模型上下文（history / session）的那份截断到 32k 字符（头 24k + 尾 8k，中间注明省略）；**被省略的中间那段不丢**——落在项目根的 `.agent-harness-out/`（已 gitignore，留 7 天）并把路径写进提示，模型可以直接 `read_file` 或 `sed -n` 读那一段，不用把原命令重跑一遍。trace 里保留全文。读取侧也有护栏——单条超过 2MB 的事件不解析（只显示摘要），网页端单字段最多 2k 字符。

## 网页端

打开 `http://localhost:5173`，界面分三块：

- **左栏**：历史会话，按「今天 / 昨天 / 日期」分组，上面有个搜索框（只搜标题）；可折叠成 56px
- **中间**：对话。assistant 不套气泡，工具调用折成一行（`▸ edit_file src/x.py +2 −1`）——点开看 diff，右侧看输出与耗时；输入框上方的 `＋ 新会话` 直接换会话，不刷新页面
- **工具在跑**：那一行右侧显示 `◌ 运行中 · 12s`（秒数由前端自己数，服务端不发心跳）；如果它是卡在等你批准，改显示 `等待批准`——**没开始跑却写"运行中"是撒谎**
- **思考过程默认收起**：只占一行 `▸ 思考过程　1842 字`，点开才铺开——它经常比回答长三倍
- **右栏**：选中那条工具的详情（参数 / 改动 / 输出 / 耗时），窄屏自动收起
- **顶栏**：会话开始时间、模型（点开可切）、在线状态、上下文用量、自动批准开关
- **底部**：`轨迹 / 后台任务` 抽屉，收起时只剩一条 tab；后台任务上有运行中的数量

窄屏（≤1180px 收右栏、≤900px 收左栏宽度）都能用，不做手机版——它不是给手机用的。

**只接受本机来源的连接，而且要出示 token。** 两道门，各管一类对手：

- **Origin 挡浏览器。** WebSocket 不受同源策略约束（没有预检、没有 CORS），任何网页都能对 `ws://127.0.0.1:8000/ws` 发起连接——能不能连上，取决于**服务端**验不验 `Origin`。这里只放行 `localhost` / `127.0.0.1` / `::1` 的页面，`Origin: null`（sandbox iframe、`file://`）直接拒。
- **token 挡非浏览器。** 每次启动生成一个随机 token，WS 必须带 `?token=…`。页面通过 `GET /api/token` 取——**这条端点没有 Origin 就 403**，所以局域网里一条 `curl` 拿不到钥匙。

两道都要，缺一不可：只有 Origin 的话，`server_host` 改成 `0.0.0.0` 之后，局域网里的 curl 不带 Origin，照样能连；只有 token 的话，token 得放在页面能读到的地方，而那地方正好是浏览器能访问的。

从局域网或手机访问时，还要把地址写进 `[server] extra_origins`：

```toml
[server]
extra_origins = ["http://192.168.1.5:8000"]
```

端口不校验（dev server 是 5173、生产是 8000），因为攻击者换不了 `Origin` 里的 hostname——挡的是"谁的页面"，不是"哪个端口"。

后端和前端 dev server 都是后台进程（不弹窗、日志吞掉）。它们跟 TUI **同生共死**：退出 TUI（`exit` /
Ctrl+C 两次 / Ctrl+D / 关窗口）时，这次启动拉起来的会一起收掉，端口当场释放：

```
网页端已关闭：后端已结束（PID 2652）
```

只收**自己拉的**：如果进 TUI 时端口已经有人（另一个 TUI 起的、或你手动起的），复用它，退出时不动它。

**网页上还有一轮在跑时会先等它**（不是干等到底：`[web] stop_wait` 秒，默认 120，Ctrl+C 当场不等）：

```
网页上还有 1 轮在跑——等它写完再关（最多 120 秒，Ctrl+C 直接关）
网页端已关闭：后端已结束（PID 2652）
```

不等的话那一轮会被拦腰砍断：工具可能正改文件、trace 停在半截、浏览器里的人在等一个永远不来的回复。

想把网页端留着自己用（关掉终端后在浏览器里继续聊），在配置里关掉这一条：

```toml
[web]
stop_on_exit = false   # 默认 true
stop_wait = 120        # 收之前等网页那轮跑完的秒数（0 = 不等）
```

**TUI 被强杀**（`kill -9` / 任务管理器结束 / 关掉整个终端窗口）时 `finally` 跑不到，进程会变成孤儿。
下次启动会自己认领：`~/.agent-harness/web.json` 记着"哪个 TUI 拉起了哪些 pid"，那个 TUI 已经没了、
端口还占着、`/health` 还答 agent-harness——三条都成立才收，然后印一行

```
收掉上次遗留的进程：后端已结束（上次遗留 PID 2652）
```

也可以手动收（不需要 API key）：

```bash
uv run lingan --stop-web      # 结束占用 8000 / 5173 的进程
```

后端复用前 TUI 会先问一句 `/health`：如果 8000 上的进程不是当前版本（协议号对不上），启动时就印一行黄字提醒，而不是假装一切正常。

## 策略钩子

工具执行前后各有一个挂载点（`hooks.py`）：

| 时机 | 签名 | 能力 |
|---|---|---|
| 执行前 | `fn(action) -> Verdict \| None` | `deny("原因")` 拦下，原因原样回灌给模型 |
| 执行后 | `fn(action, result) -> str \| None` | 返回新字符串 = **替换结果**（脱敏这类） |

失败语义是两个方向：预钩子抛异常 = **默认拦截**（策略坏了宁可停），后钩子抛异常 = 吞掉（观察类不拖垮主流程）。

内置两条：

- **`forbid_env_writes`**（前）：`.env` 家族不许经工具改——要改动让人手动来
- **`redact_secrets`**（后）：输出里的密钥打码后再往下走

第二条解决的是一个具体的泄漏路径：模型随手跑个 `env` 或 `cat ~/.npmrc`，整份环境变量会同时进**上下文**、**界面**和 **trace**——而 trace 是不截断的审计账，等于把密钥永久写到了磁盘上。所以脱敏必须发生在写账之前（`loop.py` 里后钩子排在 `ev.fields["result"]` 之前）。

规则只认高置信度形状（`sk-` / `ghp_` / `xoxb-` / `AKIA` + 足够长度）：**宁可漏一个，也不要把正常文本打成星号**——误伤一次，人就再也不信这个打码了。

加策略只往 `assembly.py` 的 `PRE_HOOKS` / `POST_HOOKS` 名单里塞函数，loop 一个字不用动。

## 已知边界（有意不做的）

- **没有 OS 级沙箱**：`run_shell` 靠风险分级 + 人工审批，不是容器隔离。别拿它跑不可信的任务。
- **路径围栏只管文件工具**：`read_file` / `write_file` / `edit_file` 越界直接拒；`run_shell` 不受它约束（`cd ..` 之后干什么都能干）。命令里出现界外路径时，审批理由会写一句「引用了工作区外：…」——**那是提示不是拦截**，shell 里换个写法就绕过去了，别把它当边界。
  **唯一的例外是凭据文件**：只读命令一旦引用 `.ssh/`、`id_rsa`、`.aws/credentials`、`.npmrc`、`.kube/config` 这类路径，会从「自动放行」降级成「问一次」。原因是只读清单里有 `cat`/`head`/`rg`，不降级的话 `cat ~/.ssh/id_rsa` 会**静默执行**——一句提示词注入就能把私钥送进上下文。这条排在 `allow` 规则之前（`allow = ["cat *"]` 放不出私钥）；字符串匹配当然能被绕过（`cat $(echo ~)/.ssh/x`），它拦的是照做的直白写法。
- **审批会超时**：默认 5 分钟没人答复就按驳回处理（`[loop] approval_timeout`，设 0 = 一直等）。
- **「停止」只在本步结束后生效**：网页点按钮、终端按第一次 `Ctrl+C`，都是「跑完手里这一步就不再发起新动作」（不硬杀进程；终端按第二次才是硬中断）。Windows 上这台机器的 `Ctrl+C` 是控制台事件，子进程也会收到，所以正在跑的 shell 命令仍可能被打断——软中断保证的是「不再开新步 + 这一轮已有结果照常落盘」。
- **子 agent 的「只读」靠提示词**，工具是全量的；它不能再派生孙 agent（深度上限 1）。
- **子 agent 的结论才进主上下文**，过程不进。但过程**看得见**：终端里缩进成 `⤷` 一行行滚（工具行 + 正在写的字），网页端以 notice 形式显示它的工具活动。看完整细节仍去 `/trace`。
- **Linux / macOS 上找「谁占着端口」靠 `lsof`**：它不在 Python 依赖里，极简镜像（Alpine、最小容器）默认可能没有。缺了不报错——只是认不出僵尸后端，`--stop-web` 会变成"没找到"。`apt install lsof` / `apk add lsof` 即可。
- **`read_file` 读不了图片**：它只解文本，图片字节读出来是乱码。看图要用 `view_image`（见下节）——但它是下采样后转述，读不清小字、也做不了像素级比对；那种需求仍然要写脚本算指标。
- **`/tmp` 不是同一个目录**：`run_shell` 走 Git Bash（`/tmp` → `%TEMP%`），而 node 这类原生程序把 `/tmp` 解析到当前盘根（`D:\tmp`）。跨进程传路径用绝对路径。
- **后台任务只在内存里**：进程重启就清空；而且 TUI 与网页后端是两个进程、各自编号（两边都有 `t1`），在一个入口起的任务，另一个入口查不到。
- 只接 OpenAI 兼容协议面；DeepSeek 私有参数（`thinking` / `reasoning_effort`）默认不传。

## 审批怎么答

危险动作会停下来问你一句，只认三个键：

问题分两行：先给**为什么要问**（`改动文件：rm（引用了工作区外：../other）`），再给完整命令——只给命令等于让人盲批。

| 键 | 含义 |
|---|---|
| `Enter` | 执行这一次 |
| `Esc` | 驳回（也可以打 `n` 回车） |
| `a` + `Enter` | 执行，并且**本次会话不再问这个程序** |

网页端是三个按钮：批准 / 驳回 / 本会话不再问。

记住的范围是"程序名"（`git`、`uv`、`del`），不是整条命令；只存在内存里，**重启就忘**。想让某些命令永远不问，写进配置：

```toml
[guardrail]
allow = ["git status *", "git diff *", "rg *"]   # 前缀语义：整条命令以它开头就不再问
```

**`allow` 是前缀匹配，不是"这一条命令"**。写 `git *` 等于把整个 git 放行——`git push`、`git reset --hard`、`git config --global` 都不会再问你，而这几条恰恰最该问。想只放行看图那几条，就把子命令写进去（上面那样）；`uv *` 同理，会连 `uv add`（改依赖）一起放过。

`catastrophic` 档（`format` / `mkfs` / `dd` / `rm -rf /`）**不受 allow 影响**——写 `allow = ["*"]` 也放不出来，这是刻意留的地板。

### 自动批准（Shift+Tab）

在提示行按 `Shift+Tab`：底部那条横线整条变黄，写着 `⚡ 自动批准 · Shift+Tab 关闭`。这时所有审批自动通过，屏幕上留一行 `⚡ 自动批准 · 改动文件：rm`，`/trace` 里也有一条 `⚡ 自动批准`。

三条边界：

- **`catastrophic` 不受影响**：`format` / `mkfs` / `dd` / `rm -rf /` 仍然直接拒——它由主循环在执行前拦掉，连审批都不会进，自动批准碰不到那条路。
- **只活在内存里**：重启回到手动，不写配置、不跨会话。
- **要在提示行按**：运行中屏幕归流式渲染，`Shift+Tab` 到不了输入行；正在等你审批时也不行（那三个键只管这一条命令）。

网页端顶栏有同一个开关（`自动批准` → 开启后变成 `⚡ 自动批准`，整颗变黄），语义与 TUI 的 `Shift+Tab` 完全一致：不弹审批卡、命令照跑、对话里留一行 `自动批准 · 改动文件：rm`、trace 里记一条 `kind: auto`。它也是内存态，关掉页面或重连就回到手动。

底部那条横线还会显示上下文用量：

```
────── 上下文 8.4k/70k (12%) · Shift+Tab 自动批准
```

- 左边是**当前**（系统提示 + 历史的字符当量，和压缩判断用同一个估算函数）
- 右边是**会触发压缩的阈值**（`[context] compact_threshold`，不是模型的物理窗口）
- 括号里是占比；超过 70% 转黄、95% 转红——不必等它变成"这一轮怎么这么慢"才发现

网页端把同一个数字放在顶栏（`8.4k/70k (12%)`），算法与 TUI 共用：两边各算一套的话，迟早出现"显示 60% 但已经开始压缩"。

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

**超时杀的是整棵进程树，不只是 shell。** Windows 用 `taskkill /T`；POSIX 把命令放进独立进程组（`start_new_session`）再 `killpg`。不这么做的话，`npm run build` 起的 node、pytest 起的 worker 会活下来继续占端口——而工具报告说"已被终止"，人只会困惑"为什么端口还被占着"。

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

## 联网搜索与看图

两个工具，都要在配置里开或配好才会出现在菜单里（配不出来就**不注册**，模型看不见，不会去调）。

```
web_search("DeepSeek API 定价")   → 标题 / 链接 / 摘要（不返回正文）
fetch_url("https://…")            → 该页正文，转成纯文本，截断 8k
view_image("shot.png", "有没有报错？") → 文字描述
```

**搜索源不绑定厂商**。`[search] provider` 支持 `deepseek`（复用模型那把 key）、`tavily`、`brave`、`searxng`（自建），`auto` 的默认顺序是：当前 profile 是 DeepSeek 就用它 → 否则看环境里有没有 `TAVILY_API_KEY` → 都没有就不开。

```toml
[search]
provider = "auto"        # auto | deepseek | tavily | brave | searxng | off
max_results = 5
snippet_budget = 1500    # 所有摘要加起来的字符预算，按条数均分（5 条 → 每条 300）
# api_key_env = "TAVILY_API_KEY"   # 空 = 按 provider 约定
# searxng_url = "http://localhost:8080"
```

摘要按**条数均分预算**，而不是每条固定字数：只拿到 2 条时每条能分到 750 字，拿到 10 条时每条 150 字——两头都不浪费、也不挤爆上下文。截断会**停在句号上**并带 `…`，因为在词中间砍断的半个句子会被模型当成完整事实。摘要里的 markdown 标记（标题井号、链接语法、图片）先压掉再算长度。

Brave 和 SearXNG 不提供正文抓取，`fetch_url` 会自动落到内置抓取器——它带 **SSRF 挡板**（拒绝 loopback / 私网 / link-local / 云元数据，每一跳重定向都重验），只交纯文本。

**看图**走的是"子调用"：`view_image` 自己发一次带图的请求，主对话历史里只留那段文字。原因是协议限制——图片只能出现在 user 消息的 content blocks 里，而工具结果的 role 是 `tool`。

```toml
[models.flash]
name = "deepseek-flash"
vision = "auto"          # auto | true | false（auto = 发一次试试，失败时按服务端报错说清）

[tools]
vision_profile = ""      # 空 = 用当前模型；主模型是推理型看不了图时，指一个能看的
```

清晰度**按问题自适应**：问"报错写的什么""标题文字"这类要读字的，自动用 `detail=high`（保原图，约 1024 token 封顶）；问"布局对不对""主色调是什么"用 `detail=low`（下采样 512×512，便宜三到五倍）。工具也可以显式传 `detail`。

用 low 时返回结果末尾会带一句"这是 512×512 缩略图，要看细节再传 high"——模型看到这句才有机会补一次，而不是拿着缩略图硬猜小字。

两个都验证过真机：搜索 / 抓取用 DeepSeek 的 `/v1/tools/*`，看图用 `deepseek-flash`（左红右蓝的测试图，答对了）。

## 常见坑

- **同一台机器上 `find` 有两个**：在 Git Bash 里 `find` 是 GNU 的（找文件），`cmd` 里是 Windows 的（逐行搜文本）。跑命令的是 bash，所以 `find . -name "*.py"` 才对；`find /c /v ""` 那套是 cmd 语法，在这里会把 `*.py` 当路径（2026-10-19 就这么灌出过 91MB 输出）。不确定就 `command -v find` 看一眼。
- **`write_file` 覆盖整个文件**，改已有文件用 `edit_file`。
- 项目约定写在 `AGENTS.md`，每轮请求前重读并注入 system prompt——改完立刻生效，两个入口都不用重启。
- `~/.agent-harness/config.toml` 相反：**进程启动时读一次**（网页端是每个连接读一次），改了要重启/重连才生效。提示词每轮读、配置不重读，这个差别是刻意的——配置里是端口、密钥、模型表，中途换掉会让已经在跑的那一轮前后不一致。

## 目录结构

```
src/agent_harness/
  __init__.py   包根：只有身份说明，不导入任何运行时模块
  cli.py        CLI 主入口（分派 / 选会话 / REPL / 落盘 / 收网页端）
  turn.py       一轮编排：装配 → agent_loop（TUI 与 Web 共用，只统一顺序不统一界面）
  loop.py       主循环：护栏 → 预钩子 → 执行 → 回灌 → 停机
  guardrail.py  命令风险分级 + 路径围栏
  approval.py   审批的会话记忆（记住哪个程序不再问；终端与网页共用这一份）
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
  workspace.py  工作区（项目根）——一个显式的值，不是各处 Path.cwd()
  assembly.py   公共装配 + SwitchableLLM
  tui.py        终端界面
  server.py     WebSocket 会话协议（一个连接 = 一个会话）
  webapp.py     HTTP 面：探活、静态界面托管
  weblaunch.py  TUI 启动时拉起网页端
tests/          test_s1 ~ test_s11（含 s4b/s4c/s6a/s6b）+ tools / contract / package / turn / approval 等（323 条）
web/            React + TS + Vite 前端
```
