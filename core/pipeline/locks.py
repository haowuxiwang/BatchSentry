"""Module-level pipeline state: per-job locks + task registry (module refactor)."""
from __future__ import annotations

import asyncio

# Phase 7: per-job async lock — prevents cancel+retry race where two
# pipeline coroutines could run simultaneously on the same job_id.
# Keyed by job_id; entries are removed when pipeline exits.
_pipeline_locks: dict[str, asyncio.Lock] = {}

# 活跃 pipeline task 注册表 — 用于优雅关闭时取消所有运行中的任务
# key=job_id, value=asyncio.Task。task 完成后自动从注册表移除。
_pipeline_tasks: dict[str, asyncio.Task] = {}
_locks_guard = asyncio.Lock()

# Module-level lock serializing all DB writes on the shared aiosqlite
# connection (single connection does NOT support concurrent execute).
# Used by _is_cancelled and _analyze_one to prevent "Recursive use of
# cursors" errors.
db_lock = asyncio.Lock()

# 分片 OCR 队列轮询间隔（run_ocr_sliced 线程回调 → asyncio.Queue 的
# 等待超时）。生产 2s 足够灵敏；测试可 monkeypatch 加速。
_SLICE_QUEUE_TIMEOUT = 2.0
