"""Web 服务层（S6b）：FastAPI + WebSocket——把 harness 暴露成网页可交互的服务。

架构：每个 WS 连接 = 一个 agent 会话；agent_loop（同步阻塞）跑在旁路线程
（asyncio.to_thread），事件经 asyncio.run_coroutine_threadsafe 推回事件循环 → WS。
"""

import asyncio
import json
import os
import secrets

import uvicorn
from fastapi import WebSocket, WebSocketDisconnect

from . import webapp
from .approval import SessionAllow
from .assembly import WORKSPACE, load_settings_and_llm
from .prompt import build_system_prompt
from .runtime import (
    SessionRuntime,
    choices,
    prune,
)
from .settings import load_settings
from .tools import task_snapshot, task_stop, tool_detail
from .trace import TRACE_FIELD_CHARS, ellipsis_note
from .webapp import (  # noqa: F401  (对外沿用 server.app / server.origin_allowed 等)
    PROTOCOL,
    SESSION_TOKEN,
    _turn_ended,
    _turn_started,
    app,
    health,
    origin_allowed,
)  # noqa: F401  (对外沿用 server.app / server.health / server.PROTOCOL)

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


def _render_history(history: list) -> list[dict]:
    """把恢复出来的历史渲染成前端画得出来的形状（user/assistant 正文；tool 给名字 + 参数 + 输出）。

    工具参数不落库——它在 assistant 的 `tool_calls` 里，按 `tool_call_id` 拼回来，
    否则刷新后工具行只剩名字、点开没参数（2026-10-24 用户报的）。

    这是**渲染**，所以留在这一层：runtime 只把历史原样交出来，终端那边画成字符、
    这边画成 JSON，一件事两种画法。
    """
    args_by_call: dict = {}
    for m in history:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                try:
                    args_by_call[tc.get("id")] = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args_by_call[tc.get("id")] = {}
    rendered = []
    for m in history:
        if m.role == "user":
            rendered.append({"kind": "user", "text": m.content})
        elif m.role == "assistant" and (m.content or "").strip():
            rendered.append({"kind": "assistant", "text": m.content})
        elif m.role == "tool":
            body = m.content or ""
            if len(body) > TOOL_RESULT_CHARS:
                body = body[:TOOL_RESULT_CHARS] + ellipsis_note(len(body))
            rendered.append({
                "kind": "tool",
                "name": m.tool_name or "工具结果",
                "detail": tool_detail(args_by_call.get(m.tool_call_id)),
                "body": body,
            })
    return rendered


def list_tasks() -> list[dict]:
    """后台任务快照（底部抽屉用）。任务是进程级全局的，所以不按会话过滤。"""
    from . import tasks as task_reg

    snaps = (task_snapshot(t.id) for t in task_reg.all_tasks())
    return sorted((s for s in snaps if s), key=lambda s: s["started"])


def token_ok(token: str | None) -> bool:
    """定长比较，避免把 token 变成计时侧信道。"""
    return bool(token) and secrets.compare_digest(str(token), SESSION_TOKEN)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # 先验来源和 token 再握手：没通过连协议都不建立。
    # 顺序上 token 在前——它才是那条"局域网 curl 也进不来"的门闩。
    if not token_ok(ws.query_params.get("token")):
        await ws.close(code=1008)
        return
    # settings 这里单独读一次（很便宜），因为完整的 load_settings_and_llm 要建客户端，
    # 而它得在 accept 之后才能推提示。
    cfg = load_settings()
    if not origin_allowed(ws.headers.get("origin"), tuple(cfg.extra_origins)):
        await ws.close(code=1008)  # policy violation：别 accept 再关，直接不建立
        return
    await ws.accept()
    loop = asyncio.get_running_loop()

    def send(payload: dict) -> None:
        """线程 → 事件循环 → WS（run_user 与模型适配器共用）。"""
        asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop)

    llm, settings = load_settings_and_llm(
        build_system_prompt(WORKSPACE.root), _CLI_MODEL,
        on_notice=lambda text: send({"type": "notice", "text": text}),
    )

    def _ask(command: str, reason: str) -> str:
        """网页怎么问：推卡片，等答复。返回 decision（approve / always / deny）。

        reason 一起推给前端：只给命令的话，批的人不知道自己批的是什么风险。
        **谁在等**归 runtime（`begin_approval`），记忆和自动批准归 `SessionAllow`——
        这里只负责"怎么问"，三件事各有各的家。
        """
        slot = runtime.begin_approval()
        send({"type": "approval_request", "command": command, "reason": reason})
        decision, timed_out = slot.wait(settings.approval_timeout or None)
        if timed_out:  # 没人回答：按驳回处理，别让这一轮永远挂着
            send({"type": "notice", "text": f"审批超时（{settings.approval_timeout:g} 秒无人答复），已按驳回处理。"})
        return decision

    def _on_auto(command: str, reason: str) -> None:
        send({"type": "notice", "text": f"自动批准 · {reason}"})
        runtime.record_auto(command, reason)  # 记到「这一轮」的账上（runtime 里那份随会话走）

    # 建在连接级（不是每轮）：它记着「本会话不再问哪些程序」和自动批准开关，
    # 每轮重建等于每轮失忆——那正好是这份记忆存在的意义。
    approver = SessionAllow(
        _ask,
        on_granted=lambda head: send({"type": "notice", "text": f"本会话不再询问 {head}"}),
        on_reused=lambda head: send({"type": "notice", "text": f"本会话已允许 {head}，直接执行"}),
        on_auto=_on_auto,
    )
    # 会话级装配（id / 历史 / 轨迹 / 该用哪个模型）全在 runtime 里：连接级的东西
    # （llm、approver）传进去，切会话不重建——它们是"你"的，不是会话的。
    runtime = SessionRuntime(
        llm=llm, settings=settings, approver=approver,
        workspace=WORKSPACE.root, session_id=None,  # 新连接 = 新会话
    )

    def status_payload() -> dict:
        """顶栏要的那点状态：审批模式 + 上下文用量。

        用量算法与 TUI 完全一致（同一个 runtime）：两边各算一套的话，迟早出现
        "显示 60% 但已经开始压缩"。
        """
        used, limit = runtime.usage()
        return {"type": "status", "auto": approver.auto, "used": used, "limit": limit}

    await ws.send_json(
        {"type": "session", "id": runtime.session_id, "history_n": 0, "model": llm.current}
    )
    await ws.send_json(status_payload())

    def render(ev: dict) -> None:
        """事件 → WS 报文。**只做形状转换，不做判断**——这是"UI 只渲染"的落点。

        策略性的事（要不要拒、能不能跑、下一轮什么时候开始）已经在 runtime 里定了；
        这里连"该不该做"都不该决定，只决定"这条数据画成什么样"。
        """
        kind = ev["type"]
        if kind == "turn_started":  # /health 的 busy：TUI 退出前靠它判断"还有一轮在跑"
            _turn_started()
        elif kind == "turn_ended":
            _turn_ended()
        elif kind == "session":
            payload = {
                "type": "session",
                "id": ev["id"],
                "history_n": ev["history_n"],
                "model": ev["model"],
            }
            if ev["resumed"]:
                payload["resumed"] = True
            send(payload)
            if ev["notice"]:  # 比如"原用模型已不在配置里"——别静默回退
                send({"type": "notice", "text": ev["notice"]})
            # 换会话了就把历史推给前端重画（新会话是空表，等于让前端清屏）
            send({"type": "history", "messages": _render_history(ev["history"])})
        elif kind == "status":
            send({
                "type": "status", "auto": ev["auto"],
                "used": ev["used"], "limit": ev["limit"],
            })
        elif kind == "tool":
            send({"type": "tool", "name": ev["name"], "detail": ev["detail"][:50]})  # 与 TUI 同一截断
        elif kind == "tool_result":
            text = ev["result"]
            if len(text) > TOOL_RESULT_CHARS:
                text = text[:TOOL_RESULT_CHARS] + ellipsis_note(len(text))
            diff = ev["diff"]
            if len(diff) > TOOL_DIFF_CHARS:
                diff = diff[:TOOL_DIFF_CHARS] + f"\n…（diff 共 {len(diff)} 字符，已截断）"
            send({
                "type": "tool_result",
                "name": ev["name"],
                "status": ev["status"],
                "ms": ev["ms"],
                "result": text,
                "diff": diff,
                "added": ev["added"],
                "removed": ev["removed"],
            })
        elif kind == "child_tool":
            # 子 agent 的工具活动用一行 notice 带出来：前端不用改也能看见"它在干活"。
            # 正文增量（child_text）和子工具结果故意不转发——碎片太多，会把主流刷满。
            detail = ev["detail"][:40]
            send({"type": "notice", "text": f"⤷ {ev['name']}{'：' + detail if detail else ''}"})
        elif kind in ("child_text", "child_tool_result"):
            return
        elif kind == "rejected":  # 跑着的时候又来了命令：说出口，别让用户以为丢了
            send({"type": "notice", "text": ev["text"]})
        else:  # assistant_chunk / reasoning / hook / notice / error / assistant_done / model_switched
            send(ev)

    runtime.on_event(render)

    try:
        while True:  # 常驻收消息——审批答复也走这里（不能被 agent 执行阻塞）
            msg = await ws.receive_json()
            mtype = msg.get("type")
            # 下面五条是"查数据"，不动会话状态，直接答
            if mtype == "models_request":  # 列可选模型（/model 面板）
                await ws.send_json(
                    {"type": "models", "list": llm.profiles, "current": llm.current_profile}
                )
            elif mtype == "trace_request":  # S8：轨迹时间线（只读投影——决策 4）
                recs = runtime.trace_records()
                await ws.send_json(
                    {"type": "trace", "list": [_slim(r) for r in recs[-1000:]], "total": len(recs)}
                )
            elif mtype == "sessions_request":  # 列历史会话（前端启动时问）
                prune(WORKSPACE.root)  # 顺手打扫（浏览器每刷新一次就留一个空占位）
                items = [
                    {"id": c.id, "preview": c.preview}
                    for c in choices(WORKSPACE.root)  # 全量下发——前端弹层可滚动
                ]
                await ws.send_json({"type": "sessions", "list": items})
            elif mtype == "tasks_request":  # 后台任务列表（底部抽屉用）——只读快照
                await ws.send_json({"type": "tasks", "list": list_tasks()})
            elif mtype == "task_stop_request":  # 人在界面上按「停止」（进程级，不属于会话）
                tid = str(msg.get("id", ""))
                await asyncio.to_thread(task_stop, tid)
                await ws.send_json({"type": "tasks", "list": list_tasks()})
            else:
                # 会改状态 / 会跑一轮的命令：**只有一个入口**。判定、措辞、拒绝都在 runtime，
                # 这里连 try/except 都不需要——出错会以 error 事件回来。
                runtime.submit(msg)
    except WebSocketDisconnect:
        pass
    finally:
        # 连接没了 = 没人能回答那条审批：放掉阻塞的线程（否则每断一次线泄漏一个）
        runtime.deny_pending_approval()


# 打包形态：前端构建产物跟着包走（src/agent_harness/web_dist），后端一个端口全包
# ——装完不用 Node、不用 vite，直接开 http://127.0.0.1:8000。
# （静态托管本身在 webapp.py，这里只用来决定启动时打印哪个地址）


def run_server(model: str | None = None) -> None:
    """起后端 + 网页界面（`lingan -web` 走这里）。model=None → 用配置默认 profile。"""
    from .settings import load_settings

    s = load_settings()
    name = model or s.default_model
    if model and model not in s.models:  # fail fast：别等 WS 连上才在 handler 里炸（前端只看到连接即断）
        raise SystemExit(f"未知模型 profile：{model}（可用：{', '.join(s.models)}）")
    if model:
        global _CLI_MODEL
        _CLI_MODEL = model  # ws_endpoint 构造 llm 时用
    # 同样在这里把密钥也验掉：否则要等浏览器连上那一刻才 SystemExit，前端只看到「连接闪断」
    p = s.model(name)
    if not p.api_key and not os.environ.get(p.api_key_env):
        raise SystemExit(
            f"缺少 API key：profile {name} 没写 api_key，环境变量 {p.api_key_env} 也没设置"
        )
    where = (
        f"http://{s.server_host}:{s.server_port}"
        if (webapp.WEB_DIST / "index.html").exists()
        else "http://localhost:5173（开发模式：在 web/ 下 npm run dev）"
    )
    print(f"网页界面: {where}")
    uvicorn.run(app, host=s.server_host, port=s.server_port)


def main() -> None:
    """独立入口（测试 / `python -m` 用）：自己解析 --model，其余交给 run_server。"""
    import argparse

    ap = argparse.ArgumentParser(description="Lingagent Web")
    ap.add_argument("--model", help="模型 profile 名（配置 [models.*]；缺省用 default）")
    args = ap.parse_args()
    run_server(args.model)


_CLI_MODEL: str | None = None  # --model 指定（None = 配置 default）


if __name__ == "__main__":
    main()
