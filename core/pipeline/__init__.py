"""Pipeline package — orchestration across stages 1→2→3 (module refactor).

Public API is re-exported here so `from core.pipeline import X` keeps
working (api/jobs.py, main.py, tests). Sub-modules resolve patchable
collaborators at call time via `from core.pipeline import X as _run_X`
so tests patching `core.pipeline.X` still take effect.
"""
import asyncio  # noqa: F401  (tests patch core.pipeline.asyncio.*)

from config import config  # tests patch core.pipeline.config / engine uses it

# Analyzer entry points — re-exported so tests patch core.pipeline.analyze_page
# / analyze_cross_page and stage modules resolve them at call time.
from core.page_analyzer import analyze_page, AnalysisCancelled
from core.cross_page_analyzer import analyze_cross_page

from core.pipeline.locks import (
    _pipeline_locks,
    _pipeline_tasks,
    _locks_guard,
    db_lock,
    _SLICE_QUEUE_TIMEOUT,
    live_tasks_for,
    register_pipeline_task,
    unregister_pipeline_task,
)
from core.pipeline.state import (
    InvalidTransitionError,
    VALID_TRANSITIONS,
    _STUCK_STATUSES,
    _audit_log,
    _transition_status_unlocked,
    transition_status,
    recover_stuck_jobs,
    _is_cancelled,
    _update_ocr_progress,
    _update_self_heal_progress,
)
from core.pipeline.ocr_support import (
    _get_ocr_backend,
    _get_ocr_chain,
    _sanitize_ocr_text,
    _pdf_page_count,
    _run_ocr_with_failover,
)
from core.pipeline.self_heal import _self_heal_empty_pages
from core.pipeline.stage1 import _run_stage1_full, _get_existing_pages
from core.pipeline.stage2 import (
    _run_stage2_analysis,
    _analyze_one,
    _get_analyzed_pages,
)
from core.pipeline.stage3 import _run_stage3_cross_analysis
from core.pipeline.engine import (
    launch_pipeline,
    run_pipeline,
    _run_pipeline_impl,
    _run_sliced_stage1_2,
)

# ── 对外（含**打桩**）名字清单 ──────────────────────────────────────
# 这里是**可 patch 面**（测试按包路径打桩），不是普通的 import 清单：
# 删掉其中任何一个都会让 `patch("core.pipeline.X")` 静默失效 —— 那类失败
# 表现为"测试仍然绿、但打桩没生效"，比 ImportError 难查得多。
# `__all__` 同时是 ruff F401 的正确豁免方式（比 38 个 noqa 更能说明意图）。
__all__ = [
    "AnalysisCancelled",
    "InvalidTransitionError",
    "VALID_TRANSITIONS",
    "_SLICE_QUEUE_TIMEOUT",
    "_STUCK_STATUSES",
    "_analyze_one",
    "_audit_log",
    "_get_analyzed_pages",
    "_get_existing_pages",
    "_get_ocr_backend",
    "_get_ocr_chain",
    "_is_cancelled",
    "_locks_guard",
    "_pdf_page_count",
    "_pipeline_locks",
    "_pipeline_tasks",
    "_run_ocr_with_failover",
    "_run_pipeline_impl",
    "_run_sliced_stage1_2",
    "_run_stage1_full",
    "_run_stage2_analysis",
    "_run_stage3_cross_analysis",
    "_sanitize_ocr_text",
    "_self_heal_empty_pages",
    "_transition_status_unlocked",
    "_update_ocr_progress",
    "_update_self_heal_progress",
    "analyze_cross_page",
    "analyze_page",
    "config",
    "db_lock",
    "launch_pipeline",
    "live_tasks_for",
    "recover_stuck_jobs",
    "register_pipeline_task",
    "run_pipeline",
    "transition_status",
    "unregister_pipeline_task",
]
