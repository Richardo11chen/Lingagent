# Agent Harness 项目约定

## 项目是什么

一个自己转圈干活的 coding agent 引擎：Python 后端把 LLM 封装成会读写文件、跑测试、自我修正、危险动作要人批准的完整系统。

## 开发约定

- 跑测试用 `uv run pytest`（测试都在 `tests/` 目录）
- 代码注释用中文；函数名用英文
- 修改现有文件优先用 `edit_file`；只有新建文件或整体重写才用 `write_file`
- 改完代码必须跑测试验证，绿了才算完成——不要没验证就宣称完成
- 危险命令要人工审批；只能在项目目录内活动

## 结构

- `src/agent_harness/`：`llm`（模型适配器）· `loop`（主循环）· `tools`（工具）· `guardrail`（护栏/围栏）· `sensors`（反馈校验）· `session`（会话持久化）· `compact`（上下文压缩）· `config`（规则加载）· `messages`（Msg 结构）
- `tests/`：`test_s1` ~ `test_s5`、`test_s4b`、`test_s4c`

