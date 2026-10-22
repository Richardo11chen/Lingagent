# Agent Harness 项目约定

## 项目是什么

一个自己转圈干活的 coding agent 引擎：Python 后端把 LLM 封装成会读写文件、跑测试、自我修正、危险动作要人批准的完整系统。

## 开发约定

- 跑测试用 `uv run pytest`（测试都在 `tests/` 目录）
- 代码注释用中文；函数名用英文
- 修改现有文件优先用 `edit_file`；只有新建文件或整体重写才用 `write_file`
- 改完代码必须跑测试验证，绿了才算完成——不要没验证就宣称完成
- 危险命令要人工审批；只能在项目目录内活动
- `run_shell` 跑在 **Git Bash**（不是 cmd.exe）：bash 语法、正斜杠路径、`find` 是 GNU 的（能 `-delete`/`-exec`，也会把整盘遍历一遍——限制范围）。`dir`/`type`/`del` 这些 cmd 内建命令不存在，用 `ls`/`cat`/`rm`
- 不会自己结束的命令（dev server / watch）用 `run_shell(background=true)` 起，`task_output` 看输出、`task_stop` 停——前台等它们只会等到超时

## 结构

- `src/agent_harness/`：`llm`（模型适配器，OpenAI 兼容）· `loop`（主循环）· `tools`（工具 + 结果截断 + 后台任务）· `shell`（挑 shell）· `tasks`（后台任务登记簿）· `paths`（应用目录）· `guardrail`（护栏/围栏）· `sensors`（反馈校验）· `session`（会话持久化）· `trace`（轨迹）· `hooks`（策略挂载）· `subagent`（子 agent）· `compact`（上下文压缩）· `config`（规则加载）· `messages`（Msg 结构）· `settings`（多模型配置）· `assembly`（公共装配 + SwitchableLLM）· `tui`（终端界面）· `server`（Web 服务）· `weblaunch`（拉起网页端）
- `tests/`：`test_s1` ~ `test_s11` + `test_tools`（含 `test_s4b` / `test_s4c` / `test_s6a` / `test_s6b` / `test_s10` / `test_s11`），169 条
- 文档在 `docs/`（PLAN 工程真源 / HANDOFF 交接单）；根目录只留 README、AGENTS、CLAUDE 这类「工具要读」的文件
- 网页端：后端 `uv run agent-harness-web`（FastAPI，默认 127.0.0.1:8000）；前端 `cd web && npm run dev`（Vite + React）
- 结果长度约定：trace 存全文（审计账）；进 history / session 的那份截断到 32k（`tools.cap_result`）

