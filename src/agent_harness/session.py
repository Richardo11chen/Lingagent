"""会话持久化：JSONL 追加存档、列出历史会话、恢复载回。

设计定案（2026-09-22）：时间戳 id + 原子创建；JSONL append-only；投影式读回；
坏行（崩溃残留，只可能在末尾）跳过。
"""

import json
import time
from pathlib import Path

from .messages import Msg

# 存档根在用户目录（Claude Code 形态）——项目目录干净、工具（含 shell 猜路径）够不着
SESSION_ROOT = Path.home() / ".agent_harness_sessions"


def _project_key(workspace: Path) -> str:
    """项目路径 → 目录名：非字母数字一律转义成 '-'（Claude Code 形态）。

    已知边界：不同路径理论上可转义碰撞（如 D:\\a-b 与 D:\\a\\b）——学习项目接受，
    真撞再加 hash 后缀。
    """
    return "".join(c if c.isalnum() else "-" for c in str(workspace.resolve()))


def _session_dir(workspace: Path) -> Path:
    d = SESSION_ROOT / _project_key(workspace)
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_session_id(workspace: Path) -> str:
    """生成会话 id（时间戳带微秒）并原子占位创建——撞了自动加序号。"""
    base = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1_000_000) % 1_000_000:06d}"
    d = _session_dir(workspace)
    for suffix in [""] + [f"-{i}" for i in range(1, 100)]:
        sid = base + suffix
        try:
            with open(d / f"{sid}.jsonl", "x", encoding="utf-8"):
                return sid  # 占位成功——「检查+创建」由 OS 原子保证（O_CREAT|O_EXCL）
        except FileExistsError:
            continue
    raise RuntimeError("会话 id 生成失败：连续碰撞 100 次")


def session_path(workspace: Path, session_id: str) -> Path:
    """会话文件路径——唯一的拼接口，外部（含测试）别再手拼实现细节。"""
    return _session_dir(workspace) / f"{session_id}.jsonl"


def _msg_to_dict(m: Msg) -> dict:
    """Msg → JSON 原生 dict（tool_calls 的 pydantic 对象用 model_dump 转纯 dict）。"""
    d: dict = {"role": m.role, "content": m.content}
    if m.tool_name:
        d["tool_name"] = m.tool_name
    if m.tool_call_id:
        d["tool_call_id"] = m.tool_call_id
    if m.tool_calls:
        d["tool_calls"] = [
            tc.model_dump() if hasattr(tc, "model_dump") else tc for tc in m.tool_calls
        ]
    return d


def _dict_to_msg(d: dict) -> Msg:
    return Msg(
        role=d["role"],
        content=d.get("content", ""),
        tool_name=d.get("tool_name"),
        tool_call_id=d.get("tool_call_id"),
        tool_calls=d.get("tool_calls"),
    )


def save_messages(workspace: Path, session_id: str, msgs: list[Msg]) -> None:
    """追加本轮新增消息（一行一条，append-only）。"""
    path = session_path(workspace, session_id)
    with open(path, "a", encoding="utf-8") as f:
        for m in msgs:
            f.write(json.dumps(_msg_to_dict(m), ensure_ascii=False) + "\n")


def list_sessions(workspace: Path) -> list[dict]:
    """列历史会话（mtime 倒序）：[{id, mtime, first_user_msg}]。"""
    d = _session_dir(workspace)
    out: list[dict] = []
    for p in d.glob("*.jsonl"):
        first_user = ""
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        break
                    if rec.get("role") == "user":
                        first_user = str(rec.get("content", ""))[:60]
                        break
        except OSError:
            continue
        out.append({"id": p.stem, "mtime": p.stat().st_mtime, "first_user_msg": first_user})
    out.sort(key=lambda s: s["mtime"], reverse=True)
    return out


def load_session(workspace: Path, session_id: str) -> list[Msg]:
    """读回 Msg 列表——坏行（崩溃残留的半行）跳过，保住会话其余部分。"""
    path = session_path(workspace, session_id)
    msgs: list[Msg] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(_dict_to_msg(json.loads(line)))
            except json.JSONDecodeError:
                continue
    return msgs
