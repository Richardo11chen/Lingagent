"""Web 服务层（S6b）：FastAPI + WebSocket——把 harness 暴露成网页可交互的服务。

架构：每个 WS 连接 = 一个 agent 会话；agent_loop（同步阻塞）跑在旁路线程
（asyncio.to_thread），事件经 asyncio.run_coroutine_threadsafe 推回事件循环 → WS。
"""

import asyncio
import os
import threading
from functools import partial

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .assembly import (
    POST_HOOKS,
    PRE_HOOKS,
    WORKSPACE,
    build_session_tools,
    build_system_prompt,
    list_visible_sessions,
    load_settings_and_llm,
)
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
from .trace import Tracer, TRACE_FIELD_CHARS, ellipsis_note, load_trace, mark_viewed

app = FastAPI()


def _slim(rec: dict) -> dict:
    """把一条轨迹记录压到适合过 WS 的大小——和 TUI 用同一个上限、同一句说明。"""
    out: dict = {}
    for k, v in rec.items():
        if isinstance(v, str) and len(v) > TRACE_FIELD_CHARS:
            out[k] = v[:TRACE_FIELD_CHARS] + ellipsis_note(len(v))
        else:
            out[k] = v
    return out


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
    approval: dict = {}  # 审批等待槽：{"event": Event, "result": bool}
    stop_event = threading.Event()  # 本轮「别继续了」——网页停止按钮置位，loop 每步看一眼
    user_task: asyncio.Task | None = None

    await ws.send_json({"type": "session", "id": session_id, "history_n": len(history), "model": llm.current})

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
            detail = str(args.get("command") or args.get("path") or "")
            send({"type": "tool", "name": name, "detail": detail[:50]})  # 与 TUI 同一个截断长度

        def on_reasoning(chunk: str) -> None:
            send({"type": "reasoning", "text": chunk})

        def on_hook(by: str, reason: str) -> None:
            send({"type": "hook", "by": by, "reason": reason})

        def approver(command: str, reason: str) -> bool:
            """线程内阻塞等前端答复——收消息 task 唤醒 Event。

            reason 一起推给前端：只给命令的话，批的人不知道自己批的是什么风险。
            """
            ev = threading.Event()
            approval["event"] = ev
            approval["result"] = False
            send({"type": "approval_request", "command": command, "reason": reason})
            waited = ev.wait(timeout=settings.approval_timeout or None)
            if not waited:  # 没人回答：按驳回处理，别让这一轮永远挂着
                send({"type": "notice", "text": f"审批超时（{settings.approval_timeout:g} 秒无人答复），已按驳回处理。"})
                return False
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
            on_reasoning=on_reasoning,
            tracer=tracer,
            pre_hooks=PRE_HOOKS, post_hooks=POST_HOOKS,
            on_hook=on_hook,
            max_steps=settings.max_steps,
            should_stop=stop_event.is_set,
        )
        try:
            reply, new_history = await asyncio.to_thread(run)
        except Exception as e:  # 网络断了 / 模型报错 / 参数解析炸了——推给前端，别让界面一直转圈
            send({"type": "error", "text": f"本轮失败：{type(e).__name__}: {e}"})
            return
        history[:] = new_history  # 原地更新（闭包外的引用同步）
        save_messages(WORKSPACE, session_id, new_history[old_len:])
        await ws.send_json({"type": "assistant_done", "text": reply})

    try:
        while True:  # 常驻收消息——审批答复也走这里（不能被 agent 执行阻塞）
            msg = await ws.receive_json()
            mtype = msg.get("type")
            if mtype == "user":
                if user_task and not user_task.done():
                    continue  # 上一轮还在跑——忽略
                stop_event.clear()  # 新一轮：把上一轮的「停」清掉
                user_task = asyncio.create_task(run_user(str(msg.get("text", ""))))
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
                    ev.set()
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
    except WebSocketDisconnect:
        pass
    finally:
        # 连接没了 = 没人能回答那条审批：放掉阻塞的线程（否则每断一次线泄漏一个）
        ev = approval.get("event")
        if ev is not None:
            approval["result"] = False
            ev.set()


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
    print("前端（网页界面）: http://localhost:5173（在 web/ 下 npm run dev）")
    uvicorn.run(app, host=s.server_host, port=s.server_port)


_CLI_MODEL: str | None = None  # --model 指定（None = 配置 default）


if __name__ == "__main__":
    main()
