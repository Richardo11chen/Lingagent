"""Web 服务层（S6b）：FastAPI + WebSocket——把 harness 暴露成网页可交互的服务。

架构：每个 WS 连接 = 一个 agent 会话；agent_loop（同步阻塞）跑在旁路线程
（asyncio.to_thread），事件经 asyncio.run_coroutine_threadsafe 推回事件循环 → WS。
"""

import asyncio
import threading
from functools import partial

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .assembly import (
    WORKSPACE,
    build_system_prompt,
    list_visible_sessions,
    load_settings_and_llm,
    tools,
)
from .llm import Msg
from .loop import agent_loop
from .session import load_session, new_session_id, save_messages, trace_path
from .trace import Tracer, load_trace

app = FastAPI()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_running_loop()
    session_id = new_session_id(WORKSPACE)
    history: list[Msg] = []
    llm, settings = load_settings_and_llm(build_system_prompt(), _CLI_MODEL)
    approval: dict = {}  # 审批等待槽：{"event": Event, "result": bool}
    user_task: asyncio.Task | None = None

    await ws.send_json({"type": "session", "id": session_id, "history_n": len(history)})

    async def run_user(goal: str) -> None:
        """跑一轮 agent（旁路线程）——回调经事件循环推回 WS。"""

        def send(payload: dict) -> None:
            asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop)

        def on_text(chunk: str) -> None:
            send({"type": "assistant_chunk", "text": chunk})

        def on_tool(name: str, args: dict) -> None:
            detail = str(args.get("command") or args.get("path") or "")
            send({"type": "tool", "name": name, "detail": detail[:80]})

        def on_reasoning(chunk: str) -> None:
            send({"type": "reasoning", "text": chunk})

        def approver(command: str) -> bool:
            """线程内阻塞等前端答复——收消息 task 唤醒 Event。"""
            ev = threading.Event()
            approval["event"] = ev
            approval["result"] = False
            send({"type": "approval_request", "command": command})
            ev.wait()
            return approval["result"]

        old_len = len(history)
        tracer = Tracer(trace_path(WORKSPACE, session_id))  # 每轮现取——resume 换 id 后自动跟到新文件
        run = partial(
            agent_loop,
            goal,
            llm,
            tools,
            build_system_prompt(),
            approver=approver,
            history=history,
            on_text=on_text,
            on_tool=on_tool,
            on_reasoning=on_reasoning,
            tracer=tracer,
            max_steps=settings.max_steps,
        )
        reply, new_history = await asyncio.to_thread(run)
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
                user_task = asyncio.create_task(run_user(str(msg.get("text", ""))))
            elif mtype == "approval_response":
                ev = approval.get("event")
                if ev:
                    approval["result"] = bool(msg.get("approved"))
                    ev.set()
            elif mtype == "models_request":  # 列可选模型（/model）
                await ws.send_json(
                    {"type": "models", "list": llm.profiles, "current": llm.current_profile}
                )
            elif mtype == "switch_model":  # 运行时切换模型
                name = str(msg.get("name", ""))
                try:
                    await ws.send_json({"type": "model_switched", "model": llm.switch(name)})
                except KeyError:
                    await ws.send_json(
                        {"type": "model_switched", "model": "", "error": f"未知 profile：{name}"}
                    )
            elif mtype == "trace_request":  # S8：轨迹时间线（只读投影——决策 4）
                recs = load_trace(trace_path(WORKSPACE, session_id))
                await ws.send_json({"type": "trace", "list": recs[-1000:], "total": len(recs)})
            elif mtype == "sessions_request":  # 列历史会话（前端启动时问）
                items = [
                    {"id": s["id"], "preview": s["first_user_msg"]}
                    for s in list_visible_sessions()[:10]
                ]
                await ws.send_json({"type": "sessions", "list": items})
            elif mtype == "resume":  # 恢复某个会话
                sid = str(msg.get("id", ""))
                if sid and (user_task is None or user_task.done()):
                    history[:] = load_session(WORKSPACE, sid)
                    session_id = sid
                    await ws.send_json(
                        {"type": "session", "id": session_id, "history_n": len(history), "resumed": True}
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


def main() -> None:
    import argparse

    from .settings import load_settings

    ap = argparse.ArgumentParser(description="Agent Harness Web")
    ap.add_argument("--model", help="模型 profile 名（配置 [models.*]；缺省用 default）")
    args = ap.parse_args()

    s = load_settings()
    if args.model:
        if args.model not in s.models:  # fail fast：别等 WS 连上才在 handler 里炸（前端只看到连接即断）
            raise SystemExit(f"未知模型 profile：{args.model}（可用：{', '.join(s.models)}）")
        global _CLI_MODEL
        _CLI_MODEL = args.model  # ws_endpoint 构造 llm 时用
    print("前端（网页界面）: http://localhost:5173（在 web/ 下 npm run dev）")
    uvicorn.run(app, host=s.server_host, port=s.server_port)


_CLI_MODEL: str | None = None  # --model 指定（None = 配置 default）


if __name__ == "__main__":
    main()
