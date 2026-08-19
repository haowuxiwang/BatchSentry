"""Job status + SSE progress stream."""
from __future__ import annotations

import json
import logging

from fastapi import HTTPException, Request

from db.client import get_db
from api.jobs import _TERMINAL_STATUSES, router
from api.jobs.page_image import _page_finding_counts

logger = logging.getLogger(__name__)

@router.get("/{job_id}")
async def get_job_status(job_id: str, request: Request = None):
    """Get job status, progress, and findings summary."""
    # P2-1: GET 读端点守卫统一
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = await cursor.fetchone()
    if not job:
        raise HTTPException(404, "Job not found")

    cursor = await db.execute(
        "SELECT COUNT(*) FROM page_cache WHERE job_id = ?", (job_id,)
    )
    pages_ocr = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT COUNT(*) FROM page_cache WHERE job_id = ? AND structured_json IS NOT NULL",
        (job_id,),
    )
    pages_analyzed = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT COUNT(*) FROM findings WHERE job_id = ?", (job_id,)
    )
    total_findings = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT COUNT(*) FROM findings WHERE job_id = ? AND status = 'pending'", (job_id,)
    )
    review_findings = (await cursor.fetchone())[0]

    page_finding_counts = await _page_finding_counts(db, job_id)

    return {
        "id": job["id"],
        "filename": job["filename"],
        "status": job["status"],
        "total_pages": job["total_pages"],
        "failed_pages": job["failed_pages"],
        "pages_ocr_done": pages_ocr,
        "pages_analyzed": pages_analyzed,
        "total_findings": total_findings,
        "review_findings": review_findings,
        "created_at": job["created_at"],
        "finished_at": job["finished_at"],
        "error_message": job["error_message"],
        "stage1_ms": job["stage1_ms"],
        "stage2_ms": job["stage2_ms"],
        "stage3_ms": job["stage3_ms"],
        "ocr_progress": _parse_ocr_progress(job["ocr_progress"] if "ocr_progress" in job.keys() else None),
        "self_heal_progress": _parse_self_heal_progress(job["ocr_progress"] if "ocr_progress" in job.keys() else None),
        "phase": _derive_phase(job["status"], pages_analyzed, job["total_pages"] or 0),
        "page_finding_counts": page_finding_counts,
        "ocr_backend_used": job["ocr_backend_used"] if "ocr_backend_used" in job.keys() else None,
        "ocr_backend_display": _ocr_backend_display(job),
    }

def _ocr_backend_display(job) -> str | None:
    """ocr_backend_used → 中文显示名（zh_map 单一来源）。"""
    raw = job["ocr_backend_used"] if "ocr_backend_used" in job.keys() else None
    if not raw:
        return None
    from core.zh_map import zh_ocr_backend
    return zh_ocr_backend(raw)


async def _get_job_progress(db, job_id: str) -> dict:
    """获取 job 进度快照（SSE 推送用）。

    复用 get_job_status 的查询逻辑，但返回精简字段。
    对抗审查（中文化收尾）：每 3 秒一次的 SSE 轮询此前 SELECT * 全列 —
    pdf_path / md5 / error_message 全文等无关字段随每次推送传输；
    改为投影到推送实际使用的列（jobs 表行内多数列从不用于进度）。
    """
    cursor = await db.execute(
        "SELECT id, status, total_pages, error_message, failed_pages, "
        "stage1_ms, stage2_ms, stage3_ms, ocr_progress, ocr_backend_used "
        "FROM jobs WHERE id = ?", (job_id,)
    )
    job = await cursor.fetchone()
    if not job:
        return None

    cursor = await db.execute(
        "SELECT COUNT(*) FROM page_cache WHERE job_id = ?", (job_id,)
    )
    pages_ocr = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT COUNT(*) FROM page_cache WHERE job_id = ? AND structured_json IS NOT NULL",
        (job_id,),
    )
    pages_analyzed = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT COUNT(*) FROM findings WHERE job_id = ?", (job_id,)
    )
    total_findings = (await cursor.fetchone())[0]

    page_finding_counts = await _page_finding_counts(db, job_id)

    return {
        "id": job["id"],
        "status": job["status"],
        "total_pages": job["total_pages"] or 0,
        "pages_ocr_done": pages_ocr,
        "pages_analyzed": pages_analyzed,
        "total_findings": total_findings,
        "error_message": job["error_message"],
        "failed_pages": job["failed_pages"],
        "stage1_ms": job["stage1_ms"],
        "stage2_ms": job["stage2_ms"],
        "stage3_ms": job["stage3_ms"],
        "ocr_progress": _parse_ocr_progress(job["ocr_progress"] if "ocr_progress" in job.keys() else None),
        "self_heal_progress": _parse_self_heal_progress(job["ocr_progress"] if "ocr_progress" in job.keys() else None),
        "phase": _derive_phase(job["status"], pages_analyzed, job["total_pages"] or 0),
        "page_finding_counts": page_finding_counts,
        "ocr_backend_used": job["ocr_backend_used"] if "ocr_backend_used" in job.keys() else None,
        "ocr_backend_display": _ocr_backend_display(job),
    }

def _parse_ocr_progress(raw) -> dict:
    """解析 jobs.ocr_progress JSON 字符串 → {"done": N, "total": M}。

    容忍 None / 空 / 非法 JSON（返回空 dict，前端回退到 pages_ocr_done）。
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {"done": int(data.get("done", 0)), "total": int(data.get("total", 0))}
    except (ValueError, TypeError):
        pass
    return {}


def _parse_self_heal_progress(raw) -> dict | None:
    """解析 ocr_progress JSON 中的 self_heal 子键（空页自愈进度）。

    主 OCR 完成后自愈期间 done==total 不变，客户端靠该子键显示
    "空页自愈 x/y"；无自愈/未进行中返回 None（前端不显示）。
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
        sh = data.get("self_heal") if isinstance(data, dict) else None
        if isinstance(sh, dict) and sh.get("total"):
            return {
                "done": int(sh.get("done", 0)),
                "total": int(sh.get("total", 0)),
                "pages": [int(p) for p in (sh.get("pages") or [])],
            }
    except (ValueError, TypeError):
        pass
    return None


def _derive_phase(status: str, pages_analyzed: int, total_pages: int) -> str:
    """派生阶段指示（SSE 前端进度文案用）。

    translating 覆盖 Stage 2 + Stage 3 两段 — stages 之间无状态位，
    用"页分析完成数 == 总页数"推断已进入跨页分析（stage2 完成后才
    启动 stage3，毫秒级边界误差可接受）。
    """
    if status == "ocr_running":
        return "ocr"
    if status == "analyzing":
        return "cross" if total_pages > 0 and pages_analyzed >= total_pages else "analyze"
    if status in _TERMINAL_STATUSES:
        return "done"
    return "idle"

@router.get("/{job_id}/stream")
async def stream_job_progress(job_id: str, request: Request = None):
    """SSE 端点：实时推送 job 进度，直到终态。

    前端通过 EventSource 订阅，每 2 秒收到一次进度更新。
    遇到终态 (review/partial_review/error/cancelled/archived) 后推送最终状态并关闭。
    """
    # P2-1: 守卫统一
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
                    logger.info(f"[{job_id}] SSE client disconnected, stopping progress stream")
                    return
                try:
                    progress = await _get_job_progress(db, job_id)
                except Exception as e:
                    # P0-4 修复：DB 异常（连接断/锁）此前静默冒泡 → 流中断
                    # 且服务端零日志，"进度卡住"无法定位。记录后推送错误帧
                    # 让前端走重连逻辑。
                    logger.error(f"[{job_id}] SSE progress query failed: {e!r}")
                    seq += 1
                    yield (f"id: {seq}\n"
                           f"data: {json.dumps({'type': 'error', 'message': '进度查询失败'}, ensure_ascii=False)}\n\n")
                    await asyncio.sleep(3)
                    continue
                if progress is None:
                    seq += 1
                    # 注意：不能用 `event: error` 帧 — SSE 规范中 error 是保留事件
                    # 类型，浏览器收到后立即断开连接且不暴露 data，前端无法区分
                    # "job 不存在" 与网络抖动。改用普通 message 帧携带 type 字段。
                    yield (f"id: {seq}\n"
                           f"data: {json.dumps({'type': 'error', 'message': '任务不存在'}, ensure_ascii=False)}\n\n")
                    return

                payload = json.dumps(progress, ensure_ascii=False)
                seq += 1
                # 每条事件带自增 id：EventSource 断线重连时自动携带
                # Last-Event-ID；快照是幂等全量，重放/重连后立即自愈。
                yield f"id: {seq}\ndata: {payload}\n\n"

                if progress["status"] in _TERMINAL_STATUSES:
                    seq += 1
                    yield f"id: {seq}\nevent: done\ndata: {payload}\n\n"
                    return

                await asyncio.sleep(3)
        except asyncio.CancelledError:
            # 客户端断开时 Starlette 取消生成器 — 正常路径，不算错误
            raise
        except Exception as e:
            logger.error(f"[{job_id}] SSE stream crashed: {e!r}", exc_info=True)
            raise

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲（如有反向代理）
        },
    )
