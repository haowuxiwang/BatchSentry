"""Job management API — router + shared constants + public surface.

Split from api/jobs.py (1236 lines) into api/jobs/{upload,listings,page_image,
status,actions}.py. This module owns the router and the names tests patch via
the api.jobs namespace (launch_pipeline / Path / open / _MAX_CONCURRENT_JOBS /
_ACTIVE_STATUSES / _MAX_IMAGE_PIXELS) — consumers resolve those at runtime via
'from api.jobs import X' so monkeypatching keeps working.
"""
from __future__ import annotations

import builtins
import logging
import os
from pathlib import Path

from fastapi import APIRouter

from config import UPLOAD_LIMITS

from db.client import get_db

from core.pipeline import (
    InvalidTransitionError,
    db_lock,
    launch_pipeline,
    transition_status,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


async def mark_launch_failed(job_id: str, exc: BaseException) -> bool:
    """把"已写 pending 但 launch 失败"的 job 收敛为 error（#142）。

    为什么必须有（对抗审查 #142）：`pending` 且**注册表里没有活 task** 的 job
    是系统的**无终态黑洞** ——
      - 看门狗不监视它（`is_watched` 只认 `pending` + 有活 task）；
      - 启动恢复要求 `created_at < process_started_at`，同进程内的孤儿要等下次重启；
      - SSE 的 `while True` 只认终态 ⇒ 界面无限等待、无任何提示。
    可达路径 = 两处 `launch_pipeline` 都不在 try 内（upload / retry）。

    返回是否真的改写（False = 并发路径已把它推进，不重复动作）。
    审计必写 —— GMP 追溯要求"为什么这个任务没跑起来"有据可查。
    """
    from core.pipeline.state import _audit_log

    db = await get_db()
    # 直接 UPDATE 而非 transition_status：本函数的语义是"异常收敛"，
    # pending → error 虽在状态机里合法，但并发下状态可能已被改写，
    # 带 status 条件更新可避免把别人的推进打回。
    async with db_lock:
        cursor = await db.execute(
            "UPDATE jobs SET status = 'error', error_message = ?, "
            "finished_at = datetime('now','localtime') "
            "WHERE id = ? AND status = 'pending'",
            (f"流水线启动失败：{type(exc).__name__}（未开始处理，可直接重试）", job_id),
        )
        await db.commit()
        changed = cursor.rowcount
    if changed:
        # 审计在 db_lock 之外写（_audit_log 自己取 db_lock，不可重入）
        try:
            await _audit_log(db, job_id, "launch_failed",
                             f"pipeline launch raised {type(exc).__name__}: {exc}")
        except Exception as audit_err:
            logger.error(f"[{job_id}] launch_failed 审计写入失败: {audit_err}")
    return changed > 0

# Re-export builtin open under the module namespace — tests monkeypatch
# api.jobs.open to force read failures during magic-byte validation.
open = builtins.open  # noqa: A001

# Phase 5A: stream upload in chunks instead of reading the whole PDF into
# memory. 8 MB chunks keep peak memory low even for 200 MB PDFs and let us
# enforce the size limit without ever holding the full file in RAM.
_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB
# 体积上限来自 config.UPLOAD_LIMITS（单一真值）—— 前端预检与页面文案同样
# 从那里派生，避免"前端放行、后端拒绝"的静默漂移。
_MAX_PDF_BYTES = UPLOAD_LIMITS["max_bytes"]

# 图片上传（Phase 13）：纸质批记录扫描件常为 jpg/png 单图。
# 设计决策 —"后端统一转 PDF"方案（最佳实践）：
#   - PaddleOCR 异步服务仅接受 PDF；MinerU 云端原生支持图片但提交协议
#     有差异。统一转 PDF 后双后端行为一致，pipeline/OCR/LLM/复核/报告
#     链路零改动，无需按后端分叉。
#   - Pillow 负责解码 + EXIF 方向修正（手机/相机竖图不修正会 90° 旋转，
#     OCR 质量灾难）；PyMuPDF 合成单页 PDF（300 DPI 映射）。
#   - 原图留档在 job 目录（GMP 追溯），jobs.pdf_path 指向转换后的 PDF。
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
_MAX_IMAGE_PIXELS = 100_000_000  # ~31623x31623 — 超大图解码内存保护（防 DoS）
_IMAGE_CONVERT_TIMEOUT_S = 120  # 图片→PDF 转换超时（P2-2）
# 图片 magic bytes 白名单（独立于扩展名校验 — 防伪装扩展名绕过后端解码路径）
_IMAGE_MAGIC_PREFIXES = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"BM", "BMP"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
)
_WEBP_MAGIC = (b"RIFF", b"WEBP")  # 8 字节偏移后为 WEBP

# Concurrency guard — prevents memory exhaustion from many parallel pipelines.
# Each pipeline holds the OCR result + LLM JSON in memory; 3 concurrent 200MB
# PDFs with multi-page OCR results can hit ~2GB. Override via MAX_CONCURRENT_JOBS.
# 对抗审查(cr-11): 非法 env 值兜底为默认 3，避免 import 崩溃。
try:
    _MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "3"))
except (TypeError, ValueError):
    _MAX_CONCURRENT_JOBS = 3
_ACTIVE_STATUSES = ("pending", "ocr_running", "ocr_done", "analyzing", "cancelling")
_TERMINAL_STATUSES = ("review", "partial_review", "error", "cancelled", "archived")


async def _count_active_jobs(db, active_statuses) -> int:
    """统计"占着并发额度"的 job 数。

    ⚠️ **结论只在持 `db_lock` 期间有效**：调用方必须已持锁，否则拿到的是
    过期快照 —— B2-7 的 TOCTOU 就是这么来的（在锁内查一次、很久之后才写）。
    单一实现点：upload（前后两道检查）与 retry 共用同一段 SQL
    （此前两处各写一份，改上限语义时容易漏改一处）。
    """
    cursor = await db.execute(
        "SELECT COUNT(*) FROM jobs WHERE status IN ("
        + ",".join("?" * len(active_statuses))
        + ")",
        active_statuses,
    )
    return (await cursor.fetchone())[0]

# S1（M6/T6.1）：SSE 轮询间隔（秒）—— 单一来源。
# 前后端三处必须一致，否则出现"客户端重连比服务端推送更快"的空转：
#   - `retry: <ms>` 帧（EventSource 重连退避）
#   - 服务端 event_generator 的推送/退避 sleep
#   - 前端文案（"每 N 秒刷新"）
# 历史缺陷：retry 写 2000ms、服务端 sleep(3)、docstring 写"每 2 秒"，
# 三方互相矛盾。现统一由此常量派生（消费方在调用期 `from api.jobs import`
# 取用，便于测试 monkeypatch）。
_SSE_POLL_SECONDS = 2


# Import submodules AFTER router/constants exist — they decorate the router
# and read shared names from this namespace.
# 路由顺序硬约束：/live（listings）必须注册在 /{job_id}（status）之前 —
# FastAPI 按注册顺序匹配，否则 GET /api/jobs/live 命中 /{job_id} 404。
# 因此 listings 必须先于 status 导入，且 listings.py 不得顶层 import status
# （会破坏该顺序，见 listings.py 头注释；status 符号在调用期解析）。
from api.jobs import upload, listings, page_image, status, actions  # noqa: E402

create_job = upload.create_job
list_jobs = listings.list_jobs
_live_jobs_snapshot = listings._live_jobs_snapshot
stream_all_live_jobs = listings.stream_all_live_jobs
list_archived = listings.list_archived
stats_overview = listings.stats_overview
get_job_page_image = page_image.get_job_page_image
_pdf_doc_cache = page_image._pdf_doc_cache
_get_pdf_doc = page_image._get_pdf_doc
_invalidate_pdf_doc = page_image._invalidate_pdf_doc
_page_finding_counts = page_image._page_finding_counts
get_job_status = status.get_job_status
_get_job_progress = status._get_job_progress
_reset_terminal_snap_cache = status._reset_terminal_snap_cache
_parse_ocr_progress = status._parse_ocr_progress
stream_job_progress = status.stream_job_progress
cancel_job = actions.cancel_job
retry_job = actions.retry_job
archive_job = actions.archive_job
unarchive_job = actions.unarchive_job
delete_job = actions.delete_job
