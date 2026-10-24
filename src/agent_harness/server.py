"""Web 服务层（S6b）：FastAPI + WebSocket——把 harness 暴露成网页可交互的服务。

架构：每个 WS 连接 = 一个 agent 会话；agent_loop（同步阻塞）跑在旁路线程
（asyncio.to_thread），事件经 asyncio.run_coroutine_threadsafe 推回事件循环 → WS。
"""

import asyncio
import os
import threading
from functools import partial
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .assembly import (
    POST_HOOKS,
    PRE_HOOKS,
    WORKSPACE,
    build_session_tools,
    build_system_prompt,
    list_visible_sessions,
    load_settings_and_llm,
)
from .guardrail import command_head
from .compact import estimate_tokens
from .llm import Msg
from .loop import agent_loop
from .session import (
    load_session,
    load_session_model,
    new_session_id,
    prune_empty_sessions,
    save_messages,
    save_session_model,
    trace_path,
)
from .tools import task_snapshot, task_stop, tool_detail
from .trace import Tracer, TRACE_FIELD_CHARS, ellipsis_note, load_trace, mark_viewed

app = FastAPI()

# 前后端消息版本。加/改 WS 消息（或 /health 里新增 TUI 依赖的字段）时 +1，weblaunch 用它判断
# 「8000 上那个进程是不是旧代码」。别指望端口探测：上一个 TUI 拉起的后端会一直活着，占着端口
# 假装自己是新的。3 = /health 多了 busy（TUI 退出前靠它决定等不等网页那轮）。
PROTOCOL = 3

# 正在跑几轮（进程级）。TUI 退出前问一句：网页上还有人在等回复就先别关，不然那半轮就断了。
_BUSY = {"n": 0}
_BUSY_LOCK = threading.Lock()


def _turn_started() -> None:
    with _BUSY_LOCK:
        _BUSY["n"] += 1


def _turn_ended() -> None:
    with _BUSY_LOCK:
        _BUSY["n"] = max(0, _BUSY["n"] - 1)  # 兜住：重复减不出现负数，免得一直「有人在跑」


@app.get("/health")
def health() -> dict:
    """给 weblaunch 的自检端点：应用名 + 协议版本 + 还有几轮在跑。"""
    with _BUSY_LOCK:
        busy = _BUSY["n"]
    return {"app": "agent-harness", "protocol": PROTOCOL, "busy": busy}

# 工具结果过 WS 的上限：结果与 diff 都是给眼睛看的，不必几十万字符一起推。
# 超限的部分前端拿不到，但 trace 文件里一直是全文（审计账不截断）。
TOOL_RESULT_CHARS = 8_000
TOOL_DIFF_CHARS = 12_000


def _slim(rec: dict) -> dict:
    """把一条轨迹记录压到适合过 WS 的大小——和 TUI 用同一个上限、同一句说明。"""
    out: dict = {}
    for k, v in rec.items():
        if isinstance(v, str) and len(v) > TRACE_FIELD_CHARS:
            out[k] = v[:TRACE_FIELD_CHARS] + ellipsis_note(len(v))
        else:
            out[k] = v
    return out


def list_tasks() -> list[dict]:
    """后台任务快照（底部抽屉用）。任务是进程级全局的，所以不按会话过滤。"""
    from . import tasks as task_reg

    snaps = (task_snapshot(t.id) for t in task_reg.all_tasks())
    return sorted((s for s in snaps if s), key=lambda s: s["started"])


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_running_loop()
    session_id = new_session_id(WORKSPACE)
    history: list[Msg] = []

    def send(payload: dict) -> None:
        """线程 → 事件循环 → WS（run_user 与模型适配器共用）。"""
        asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop)

    llm, settings = load_settings_and_llm(
        build_system_prompt(), _CLI_MODEL,
        on_notice=lambda text: send({"type": "notice", "text": text}),
    )
    approval: dict = {}  # 审批等待槽：{"event": Event, "result": bool, "remember": bool}
    approved_programs: set[str] = set()  # 本连接内"不再问"的程序名（重启即忘）
    auto_approve = {"on": False}  # 网页版「自动批准」（对应 TUI 的 Shift+Tab），内存态
    stop_event = threading.Event()  # 本轮「别继续了」——网页停止按钮置位，loop 每步看一眼
    user_task: asyncio.Task | None = None

    def status_payload() -> dict:
        """顶栏要的那点状态：审批模式 + 上下文用量。

        分子算法与 TUI 完全一致（系统提示 + 历史字符当量），分母是压缩阈值——
        两边各算一套的话，迟早出现"显示 60% 但已经开始压缩"。
        """
        return {
            "type": "status",
            "auto": auto_approve["on"],
            "used": estimate_tokens(history) + len(build_system_prompt()),
            "limit": settings.compact_threshold,
        }

    await ws.send_json({"type": "session", "id": session_id, "history_n": len(history), "model": llm.current})
    await ws.send_json(status_payload())

    def _report_round_failure(task: asyncio.Task) -> None:
        """兜底：run_user 里任何没被接住的异常，都要变成前端看得见的一句话。

        否则前端只在等 assistant_done，界面就永远转圈——排查时毫无线索。
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            send({"type": "error", "text": f"本轮失败：{type(exc).__name__}: {exc}"})

    async def run_user(goal: str) -> None:
        """跑一轮 agent（旁路线程）——回调经事件循环推回 WS。"""

        def on_text(chunk: str) -> None:
            send({"type": "assistant_chunk", "text": chunk})

        def on_tool(name: str, args: dict) -> None:
            detail = tool_detail(args)
            send({"type": "tool", "name": name, "detail": detail[:50]})  # 与 TUI 同一个截断长度

        def on_reasoning(chunk: str) -> None:
            send({"type": "reasoning", "text": chunk})

        def on_hook(by: str, reason: str) -> None:
            send({"type": "hook", "by": by, "reason": reason})

        def on_tool_result(payload: dict) -> None:
            """工具结束：把状态、耗时、输出和改动推给前端。

            这一条是本轮之前缺的——只有「工具开始」的话，前端看到"它开始改 src/tools.py"
            之后就断了，输出和 diff 都无处显示。
            """
            text = str(payload.get("result") or "")
            if len(text) > TOOL_RESULT_CHARS:
                text = text[:TOOL_RESULT_CHARS] + ellipsis_note(len(text))
            diff = str(payload.get("diff") or "")
            if len(diff) > TOOL_DIFF_CHARS:
                diff = diff[:TOOL_DIFF_CHARS] + f"\n…（diff 共 {len(diff)} 字符，已截断）"
            added = removed = 0
            for line in diff.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    removed += 1
            send({
                "type": "tool_result",
                "name": payload.get("name", ""),
                "status": payload.get("status", ""),
                "ms": payload.get("ms", 0),
                "result": text,
                "diff": diff,
                "added": added,
                "removed": removed,
            })

        def approver(command: str, reason: str) -> bool:
            """线程内阻塞等前端答复——收消息 task 唤醒 Event。

            reason 一起推给前端：只给命令的话，批的人不知道自己批的是什么风险。
            本会话"不再问"的记忆存在这里（按程序名），重启即忘。
            """
            if auto_approve["on"]:  # 网页版自动批准：不弹卡片，但必须留一条痕迹
                send({"type": "notice", "text": f"自动批准 · {reason}"})
                tracer.emit("auto", command=command, reason=reason)
                return True
            head = command_head(command)
            if head and head in approved_programs:
                send({"type": "notice", "text": f"本会话已允许 {head}，直接执行"})
                return True
            ev = threading.Event()
            approval["event"] = ev
            approval["result"] = False
            approval["remember"] = False
            send({"type": "approval_request", "command": command, "reason": reason})
            waited = ev.wait(timeout=settings.approval_timeout or None)
            if not waited:  # 没人回答：按驳回处理，别让这一轮永远挂着
                send({"type": "notice", "text": f"审批超时（{settings.approval_timeout:g} 秒无人答复），已按驳回处理。"})
                return False
            if approval["result"] and approval.get("remember") and head:
                approved_programs.add(head)
                send({"type": "notice", "text": f"本会话不再询问 {head}"})
            return approval["result"]

        old_len = len(history)
        # 每轮现取——resume 换 id 后自动跟到新文件；关掉 trace 就不写盘
        tracer = Tracer(trace_path(WORKSPACE, session_id)) if settings.trace_enabled else Tracer()

        def notice(text: str, **fields) -> None:  # 提示 + 留痕（tracer 每轮换，所以每轮重挂）
            send({"type": "notice", "text": text})
            tracer.emit("compact", text=text, **fields)

        llm.set_notice(notice)
        sp = build_system_prompt()
        session_tools = build_session_tools(  # S10：本会话工具表（带 spawn_subagent）
            llm, sp, approver=approver, tracer=tracer,
            max_steps=settings.max_steps, on_hook=on_hook,
            should_stop=stop_event.is_set,
            allow_commands=settings.guardrail_allow,
        )
        run = partial(
            agent_loop,
            goal,
            llm,
            session_tools,
            sp,
            approver=approver,
            history=history,
            on_text=on_text,
            on_tool=on_tool,
            on_tool_result=on_tool_result,
            on_reasoning=on_reasoning,
            tracer=tracer,
            pre_hooks=PRE_HOOKS, post_hooks=POST_HOOKS,
            on_hook=on_hook,
            max_steps=settings.max_steps,
            should_stop=stop_event.is_set,
            allow_commands=settings.guardrail_allow,
        )
        try:
            reply, new_history = await asyncio.to_thread(run)
        except Exception as e:  # 网络断了 / 模型报错 / 参数解析炸了——推给前端，别让界面一直转圈
            send({"type": "error", "text": f"本轮失败：{type(e).__name__}: {e}"})
            return
        history[:] = new_history  # 原地更新（闭包外的引用同步）
        save_messages(WORKSPACE, session_id, new_history[old_len:])
        await ws.send_json({"type": "assistant_done", "text": reply})
        await ws.send_json(status_payload())  # 这一轮的上下文用量

    async def run_user_counted(goal: str) -> None:
        """给 run_user 套个计数器：/health 的 busy 就是它。

        TUI 退出前会问 busy——网页上还有一轮在跑，就先等它跑完再关（否则那半轮被拦腰砍断）。
        """
        _turn_started()
        try:
            await run_user(goal)
        finally:
            _turn_ended()

    try:
        while True:  # 常驻收消息——审批答复也走这里（不能被 agent 执行阻塞）
            msg = await ws.receive_json()
            mtype = msg.get("type")
            if mtype == "user":
                if user_task and not user_task.done():
                    continue  # 上一轮还在跑——忽略
                stop_event.clear()  # 新一轮：把上一轮的「停」清掉
                user_task = asyncio.create_task(run_user_counted(str(msg.get("text", ""))))
                user_task.add_done_callback(_report_round_failure)
            elif mtype == "interrupt":
                # 停止本轮：置位给 loop 的检查点；若正卡在审批上，顺手驳回把它放出来
                stop_event.set()
                ev = approval.get("event")
                if ev:
                    approval["result"] = False
                    ev.set()
            elif mtype == "approval_response":
                ev = approval.get("event")
                if ev:
                    approval["result"] = bool(msg.get("approved"))
                    approval["remember"] = bool(msg.get("remember"))
                    ev.set()
            elif mtype == "new_session":  # 开一个新会话（前端「＋ 新会话」）
                if user_task and not user_task.done():
                    await ws.send_json({"type": "notice", "text": "这一轮还在跑——等它结束再开新会话。"})
                else:
                    # 只换 id 和 history：连接、模型、审批记忆都留着（它们是"你"的东西，不是会话的）
                    session_id = new_session_id(WORKSPACE)
                    history.clear()
                    await ws.send_json(
                        {"type": "session", "id": session_id, "history_n": 0, "model": llm.current}
                    )
                    await ws.send_json({"type": "history", "messages": []})
                    await ws.send_json(status_payload())
            elif mtype == "set_auto":  # 网页版「自动批准」开关（= TUI 的 Shift+Tab）
                auto_approve["on"] = bool(msg.get("on"))
                await ws.send_json(status_payload())
            elif mtype == "models_request":  # 列可选模型（/model）
                await ws.send_json(
                    {"type": "models", "list": llm.profiles, "current": llm.current_profile}
                )
            elif mtype == "switch_model":  # 运行时切换模型（会话级——落盘）
                name = str(msg.get("name", ""))
                try:
                    model = llm.switch(name)
                    save_session_model(WORKSPACE, session_id, name)  # 记在会话上
                    await ws.send_json({"type": "model_switched", "model": model})
                except KeyError:
                    await ws.send_json(
                        {"type": "model_switched", "model": "", "error": f"未知 profile：{name}"}
                    )
            elif mtype == "trace_request":  # S8：轨迹时间线（只读投影——决策 4）
                recs = load_trace(trace_path(WORKSPACE, session_id))
                mark_viewed()
                await ws.send_json(
                    {"type": "trace", "list": [_slim(r) for r in recs[-1000:]], "total": len(recs)}
                )
            elif mtype == "sessions_request":  # 列历史会话（前端启动时问）
                prune_empty_sessions(WORKSPACE)  # 顺手打扫（浏览器每刷新一次就留一个空占位）
                items = [
                    {"id": s["id"], "preview": s["first_user_msg"]}
                    for s in list_visible_sessions()  # 全量下发——前端弹层可滚动
                ]
                await ws.send_json({"type": "sessions", "list": items})
            elif mtype == "tasks_request":  # 后台任务列表（底部抽屉用）——只读快照
                await ws.send_json({"type": "tasks", "list": list_tasks()})
            elif mtype == "task_stop_request":  # 人在界面上按「停止」
                tid = str(msg.get("id", ""))
                await asyncio.to_thread(task_stop, tid)
                await ws.send_json({"type": "tasks", "list": list_tasks()})
            elif mtype == "resume":  # 恢复某个会话
                sid = str(msg.get("id", ""))
                if sid and (user_task is None or user_task.done()):
                    history[:] = load_session(WORKSPACE, sid)
                    session_id = sid
                    saved = load_session_model(WORKSPACE, sid)  # 会话级模型：跟着会话走
                    if saved and saved in settings.models and saved != llm.current_profile:
                        llm.switch(saved)
                    await ws.send_json(
                        {"type": "session", "id": session_id, "history_n": len(history),
                         "resumed": True, "model": llm.current}
                    )
                    # 把历史渲染给前端（user/assistant 正文；tool 结果只给提示行——可能很长）
                    rendered = []
                    for m in history:
                        if m.role == "user":
                            rendered.append({"kind": "user", "text": m.content})
                        elif m.role == "assistant" and (m.content or "").strip():
                            rendered.append({"kind": "assistant", "text": m.content})
                        elif m.role == "tool":
                            rendered.append({"kind": "tool", "name": m.tool_name or "工具结果"})
                    await ws.send_json({"type": "history", "messages": rendered})
                    await ws.send_json(status_payload())  # 换会话后用量跟着变
    except WebSocketDisconnect:
        pass
    finally:
        # 连接没了 = 没人能回答那条审批：放掉阻塞的线程（否则每断一次线泄漏一个）
        ev = approval.get("event")
        if ev is not None:
            approval["result"] = False
            ev.set()


# 打包形态：前端构建产物跟着包走（src/agent_harness/web_dist），后端一个端口全包
# ——装完不用 Node、不用 vite，直接开 http://127.0.0.1:8000。
WEB_DIST = Path(__file__).parent / "web_dist"


def _mount_web_ui() -> None:
    """挂前端路由。构建产物在就托管，不在就是纯 API（开发时前端在 5173，由 vite 顶着）。

    路由无条件注册、响应时才看文件在不在：否则测试和线上会因为"dist 有没有构建过"走出
    两套不同的 URL 表，藏 bug。
    """
    if (WEB_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def _ui_index():
        return _web_dist_response(WEB_DIST / "index.html")

    @app.get("/{path:path}", include_in_schema=False)
    def _ui_file(path: str):
        """dist 里的真文件发文件，其余一律回 index.html（前端自己按路径渲染）。"""
        target = (WEB_DIST / path).resolve()
        try:
            target.relative_to(WEB_DIST.resolve())  # 挡住 ../ 穿越
        except ValueError:
            target = WEB_DIST / "index.html"
        return _web_dist_response(target if target.is_file() else WEB_DIST / "index.html")


def _web_dist_response(path: Path):
    if not path.is_file():
        raise HTTPException(status_code=404, detail="网页界面没打包（只跑 API）")
    return FileResponse(path)


_mount_web_ui()


def main() -> None:
    import argparse

    from .settings import load_settings

    ap = argparse.ArgumentParser(description="Agent Harness Web")
    ap.add_argument("--model", help="模型 profile 名（配置 [models.*]；缺省用 default）")
    args = ap.parse_args()

    s = load_settings()
    name = args.model or s.default_model
    if args.model and args.model not in s.models:  # fail fast：别等 WS 连上才在 handler 里炸（前端只看到连接即断）
        raise SystemExit(f"未知模型 profile：{args.model}（可用：{', '.join(s.models)}）")
    if args.model:
        global _CLI_MODEL
        _CLI_MODEL = args.model  # ws_endpoint 构造 llm 时用
    # 同样在这里把密钥也验掉：否则要等浏览器连上那一刻才 SystemExit，前端只看到「连接闪断」
    p = s.model(name)
    if not p.api_key and not os.environ.get(p.api_key_env):
        raise SystemExit(
            f"缺少 API key：profile {name} 没写 api_key，环境变量 {p.api_key_env} 也没设置"
        )
    where = (
        f"http://{s.server_host}:{s.server_port}"
        if (WEB_DIST / "index.html").exists()
        else "http://localhost:5173（开发模式：在 web/ 下 npm run dev）"
    )
    print(f"网页界面: {where}")
    uvicorn.run(app, host=s.server_host, port=s.server_port)


_CLI_MODEL: str | None = None  # --model 指定（None = 配置 default）


if __name__ == "__main__":
    main()
