"""Module-level pipeline state: per-job locks + task registry (module refactor)."""
from __future__ import annotations

import asyncio

# Phase 7: per-job async lock — prevents cancel+retry race where two
# pipeline coroutines could run simultaneously on the same job_id.
# Keyed by job_id; entries are removed when pipeline exits.
_pipeline_locks: dict[str, asyncio.Lock] = {}

# 活跃 pipeline task 注册表 — 用于优雅关闭时取消所有运行中的任务。
#
# ⚠️ 值必须是**集合**而非单个 Task（对抗审查 #141，2026-09-18 修）：
# 同一 job_id 允许存在**多个**未完成 task —— 持锁的真凶 + 在 `async with lock`
# 上排队的等待者（retry 会再 launch 一个）。旧实现 `_pipeline_tasks[jid] = task`
# 是**单值覆盖**：新 task 一注册，真凶就失去引用，随后
# `_terminate_pipeline_task` 只能拿到"等待者"并把它取消 —— 真凶仍持 per-job 锁、
# 重试的新 task 继续排队、900s 后再被杀 ⇒ "重试 → 等 900s → 被杀 → 再重试"的
# 无限循环（看门狗自述"需人工介入"的那个状态）。
# 集合语义让"取消该 job 的全部未完成 task"成为可能，与 `find_stalled_jobs`
# 的 `has_live_task` 判据（只看是否非空，不看数量）兼容。
# task 完成后自动从集合中移除；集合空则删除键。
_pipeline_tasks: dict[str, set[asyncio.Task]] = {}
_locks_guard = asyncio.Lock()


def register_pipeline_task(job_id: str, task: asyncio.Task) -> None:
    """把 task 加入该 job 的注册集合（**累积**而非覆盖）。"""
    _pipeline_tasks.setdefault(job_id, set()).add(task)


def unregister_pipeline_task(job_id: str, task: asyncio.Task) -> bool:
    """从注册集合移除 task；集合空则删键。返回"是否确实移除过"。

    幂等：task 不在集合里（已被别处清理）返回 False，不抛。
    """
    bucket = _pipeline_tasks.get(job_id)
    if bucket is None:
        return False
    removed = task in bucket
    bucket.discard(task)
    if not bucket:
        _pipeline_tasks.pop(job_id, None)
    return removed


def live_tasks_for(job_id: str) -> list[asyncio.Task]:
    """该 job 全部**未完成**的 task（持锁者 + 等待者）。"""
    return [t for t in list(_pipeline_tasks.get(job_id, ())) if not t.done()]


# ── 派生子任务登记（#140）────────────────────────────────────────────
# `stage2` / 分片路径会 `create_task` 出 N 个页分析协程。它们**不是**顶层
# pipeline task（不进 `_pipeline_tasks`），此前完全游离：父 task 被取消时
# 它们继续跑 LLM、继续 `touch_activity`、继续写 `page_cache`/`findings` ——
# 既与 retry 后的新一轮**抢同一页**，又把心跳刷成"在动"从而**掩盖真正的停滞**。
#
# 用法（结构化并发的最小实现）：
#   with child_tasks() as children:
#       children.spawn(coro)          # 登记 + 取消时自动级联
#   # 退出 with 时自动等待/取消（见 ChildTasks.__exit__）
#
# `async with` 不可用时（同步建任务循环）用显式 API：
#   children = ChildTasks(job_id); children.spawn(...); await children.drain()
class ChildTasks:
    """一组派生子任务的登记与级联取消。"""

    __slots__ = ("_job_id", "_tasks", "_closed")

    def __init__(self, job_id: str) -> None:
        self._job_id = job_id
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    def spawn(self, coro) -> asyncio.Task:
        """create_task + 登记；父被取消时由 `cancel_all()` 级联。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        # 自清理：完成即摘掉，避免长任务集无限增长
        task.add_done_callback(self._tasks.discard)
        return task

    @property
    def tasks(self) -> list[asyncio.Task]:
        return list(self._tasks)

    def cancel_all(self) -> int:
        """取消全部未完成子任务，返回取消数（不等待）。"""
        n = 0
        for t in list(self._tasks):
            if not t.done():
                t.cancel()
                n += 1
        return n

    async def drain(self, *, cancel: bool = False, timeout: float | None = None) -> None:
        """等待子任务结束；`cancel=True` 先级联取消。

        用 `asyncio.wait` 而非 `gather`：后者会把子任务的异常（含
        CancelledError）抛进调用方，而调用方通常在 `finally` 里做状态收敛，
        绝不能被单个子任务的异常打断 —— 收敛失败比子任务泄漏更糟。
        """
        if cancel:
            self.cancel_all()
        pending = set(self._tasks)
        if not pending:
            return
        await asyncio.wait(pending, timeout=timeout)
        self._tasks.clear()

    def __enter__(self) -> "ChildTasks":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # 不管正常退出还是异常/取消，都不允许子任务游离在作用域之外。
        # 同步上下文中不能 await，故只发取消信号；真正的收尾由
        # `drain(cancel=True)` 或父 task 的 finally 完成。
        self._closed = True
        self.cancel_all()
        return False


# Module-level lock serializing all DB writes on the shared aiosqlite
# connection (single connection does NOT support concurrent execute).
# Used by _is_cancelled and _analyze_one to prevent "Recursive use of
# cursors" errors.
db_lock = asyncio.Lock()

# 分片 OCR 队列轮询间隔（run_ocr_sliced 线程回调 → asyncio.Queue 的
# 等待超时）。生产 2s 足够灵敏；测试可 monkeypatch 加速。
_SLICE_QUEUE_TIMEOUT = 2.0
