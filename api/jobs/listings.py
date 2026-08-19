"""Job listings — history list, live snapshots, archives, stats."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import HTTPException, Request

from config import config
from db.client import get_db
from api.jobs import router
from api.jobs.status import _get_job_progress, _parse_ocr_progress

logger = logging.getLogger(__name__)

@router.get("")
async def list_jobs(page: int = 1, page_size: int = 20, request: Request = None):
    """List active (non-archived) jobs with pagination.

    Returns JSON for AJAX-loaded history list (no full page reload).
    """
    # P2-1: GET 读端点守卫统一 — 本地单用户数据（文件名/状态/耗时）不暴露给
    # 任意网页探测（恶意站点可用 <img>/<script> 发起无 CORS 的 GET 侧信道）。
    # request=None 时跳过（单元测试直接调用路径）。
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    page = max(1, page)
    page_size = max(1, min(page_size, 100))  # cap to prevent abuse
    offset = (page - 1) * page_size

    db = await get_db()
    count_cursor = await db.execute(
        "SELECT COUNT(*) FROM jobs WHERE status != 'archived'"
    )
    total_jobs = (await count_cursor.fetchone())[0]
    total_pages = (total_jobs + page_size - 1) // page_size

    cursor = await db.execute(
        "SELECT id, filename, status, total_pages, created_at, finished_at, pdf_path, ocr_progress "
        "FROM jobs WHERE status != 'archived' "
        "ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (page_size, offset),
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    # Don't expose pdf_path in JSON response
    for r in rows:
        r.pop("pdf_path", None)
        r["ocr_progress"] = _parse_ocr_progress(r.get("ocr_progress"))

    return {
        "jobs": rows,
        "page": page,
        "page_size": page_size,
        "total_jobs": total_jobs,
        "total_pages": total_pages,
    }

async def _live_jobs_snapshot(db) -> list[dict]:
    """收集所有活跃任务 + 最近终态任务的进度快照（/api/jobs/live 推送体）。

    活跃任务之外，终态 job（review/error/cancelled 等，近 10 分钟内）也
    继续推送：upload 页的行状态/按钮要等收到终态快照才会切换到"可复核"，
    只推活跃任务会让任务行永远卡在"分析中"、归档/删除按钮永不启用
    （对抗审查发现，需刷新页面才恢复）。archived 排除（归档区单独渲染）。

    抽成纯函数便于单测（httpx ASGITransport 无法交付永不结束的
    SSE 流——它要等 app 完成后才返回 Response）。
    """
    # Runtime resolution — tests monkeypatch api.jobs._ACTIVE_STATUSES.
    from api.jobs import _ACTIVE_STATUSES
    placeholders = ",".join("?" * len(_ACTIVE_STATUSES))
    cursor = await db.execute(
        f"SELECT id FROM jobs WHERE status IN ({placeholders}) "
        "OR (status NOT IN ('archived') "
        "AND finished_at IS NOT NULL "
        "AND finished_at > datetime('now', 'localtime', '-10 minutes'))",
        _ACTIVE_STATUSES,
    )
    rows = await cursor.fetchall()
    snapshots = []
    for r in rows:
        progress = await _get_job_progress(db, r["id"])
        if progress:
            snapshots.append(progress)
    return snapshots

@router.get("/live")
async def stream_all_live_jobs(request: Request = None):
    """SSE 聚合端点：单连接推送所有活跃任务的进度快照。

    解决 HTTP/1.1 每域 6 条 EventSource 连接上限：upload 页多任务并行
    时不再每个任务各开一条连接（MAX_CONCURRENT_JOBS=3 时多标签页
    很容易撞上限），而是聚合为一条流，事件体为
    {"jobs": [{job 快照}, ...]}，前端按 job_id 分发。
    """
    # P2-1: 守卫统一（EventSource 跨源受 CORS 限制，但读端点统一策略）
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    import asyncio
    from fastapi.responses import StreamingResponse

    async def event_generator():
        db = await get_db()
        seq = 0
        yield "retry: 2000\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    snapshots = await _live_jobs_snapshot(db)
                except Exception as e:
                    # P0-4 修复：与单 job 流同款守卫 — DB 异常记录后跳过本轮，
                    # 不让聚合流静默中断（多任务进度全断）。
                    logger.error(f"SSE live snapshot query failed: {e!r}")
                    await asyncio.sleep(3)
                    continue
                seq += 1
                yield f"id: {seq}\ndata: {json.dumps({'jobs': snapshots}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"SSE live stream crashed: {e!r}", exc_info=True)
            raise

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

@router.get("/archived/list")
async def list_archived(request: Request = None):
    """列出已归档的 jobs。"""
    # P2-1: GET 读端点守卫统一
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, filename, status, total_pages, created_at, finished_at "
        "FROM jobs WHERE status = 'archived' ORDER BY created_at DESC LIMIT 100"
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    return {"archived": rows, "count": len(rows)}

@router.get("/stats/overview")
async def stats_overview(request: Request = None):
    """数据库存储统计 — 用于监控累积情况。"""
    # P2-1: GET 读端点守卫统一
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    import os
    db = await get_db()

    # 数据库文件大小
    db_path = config["app"].database_path
    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0

    # 各表记录数
    cursor = await db.execute("SELECT COUNT(*) FROM jobs")
    total_jobs = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM jobs WHERE status != 'archived'")
    active_jobs = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM page_cache")
    total_pages = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM findings")
    total_findings = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM audit_log")
    total_audit = (await cursor.fetchone())[0]

    # output 目录大小
    output_dir = Path(config["app"].output_dir)
    pdf_size = 0
    pdf_count = 0
    if output_dir.exists():
        for f in output_dir.rglob("*.pdf"):
            pdf_size += f.stat().st_size
            pdf_count += 1

    return {
        "database": {
            "path": str(db_path),
            "size_mb": round(db_size / 1024 / 1024, 2),
        },
        "jobs": {
            "total": total_jobs,
            "active": active_jobs,
            "archived": total_jobs - active_jobs,
        },
        "page_cache": total_pages,
        "findings": total_findings,
        "audit_log": total_audit,
        "pdf_storage": {
            "dir": str(output_dir),
            "count": pdf_count,
            "size_mb": round(pdf_size / 1024 / 1024, 2),
        },
    }
