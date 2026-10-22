"""会话持久化：JSONL 追加存档、列出历史会话、恢复载回。

设计定案（2026-09-22）：时间戳 id + 原子创建；JSONL append-only；投影式读回；
坏行（崩溃残留，只可能在末尾）跳过。

布局（2026-10-22 改）：`<SESSION_ROOT>/<项目名>-<身份前8位>/`。
项目身份 = 工作目录里的 `.agent-harness-id`（一个 uuid 文件，跟着文件夹走）——
所以项目改名、移动目录，历史都还认得。旧布局（纯路径派生名）首次访问时自动搬过来。
"""

import json
import os
import shutil
import time
import uuid
from pathlib import Path

from .messages import Msg
from .paths import APP_DIR

# 存档根在应用目录下（~/.agent-harness/sessions）——项目目录干净、工具够不着
SESSION_ROOT = APP_DIR / "sessions"
LEGACY_SESSION_ROOT = Path.home() / ".agent_harness_sessions"  # 老位置（迁移用）


def _migrate_legacy_root() -> None:
    """老存档根 → 新目录（只搬默认根；测试/自定义根一律不动，免得误搬用户数据）。"""
    if SESSION_ROOT != APP_DIR / "sessions":
        return
    if SESSION_ROOT.is_dir() and any(SESSION_ROOT.iterdir()):
        return  # 已经有内容了，别覆盖
    if not LEGACY_SESSION_ROOT.is_dir():
        return
    try:
        SESSION_ROOT.parent.mkdir(parents=True, exist_ok=True)
        if SESSION_ROOT.is_dir():
            SESSION_ROOT.rmdir()  # 只有空的才删得掉
        LEGACY_SESSION_ROOT.rename(SESSION_ROOT)
    except OSError:
        pass

# 项目身份文件：放在工作目录里，一行 uuid。删掉它 = 换一个新身份（旧历史不会丢，但不再列出）
PROJECT_ID_FILE = ".agent-harness-id"


def _project_key(workspace: Path) -> str:
    """老规则：路径 → 目录名（非字母数字一律转义成 '-'）。

    现在只用于两处：迁移旧目录、以及工作目录不可写（身份文件建不出来）时的兜底。
    """
    return "".join(c if c.isalnum() else "-" for c in str(workspace.resolve()))


def _project_id(workspace: Path) -> str:
    """读项目身份；没有就建一个。

    为什么不是路径哈希：路径会变（改名、移动），身份不能跟着变。一个跟着文件夹走的
    小文件是唯一既省事又稳的做法——`git clone` 出来的新副本没有它，自然就是新身份。
    """
    p = workspace / PROJECT_ID_FILE
    if p.exists():
        try:
            got = p.read_text(encoding="utf-8").strip()
        except OSError:
            return _project_key(workspace)
        if got:
            return got
        # 存在但空的（手动 touch 过 / 写一半崩了）——补写一个
        got = uuid.uuid4().hex
        try:
            p.write_text(got + "\n", encoding="utf-8")
        except OSError:
            return _project_key(workspace)
        return got

    got = uuid.uuid4().hex
    try:
        with open(p, "x", encoding="utf-8") as f:  # 独占创建：两个进程同时跑也只有一个赢
            f.write(got + "\n")
        return got
    except FileExistsError:  # 别的进程刚建好——用它的，别覆盖
        try:
            return p.read_text(encoding="utf-8").strip() or _project_key(workspace)
        except OSError:
            return _project_key(workspace)
    except OSError:  # 目录不可写（只读挂载等）——退回路径派生，功能不残
        return _project_key(workspace)


def _label(workspace: Path) -> str:
    """目录名里的可读部分（项目目录名，转义掉不安全字符）——纯装饰，身份看 id 后缀。"""
    label = "".join(
        c if (c.isalnum() or c in "-_") else "-" for c in workspace.resolve().name
    )
    return label or "workspace"


def _find_by_id(project_id: str) -> Path | None:
    """按身份找回已存在的目录：项目改过名时 label 变了，id 后缀不会变。"""
    if not SESSION_ROOT.is_dir():
        return None
    matches = [p for p in SESSION_ROOT.glob(f"*-{project_id[:8]}") if p.is_dir()]
    return matches[0] if len(matches) == 1 else None


def _migrate_legacy(workspace: Path, new_dir: Path) -> None:
    """老目录（纯路径派生名）→ 新目录。空目录删掉，有内容整体搬；搬不动就不硬来。"""
    old = SESSION_ROOT / _project_key(workspace)
    if old == new_dir or not old.is_dir():
        return
    try:
        if any(old.iterdir()):
            shutil.move(str(old), str(new_dir))
        else:
            old.rmdir()
    except OSError:  # 被占用 / 权限不够——本次用新目录，旧数据原样留着
        pass


def _session_dir(workspace: Path) -> Path:
    _migrate_legacy_root()
    project_id = _project_id(workspace)
    d = SESSION_ROOT / f"{_label(workspace)}-{project_id[:8]}"
    if not d.exists():
        prev = _find_by_id(project_id)
        if prev is not None:  # 项目改过名/搬过家：目录改个名就行，数据原地不动
            try:
                prev.rename(d)
            except OSError:
                d = prev  # 改不动就用老目录——宁可名字难看，也不丢历史
        else:  # 换布局后第一次访问：把老的路径派生目录搬过来
            _migrate_legacy(workspace, d)
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


def trace_path(workspace: Path, session_id: str) -> Path:
    """轨迹文件路径（S8·留痕）：与 session 同目录同 id——归档布局仍只在这里拼。"""
    return _session_dir(workspace) / f"{session_id}.trace.jsonl"


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
    if m.reasoning_content:
        d["reasoning_content"] = m.reasoning_content
    return d


def _dict_to_msg(d: dict) -> Msg:
    return Msg(
        role=d["role"],
        content=d.get("content", ""),
        tool_name=d.get("tool_name"),
        tool_call_id=d.get("tool_call_id"),
        tool_calls=d.get("tool_calls"),
        reasoning_content=d.get("reasoning_content"),
    )


def save_messages(workspace: Path, session_id: str, msgs: list[Msg]) -> None:
    """追加本轮新增消息（一行一条，append-only）。"""
    path = session_path(workspace, session_id)
    with open(path, "a", encoding="utf-8") as f:
        for m in msgs:
            f.write(json.dumps(_msg_to_dict(m), ensure_ascii=False) + "\n")


def list_trace_files(workspace: Path) -> list[Path]:
    """本项目所有轨迹文件（清理与体检用）。"""
    return sorted(_session_dir(workspace).glob("*.trace.jsonl"))


def list_sessions(workspace: Path) -> list[dict]:
    """列历史会话（mtime 倒序）：[{id, mtime, first_user_msg}]。"""
    d = _session_dir(workspace)
    out: list[dict] = []
    for p in d.glob("*.jsonl"):
        if p.name.endswith(".trace.jsonl"):
            continue  # 轨迹文件不是会话——它没有 role 字段，混进列表只会变成空预览
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


def _repair_tool_pairs(msgs: list[Msg]) -> list[Msg]:
    """补上「assistant 发了 tool_calls 但没有对应 tool 回应」的洞。

    为什么非补不可：协议要求 N 个调用 ↔ N 条回应。断点/写坏一行之后历史里留一个
    悬空调用，之后每次请求都是 400——整个会话再也打不开，只能手改 JSONL。
    """
    answered = {m.tool_call_id for m in msgs if m.role == "tool" and m.tool_call_id}
    out: list[Msg] = []
    for m in msgs:
        out.append(m)
        if m.role != "assistant" or not m.tool_calls:
            continue
        for tc in m.tool_calls:
            tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            name = ""
            fn = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
            if isinstance(fn, dict):
                name = fn.get("name", "")
            elif fn is not None:
                name = getattr(fn, "name", "")
            if tc_id and tc_id not in answered:
                out.append(Msg(
                    "tool", "（上次会话在这里中断，这次调用没有结果）",
                    tool_name=name or None, tool_call_id=tc_id,
                ))
                answered.add(tc_id)
    return out


def load_session(workspace: Path, session_id: str) -> list[Msg]:
    """读回 Msg 列表——坏行跳过；meta 行（会话属性，如模型选择）不进消息。"""
    path = session_path(workspace, session_id)
    msgs: list[Msg] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("kind") == "meta":
                continue
            msgs.append(_dict_to_msg(rec))
    return _repair_tool_pairs(msgs)


def save_session_model(workspace: Path, session_id: str, profile: str) -> None:
    """把「这个会话在用的模型」写进存档（meta 行，append-only——后写的为准）。"""
    path = session_path(workspace, session_id)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "meta", "model": profile}, ensure_ascii=False) + "\n")


def prune_empty_sessions(workspace: Path, older_than_sec: float = 3600) -> int:
    """打扫：删掉「空且老」的占位会话（0 字节 = 开了没聊）。

    阈值默认 1 小时——避开刚开始、可能马上要写的会话；正在跑的进程即使被删了
    也无碍（下次写时 `open("a")` 会把文件重建）。返回删了几个。
    """
    d = _session_dir(workspace)
    now = time.time()
    removed = 0
    for p in d.glob("*.jsonl"):
        try:
            st = p.stat()
            if st.st_size == 0 and (now - st.st_mtime) > older_than_sec:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def load_session_model(workspace: Path, session_id: str) -> str | None:
    """读回会话的模型 profile 名（没切过/文件不存在 = None → 用默认）。"""
    path = session_path(workspace, session_id)
    if not path.exists():
        return None
    model = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("kind") == "meta":
            model = rec.get("model")
    return model
