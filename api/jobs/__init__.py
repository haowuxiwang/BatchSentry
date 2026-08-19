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

from core.pipeline import (
    InvalidTransitionError,
    db_lock,
    launch_pipeline,
    transition_status,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# Re-export builtin open under the module namespace — tests monkeypatch
# api.jobs.open to force read failures during magic-byte validation.
open = builtins.open  # noqa: A001

# Phase 5A: stream upload in chunks instead of reading the whole PDF into
# memory. 8 MB chunks keep peak memory low even for 200 MB PDFs and let us
# enforce the size limit without ever holding the full file in RAM.
_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB
_MAX_PDF_BYTES = 200 * 1024 * 1024  # 200 MB

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


# Import submodules AFTER router/constants exist — they decorate the router
# and read shared names from this namespace.
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
_parse_ocr_progress = status._parse_ocr_progress
stream_job_progress = status.stream_job_progress
cancel_job = actions.cancel_job
retry_job = actions.retry_job
archive_job = actions.archive_job
unarchive_job = actions.unarchive_job
delete_job = actions.delete_job
