# -*- coding: utf-8 -*-
"""RPA 执行进度事件中枢 — 把 worker 线程里的执行日志实时桥接给事件循环侧前端。

当前状态（休眠）：
  RPA 批量任务已改为始终由独立 RPA MCP 进程执行（本机 stdio / 跨机 HTTP，
  agent 进程内不跑 RPA，见 agent.mcp_setup），本模块不再被热路径引用——
  chat.py 的订阅/转发、adapters 的 capture_stdout_to_hub 均已撤下。实时日志
  待 Phase 2（跨进程日志转发：RPA server 实时推日志给 agent → 本 hub → 前端）
  时复用本 hub 与前端 rpa_log 事件处理。scripts/test_rpa_log_stream.py 保留
  验证本 hub 的机制契约。

背景（原进程内方案的机制说明，供 Phase 2 参考）
----
RPA 批量任务（轨迹跟踪表等）是同步 @tool，一次执行耗时数分钟。LangChain 经
``langchain_core.runnables.config.run_in_executor`` 用 ``copy_context().run(wrapper)``
把同步 ``_run`` 放到 executor 线程执行——该线程因此继承了调用方（agent 图所在的
asyncio 任务）的上下文，能读到本模块的 ``CURRENT_THREAD_ID`` contextvar。

流程（原方案）
----
1. 聊天 WS handler 在 ``agent.astream`` 前设置 CURRENT_THREAD_ID 并 ``hub.subscribe``；
2. RPA 任务内的 ``logging`` 输出经 ``WSProgressHandler`` 命中当前 thread_id，
   ``print`` 输出经 adapters 里的 ``capture_stdout_to_hub`` 逐行捕获；
   两者都写入线程安全的 ``queue.Queue``；
3. 事件循环侧订阅者任务被 waker future 唤醒后 ``drain``，把日志作为
   ``{"type": "rpa_log", ...}`` 推给前端。

线程安全：worker 线程只写 ``queue.Queue``（thread-safe）并经
``loop.call_soon_threadsafe`` 唤醒事件循环，从不触碰 asyncio.Queue / 协程状态。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import io
import logging
import queue as _queue
import threading
import time
from typing import Any, Callable

# 当前会话 thread_id —— 在聊天流处理任务里设置，经 copy_context 传播到工具执行线程。
CURRENT_THREAD_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "rpa_current_thread_id", default=""
)

# 单线程最多缓冲日志条数，防止长时间任务撑爆内存。
_MAX_BUFFER = 2000


class _Subscription:
    """单个 thread_id 的订阅：线程安全队列 + 事件循环侧 waker future。"""

    __slots__ = ("q", "loop", "waker")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.q: _queue.Queue[dict[str, Any]] = _queue.Queue(maxsize=_MAX_BUFFER)
        self.loop = loop
        self.waker: asyncio.Future | None = None


class ProgressHub:
    """thread_id → 订阅；``emit`` 任意线程可调用，``drain`` 在事件循环侧消费。"""

    def __init__(self) -> None:
        self._subs: dict[str, _Subscription] = {}
        self._lock = threading.Lock()

    def subscribe(self, thread_id: str) -> None:
        """事件循环侧注册订阅（幂等）。"""
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subs.setdefault(thread_id, _Subscription(loop))

    def unsubscribe(self, thread_id: str) -> None:
        """移除订阅并唤醒仍在 drain 的订阅者，让其结束。"""
        with self._lock:
            sub = self._subs.pop(thread_id, None)
        if sub is not None:
            sub.loop.call_soon_threadsafe(self._set_waker, sub)

    def emit(self, thread_id: str, event: dict[str, Any]) -> None:
        """任意线程可调用：写入队列并（若有等待者）唤醒事件循环侧的订阅者。"""
        if not thread_id:
            return
        with self._lock:
            sub = self._subs.get(thread_id)
        if sub is None:
            return
        try:
            sub.q.put_nowait(event)
        except _queue.Full:
            return
        sub.loop.call_soon_threadsafe(self._set_waker, sub)

    def _set_waker(self, sub: _Subscription) -> None:
        """事件循环线程：唤醒该订阅的等待者（若存在）。"""
        w = sub.waker
        if w is not None and not w.done():
            w.set_result(None)

    async def drain(self, thread_id: str):
        """事件循环侧异步生成器：逐条产出该线程的日志事件。

        队列有事件立即返回；队列空时注册 waker 挂起，等 emit 唤醒。
        订阅被 unsubscribe（turn 结束）后立即结束。
        """
        while True:
            with self._lock:
                sub = self._subs.get(thread_id)
            if sub is None:
                return
            try:
                yield sub.q.get_nowait()
                continue
            except _queue.Empty:
                pass

            # 队列空：注册 waker；注册后重查一次队列，封住 emit 写入竞态。
            fut = asyncio.get_running_loop().create_future()
            with self._lock:
                cur = self._subs.get(thread_id)
                if cur is None or cur is not sub:
                    return
                sub.waker = fut
            with self._lock:
                cur = self._subs.get(thread_id)
            if cur is not None and not cur.q.empty():
                with self._lock:
                    if cur.waker is fut:
                        cur.waker = None
                continue
            try:
                await fut
            finally:
                with self._lock:
                    if self._subs.get(thread_id) is sub and sub.waker is fut:
                        sub.waker = None


# 全局单例
hub = ProgressHub()


class WSProgressHandler(logging.Handler):
    """把 RPA 任务的 logger 输出按当前 thread_id 转发到 ProgressHub。"""

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level)
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        tid = CURRENT_THREAD_ID.get()
        if not tid:
            return
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        if not message:
            return
        hub.emit(tid, {
            "content": message,
            "level": record.levelname.lower(),
            "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
        })


def install_rpa_log_handler() -> None:
    """把 WSProgressHandler 挂到 skills.rpa logger（幂等）。

    RPA 任务的 logger（skills.rpa.tasks.*.flow）经父级传播到此 handler。
    仅 agent 进程调用；独立 RPA MCP 子进程不 import 本模块，无影响。
    显式把 skills.rpa 提到 INFO：否则根 logger（默认 WARNING）会把任务里的
    logger.info 里程碑日志在到达 handler 前就过滤掉。
    """
    rpa_logger = logging.getLogger("skills.rpa")
    rpa_logger.setLevel(logging.INFO)
    for h in rpa_logger.handlers:
        if isinstance(h, WSProgressHandler):
            return
    rpa_logger.addHandler(WSProgressHandler())


class _LineWriter(io.TextIOBase):
    """把写入的字节流按行拆开、逐行实时回调（供 print 捕获）。"""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, on_line: Callable[[str], None]) -> None:
        self._on_line = on_line
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r").rstrip()
            if line:
                self._on_line(line)
        return len(s)

    def flush(self) -> None:
        if self._buf.strip():
            self._on_line(self._buf.rstrip())
        self._buf = ""

    def isatty(self) -> bool:  # 避免部分库因非 tty 而改变行为
        return False


# redirect_stdout 换的是进程级全局 sys.stdout，并发 RPA 批量任务（多用户同时触发）
# 会互相抢占。用全局锁串行化，与 mcp_server._STDOUT_REDIRECT_LOCK 同一模式。
# 锁只约束 executor 工作线程，不碰事件循环；紫鸟单浏览器模型下任务本就该串行。
_STDOUT_REDIRECT_LOCK = threading.Lock()


def capture_stdout_to_hub(fn: Callable[..., Any], payload: dict[str, Any]) -> Any:
    """在工具执行线程里执行 fn，并把其 print 输出逐行转发到当前 thread_id 的 hub。

    无 thread_id（独立 MCP 子进程 / 脚本调用）时退化为直接调用、不碰 stdout，
    由调用方（mcp_server 的 redirect_stdout）自行处理。
    返回 fn 的原始结果。
    """
    tid = CURRENT_THREAD_ID.get()
    if not tid:
        return fn(payload)

    def _emit_line(line: str) -> None:
        hub.emit(tid, {
            "content": line,
            "level": "info",
            "time": time.strftime("%H:%M:%S"),
        })

    writer = _LineWriter(_emit_line)
    with _STDOUT_REDIRECT_LOCK:
        with contextlib.redirect_stdout(writer):
            result = fn(payload)
        writer.flush()  # 无换行结尾的残余输出
    return result
