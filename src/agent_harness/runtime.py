"""会话运行时：新建 / 切换 / 存档 / 回放 / 跑一轮——两个界面共用的那一份编排。

为什么单独一层：这些编排原来在 `cli.py` 和 `server.py` 各写一遍，已经分叉了——网页端
收到新指令时**静默丢掉**，而"开新会话"那条分支却会提示"这一轮还在跑"：同一个文件里
两个答案，差别只是写的时候没对齐。这里只留一个答案，UI 只负责把结果画出来。

边界（画错了，重构完会发现"切会话把模型也重置了"）：
- **会话级**：session_id / history / tracer / 这个会话该用哪个 profile（跟着会话落盘）
- **连接级**：llm 适配器、approver（审批记忆 + 自动批准开关）。由调用方持有、传进来，
  切会话不重建——它们记的是"你"的东西，不是某个会话的。

不做：不碰界面、不管 async。谁能跑、怎么显示、停止按钮长什么样，都留在调用方；
这里只回答"能不能"和"结果是什么"。

和 `agent_harness.session`（存档）的分工：那个管 JSONL 文件怎么读写，这个管"什么时候
读、什么时候写、写哪些"。UI 只跟这里打交道——**别在界面里直接 import session**，
`tests/test_runtime.py` 有一条架构测试盯着（两边各写一次的状态迁移迟早再分叉）。
"""

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .assembly import build_system_prompt
from .compact import estimate_tokens
from .llm import Msg
from .session import (
    SESSION_ROOT,
    list_sessions,
    list_trace_files,
    load_session,
    load_session_model,
    new_session_id,
    prune_empty_sessions,
    save_messages,
    save_session_model,
    trace_path,
)
from .trace import Tracer, clean_traces, load_trace, mark_viewed
from .turn import Session, TurnCallbacks, TurnResult

# 「开着会话时又来了个命令」的唯一说法。UI 原样显示，不要自己改写——
# 两处各写一句，很快会一处说"稍后重试"、另一处说"请等待"，而它们是同一条规则。
BUSY_MESSAGE = "这一轮还在跑——等它结束再说。"


class SessionBusy(RuntimeError):
    """会话正被一轮占着，命令被拒。消息就是 `BUSY_MESSAGE`。"""


@dataclass(frozen=True)
class SessionView:
    """会话此刻长什么样——UI 拿去渲染，不要从运行时里抠细节。"""

    id: str
    model: str  # 实际模型名（deepseek-v4-pro 这种）
    profile: str  # profile 名（flash 这种）——/model 面板与落账都用它
    history: list[Msg] = field(default_factory=list)
    notice: str = ""  # 这次切换要顺带说一句的话（比如原模型已不在配置里）

    @property
    def history_n(self) -> int:
        return len(self.history)


@dataclass(frozen=True)
class SessionChoice:
    """选择器里的一行。model 是"打开它会用谁"，不是"上次记过谁"。"""

    id: str
    mtime: float
    model: str
    preview: str


def choices(workspace: Path, default_profile: str = "") -> list[SessionChoice]:
    """列可展示的历史会话（跳过开了没聊的空占位）。终端分页、网页弹层共用。

    model 是「打开这个会话会用谁」：没记过模型的会话显示成「xxx（默认）」——它打开时
    确实会用这个，写一根横杠等于让人以为「不知道用什么」，而其实答案明确。
    """
    out = []
    for s in list_sessions(workspace):
        if not s["first_user_msg"]:
            continue
        saved = load_session_model(workspace, s["id"])
        out.append(SessionChoice(
            id=s["id"],
            mtime=s["mtime"],
            model=saved or (f"{default_profile}（默认）" if default_profile else "-"),
            preview=" ".join(str(s["first_user_msg"]).split()),  # 换行会把一行提示撑成三行
        ))
    return out


def prune(workspace: Path) -> int:
    """打扫「开了没聊」的空占位。两个入口都顺手调，所以只留一份。"""
    return prune_empty_sessions(workspace)


def clean_trace(older_than_days: float = 0) -> tuple[int, int]:
    """删掉本项目的轨迹文件（0 = 全删），返回 (个数, 字节)。

    只动轨迹：session 是模型的账，trace 是给人看的账——前者不能被"清空间"顺手删掉。
    """
    return clean_traces(SESSION_ROOT, older_than_days=older_than_days)


def trace_age_days(workspace: Path) -> float | None:
    """最老的轨迹文件有多久（天）；没有轨迹 = None。

    从没打开过 /trace 时拿它当基准，别对新项目唠叨"你的轨迹 0 天没看了"。
    """
    files = list_trace_files(workspace)
    if not files:
        return None
    try:
        return (time.time() - min(f.stat().st_mtime for f in files)) / 86400
    except OSError:
        return None


class SessionRuntime:
    """一个连接（终端进程 / 一条 WS）的会话编排：切会话、切模型、跑一轮、落盘。

    为什么值得一个对象：`_busy` 这把锁就是"一轮只跑一个"这条规则的**唯一**落点。
    规则写在界面里的话，网页端会静默丢弃、终端却提示——这已经发生过一次了。

    构造参数里的 llm / approver 是连接级的，本类只持有引用、不重建：切会话时
    审批记忆和自动批准开关必须留着，否则"本会话不再问"会莫名其妙地每隔一次失效。
    """

    def __init__(self, *, llm, settings, approver, workspace, session_id: str | None = None):
        self.llm = llm
        self.settings = settings
        self.approver = approver
        self.workspace = Path(workspace)
        self._lock = threading.Lock()
        # 上下文用量的分母要用「这一轮真的发出去的那份提示词」，所以缓存一份；
        # 每轮跑完用 TurnResult.system_prompt 更新（谁读提示词只能有一处）。
        self._system_prompt = build_system_prompt()

        if session_id is None:
            # 新会话：new_session_id 会原子占位建文件，「开了没聊」由 prune 收拾
            self.session_id = new_session_id(self.workspace)
            history: list[Msg] = []
        else:
            self.session_id = session_id
            history = load_session(self.workspace, session_id)

        # history 不复制：外面（以及 Session 内部）必须是同一个列表对象，否则「原地更新」看不见
        self.session = Session(
            llm=llm,
            settings=settings,
            approver=approver,
            workspace=self.workspace,
            tracer=self._new_tracer(),
            history=history,
        )

    # ---- 读 ----

    @property
    def history(self) -> list[Msg]:
        return self.session.history

    @property
    def tracer(self) -> Tracer:
        """当前会话的轨迹记录器（自动批准也记到这份账上）。"""
        return self.session.tracer

    @property
    def busy(self) -> bool:
        """有没有一轮正在跑。UI 想提前置灰按钮时用它——但**决策**在下面几个方法里。"""
        return self._lock.locked()

    def usage(self) -> tuple[int, int]:
        """顶栏/底栏的上下文占用：(已用, 上限)。两边算同一套，否则会出现"显示 60% 却已压缩"。"""
        return (
            estimate_tokens(self.session.history) + len(self._system_prompt),
            self.settings.compact_threshold,
        )

    def trace_records(self) -> list[dict]:
        """轨迹时间线（只读投影）。顺手记一笔「有人看过」——体温计不是决策。"""
        recs = load_trace(trace_path(self.workspace, self.session_id))
        mark_viewed()
        return recs

    def view(self, notice: str | None = "") -> SessionView:
        return SessionView(
            id=self.session_id,
            model=self.llm.current,
            profile=self.llm.current_profile,
            history=self.session.history,
            notice=notice or "",  # restore_model 返回 None = 没什么要说
        )

    # ---- 会话级命令（跑着的时候一律拒绝） ----

    def new_session(self) -> SessionView:
        """开一个新会话：只换 id / 历史 / 轨迹。连接、模型、审批记忆都留着。"""
        with self._claim():
            self.session.history.clear()  # 原地清：外面拿着同一个列表
            self.session_id = new_session_id(self.workspace)
            self.session.tracer = self._new_tracer()
            return self.view()

    def resume(self, session_id: str) -> SessionView:
        """恢复某个会话：历史、该用哪个模型、轨迹文件都跟着换。"""
        with self._claim():
            self.session.history[:] = load_session(self.workspace, session_id)
            self.session_id = session_id
            notice = self.restore_model()
            self.session.tracer = self._new_tracer()
            return self.view(notice)

    def switch_model(self, name: str) -> str:
        """切到某个 profile，并记在这个会话上（退出不丢）。返回实际模型名。

        名字未知抛 KeyError——调用方要能说清"没这个 profile"，而不是静默回退。
        跑着的时候拒绝：否则一轮里前半段用 A、后半段用 B，账还会记成 B。
        """
        with self._claim():
            model = self.llm.switch(name)
            save_session_model(self.workspace, self.session_id, name)
            return model

    def restore_model(self, explicit: str | None = None) -> str | None:
        """按会话记录恢复模型；返回一句该给人看的话（None = 没什么要说）。

        explicit 优先（终端的 --model）：它压过会话记忆，并落账到这个会话上——
        用户这次就是冲着这个模型来的。返回"原模型没了"这类提示，由 UI 决定怎么显示。
        """
        if explicit:
            if explicit != self.llm.current_profile:
                self.llm.switch(explicit)
            save_session_model(self.workspace, self.session_id, self.llm.current_profile)
            return None
        saved = load_session_model(self.workspace, self.session_id)
        if not saved:
            return None
        if saved not in self.settings.models:
            return f"会话原用模型 {saved} 已不在配置里，回退默认"
        if saved != self.llm.current_profile:
            self.llm.switch(saved)
        return None

    def record_auto(self, command: str, reason: str) -> None:
        """自动批准也留账。屏幕上那行会滚走，trace 才是事后能查的地方。"""
        self.tracer.emit("auto", command=command, reason=reason)

    # ---- 一轮 ----

    def run(
        self,
        goal: str,
        *,
        callbacks: TurnCallbacks | None = None,
        should_stop=None,
    ) -> TurnResult:
        """跑一轮，成功后把这一轮新增的消息落盘；失败原样抛出去（这一轮不记）。

        落盘放在这里而不是各界面：现在的写法是"跑完 → 手动切 history[old_len:] → 存"，
        两边都写过一遍，而且都依赖于"跑失败时 history 没被改写"这个约定。
        约定只该由懂它的人守——这就是这里。
        """
        with self._claim():
            old_len = len(self.session.history)
            result = self.session.run(goal, callbacks=callbacks, should_stop=should_stop)
            self._system_prompt = result.system_prompt
            # 只有跑到这里才落盘：中途抛异常时 history 未被替换，"这一轮不记"是刻意的
            save_messages(self.workspace, self.session_id, self.session.history[old_len:])
            return result

    # ---- 内部 ----

    def _new_tracer(self) -> Tracer:
        """关掉 [trace] enabled 就给空对象（所有方法照常可调、什么都不写）。"""
        if not self.settings.trace_enabled:
            return Tracer()
        return Tracer(trace_path(self.workspace, self.session_id))

    def _claim(self):
        """非阻塞占位：占不到就是"正有一轮在跑"，直接拒绝。

        做成上下文管理器而不是"先问再跑"：问和跑之间有空隙，两个入口各自问一次
        就会双双通过（网页端两条消息挨着到达时正好撞这个）。
        """
        return _Claim(self._lock)


class _Claim:
    """`SessionRuntime._claim()` 的上下文管理器——谁占到锁谁跑，占不到就抛。"""

    def __init__(self, lock: threading.Lock):
        self._lock = lock

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            raise SessionBusy(BUSY_MESSAGE)
        return self

    def __exit__(self, exc_type, exc, tb):
        self._lock.release()
        return False
