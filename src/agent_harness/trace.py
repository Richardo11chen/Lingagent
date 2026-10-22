"""轨迹记录层（S8·留痕）：一条动作一条事件，append-only 落盘——给「人」看的账。

与 session 的分工：session 给模型看（会被投影压缩）；trace 给人看（永不压缩、字段不丢）。

事件 kind 与字段：
- model    模型请求：step / duration_ms / n_actions
- tool     工具执行：step / tool / args / duration_ms / status(ok|denied|blocked|error) / result
- approval 人工审批：step / command / decision(approved|denied) / duration_ms（人犹豫了多久）
公共字段：ts（墙钟时间，给人看）/ kind。
"""

import json
import time
from datetime import datetime
from pathlib import Path

from .paths import APP_DIR

# 单条事件的读入上限：trace 存全文（审计账），但读的时候不必把 90MB 的一行
# 也塞进内存和界面。超限的记录只留一条摘要占位（原始字节仍在文件里，需要时直接看文件）。
MAX_RECORD_BYTES = 2 * 1024 * 1024

# 上一次「人看过轨迹」的时间戳——用来判断这个功能是不是已经没人用了
VIEW_STAMP = APP_DIR / "last_trace_view"

# 单字段显示上限：终端和网页共用同一个数、同一句话——两处不一样会让人以为数据不同
TRACE_FIELD_CHARS = 2000


def ellipsis_note(total: int, shown: int = TRACE_FIELD_CHARS) -> str:
    """截断说明的唯一来源：TUI 和网页都调它，保证同一个词。"""
    return f"…（共 {total} 字符，仅显示前 {shown}；全文见同名 .trace.jsonl）"


def load_trace(path: Path) -> list[dict]:
    """读回轨迹：坏行跳过（与 session.load_session 同规矩）；文件不存在 = 空列表。

    超大行（一次命令刷出几十 MB）不解析，换成摘要占位——否则 /trace 一次要吃进整份。
    """
    if not path.exists():
        return []
    recs: list[dict] = []
    for raw in path.open("rb"):
        if len(raw) > MAX_RECORD_BYTES:
            recs.append({
                "kind": "oversized",
                "bytes": len(raw),
                "note": f"单条事件 {len(raw)} 字节，超出读取上限，已省略（全文在 trace 文件里）",
            })
            continue
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return recs


class Tracer:
    """注入式记录器（S8 决策 2）：调用方只管 emit/timed——不知道写去哪、是否在写。

    path=None = 关闭态（空对象）：所有方法照常可调、什么都不做——调用方代码不变。
    """

    def __init__(self, path: Path | None = None):
        self._path = path

    def emit(self, kind: str, **fields) -> None:
        if self._path is None:
            return
        rec = {"ts": datetime.now().isoformat(timespec="milliseconds"), "kind": kind}
        rec.update(fields)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def timed(self, kind: str, **fields):
        """包住一段调用自动记耗时：with tracer.timed(kind, **前置字段) as ev: ... ev.fields[...] = 执行后字段"""
        return _Timed(self, kind, fields)


class _Timed:
    """上下文管理器：__enter__ 取起点；__exit__ 算耗时、落事件、再放行异常。"""

    def __init__(self, tracer: Tracer, kind: str, fields: dict):
        self._tracer = tracer
        self._kind = kind
        self.fields = dict(fields)  # 出块前还能补字段（结果/状态是执行后才知道的）

    def __enter__(self):
        self._t0 = time.perf_counter()  # 单调时钟：只算差值，不受校时跳变影响
        return self

    def __exit__(self, exc_type, exc, tb):
        self.fields["duration_ms"] = round((time.perf_counter() - self._t0) * 1000)
        if exc is not None:  # 崩了也留痕——审计账不能因为异常断档
            self.fields.setdefault("status", "error")
            self.fields["error"] = f"{type(exc).__name__}: {exc}"
        self._tracer.emit(self._kind, **self.fields)
        return False  # 不吞异常——照常往外抛


def mark_viewed() -> None:
    """记一笔「有人看过轨迹」。忽略失败——这只是一条体温计，不是账本。"""
    try:
        VIEW_STAMP.parent.mkdir(parents=True, exist_ok=True)
        VIEW_STAMP.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


def days_since_viewed() -> float | None:
    """多久没看过 /trace（天；从没看过 = None）。"""
    try:
        return (time.time() - int(VIEW_STAMP.read_text(encoding="utf-8").strip())) / 86400
    except (OSError, ValueError):
        return None


def clean_traces(root: Path, older_than_days: float = 0) -> tuple[int, int]:
    """删掉 root 下所有 `.trace.jsonl`（0 = 全删，否则只删够老的）。返回 (个数, 字节)。

    只动轨迹：session 是模型的账，trace 是给人看的账——前者不能碰。
    """
    cutoff = time.time() - older_than_days * 86400
    count = freed = 0
    for p in root.rglob("*.trace.jsonl"):
        try:
            st = p.stat()
            if older_than_days and st.st_mtime > cutoff:
                continue
            p.unlink()
            count += 1
            freed += st.st_size
        except OSError:
            continue
    return count, freed
