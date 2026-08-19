"""Job lifecycle actions — cancel / retry / archive / unarchive / delete."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import HTTPException, Request

from config import config
from db.client import get_db
from api.jobs import router
from api.jobs.page_image import _invalidate_pdf_doc

logger = logging.getLogger(__name__)

@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request = None):
    """Cancel a running job."""
    # Runtime resolution — tests monkeypatch api.jobs.{transition_status,
    # InvalidTransitionError}.
    from api.jobs import InvalidTransitionError, transition_status
    # 对抗审查（cr-18）：cancel/retry/archive/unarchive 为无请求体 POST
    # （CORS 简单请求，恶意网页可跨站触发），补齐 is_local_request 守卫
    # 与上传端点（cr-13）对齐。request=None 时跳过（单元测试直调场景）。
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    try:
        await transition_status(db, job_id, "cancelling", "用户请求取消")
        logger.info(f"[{job_id}] Cancel requested by user")
    except InvalidTransitionError as e:
        logger.warning(f"[{job_id}] Cancel blocked: {e}")
        raise HTTPException(400, str(e))
    return {"ok": True, "status": "cancelling"}

@router.post("/{job_id}/retry")
async def retry_job(job_id: str, request: Request = None):
    """Retry a failed or cancelled job from where it left off."""
    # Runtime resolution — tests monkeypatch api.jobs.{Path,
    # _ACTIVE_STATUSES, _MAX_CONCURRENT_JOBS, db_lock, launch_pipeline,
    # transition_status, InvalidTransitionError}.
    from api.jobs import (
        Path,
        _ACTIVE_STATUSES,
        _MAX_CONCURRENT_JOBS,
        InvalidTransitionError,
        db_lock,
        launch_pipeline,
        transition_status,
    )
    # cr-18: CSRF 守卫（retry 会真实启动 pipeline 消耗 OCR/LLM 配额）。
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = await cursor.fetchone()
    if not job:
        raise HTTPException(404, "任务不存在")

    # Concurrency guard: retry must respect MAX_CONCURRENT_JOBS like upload.
    # Without this, mass-retrying failed jobs spawns unbounded pipelines,
    # defeating the memory protection (OCR results held in RAM per job).
    db_active = await get_db()
    async with db_lock:
        cursor = await db_active.execute(
            f"SELECT COUNT(*) FROM jobs WHERE status IN ({','.join('?' * len(_ACTIVE_STATUSES))})",
            _ACTIVE_STATUSES,
        )
        active_count = (await cursor.fetchone())[0]
        if active_count >= _MAX_CONCURRENT_JOBS:
            raise HTTPException(
                409,
                f"已有 {active_count} 个任务在处理中，上限为 {_MAX_CONCURRENT_JOBS}。请等待完成或取消后再试。",
            )

    if not job["pdf_path"] or not Path(job["pdf_path"]).exists():
        raise HTTPException(400, "PDF 文件不存在于磁盘")

    try:
        await transition_status(db, job_id, "pending", f"从 {job['status']} 重试")
        # 清除上次的 error_message，避免 review 页面残留旧的错误提示
        # （recover_stuck_jobs / 之前失败会写入 error_message，重试应视为全新尝试）
        await db.execute(
            "UPDATE jobs SET error_message = NULL, finished_at = NULL WHERE id = ?",
            (job_id,),
        )
        # P1-6: review → pending = 全量重新分析。review 是完整终态，其分析
        # 产物（findings + structured_json）代表上一次完整结果 — 用户重试
        # 的意图是"重新分析"而非"补页"（补页是 partial_review 的语义）。
        # 保留 raw_html（OCR 缓存），Stage 1 走 stage1_skipped 复用路径，
        # 不重复消耗 OCR 配额；清空后 Stage 2 全量重新提取、Stage 3 重跑规则。
        if job["status"] == "review":
            await db.execute("DELETE FROM findings WHERE job_id = ?", (job_id,))
            await db.execute(
                "UPDATE page_cache SET structured_json = NULL, analyzed_at = NULL "
                "WHERE job_id = ? AND raw_html IS NOT NULL",
                (job_id,),
            )
            await db.execute(
                "INSERT INTO audit_log (job_id, action, detail, created_at) VALUES (?, 'analysis_reset', ?, datetime(\'now\',\'localtime\'))",
                (job_id, "从 review 重试：已清空 findings 与结构化分析（全量重新分析，保留 OCR 缓存）"),
            )
        await db.commit()
        logger.info(f"[{job_id}] Retry requested from status={job['status']}")
    except InvalidTransitionError as e:
        logger.warning(f"[{job_id}] Retry blocked: {e}")
        raise HTTPException(400, str(e))

    launch_pipeline(job_id, job["pdf_path"])
    return {"ok": True, "status": "pending"}

@router.post("/{job_id}/archive")
async def archive_job(job_id: str, keep_pdf: bool = True, request: Request = None):
    """归档 job — 标记为已归档，从前端列表隐藏，但保留数据用于审计。

    Args:
        keep_pdf: True（默认）保留 PDF 用于审计追溯；False 删除 PDF 释放磁盘。
                  数据库记录始终保留。
    """
    # Runtime resolution — tests monkeypatch api.jobs.{_ACTIVE_STATUSES,
    # transition_status, InvalidTransitionError}.
    from api.jobs import _ACTIVE_STATUSES, InvalidTransitionError, transition_status
    # cr-18: CSRF 守卫。
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    # 状态机健壮性（Round 3 审计）：归档 = 已完成任务的存档。运行中的 job
    # （pipeline 正持有，会继续向 DB 写入并尝试状态转换）若被归档，pipeline
    # 末尾的 transition 会撞 InvalidTransitionError 并把 job 强改 error —
    # 状态错乱且用户困惑。与 delete_job 的 _ACTIVE_STATUSES 拒绝对齐。
    cursor = await db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")
    if row["status"] in _ACTIVE_STATUSES:
        logger.warning(
            f"[{job_id}] Archive blocked: job is active (status={row['status']})"
        )
        raise HTTPException(
            409,
            f"任务正在处理中（状态: {row['status']}），请等待进入复核/终态后再归档。",
        )
    try:
        await transition_status(db, job_id, "archived", "用户归档")
        logger.info(f"[{job_id}] Archived by user (keep_pdf={keep_pdf})")
    except InvalidTransitionError as e:
        logger.warning(f"[{job_id}] Archive blocked: {e}")
        raise HTTPException(400, str(e))

    # 可选：归档时删除 PDF 释放磁盘（数据库记录保留）
    if not keep_pdf:
        cursor = await db.execute("SELECT pdf_path FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        if row and row["pdf_path"]:
            pdf = Path(row["pdf_path"])
            output_root = Path(config["app"].output_dir).resolve()
            try:
                job_dir = pdf.parent.resolve()
                job_dir.relative_to(output_root)
                # 对抗审查(cr-8): 纵深防御 — pdf_path 恰为 output 根时
                # relative_to 返回 '.' 通过校验，rmtree 会删除全部 job 目录
                if job_dir == output_root or job_dir.name != job_id:
                    logger.warning(
                        f"[{job_id}] Archive PDF cleanup skipped (unsafe path: {job_dir})"
                    )
                elif pdf.exists():
                    import shutil
                    # 对抗审查（P1-3）：归档删除 PDF 前必须先失效
                    # _get_pdf_doc 的 fitz 文档句柄缓存 — 用户在 review 页
                    # 预览过该 PDF 后缓存句柄持有文件锁（Windows），
                    # rmtree(ignore_errors=True) 静默失败 → "删除 PDF"实际
                    # 未删除（GMP 数据卫生 + 磁盘不释放）。delete_job 已有
                    # 此调用（jobs.py:1069），归档路径此前漏了。
                    _invalidate_pdf_doc(job_id)
                    shutil.rmtree(job_dir, ignore_errors=True)
                    logger.info(f"[{job_id}] Archived + PDF removed: {job_dir}")
            except (ValueError, RuntimeError) as e:
                logger.warning(f"[{job_id}] Archive PDF cleanup skipped (path check): {e}")

    return {"ok": True, "status": "archived"}

@router.post("/{job_id}/unarchive")
async def unarchive_job(job_id: str, request: Request = None):
    """取消归档 — 恢复到 review 状态。"""
    # Runtime resolution — tests monkeypatch api.jobs.{Path,
    # transition_status, InvalidTransitionError}.
    from api.jobs import Path, InvalidTransitionError, transition_status
    # cr-18: CSRF 守卫。
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    # 对抗审查(cr-9): 归档时 keep_pdf=False 会删除 PDF，恢复后 review 页
    # PDF 预览 404、retry 报"PDF file not found"。无 PDF 的 job 允许恢复
    # 查看 findings（数据仍完整），但给出明确提示由前端展示。
    cursor = await db.execute("SELECT pdf_path FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    pdf_missing = bool(row and (not row["pdf_path"] or not Path(row["pdf_path"]).exists()))
    try:
        await transition_status(db, job_id, "review", "用户取消归档")
        logger.info(f"[{job_id}] Unarchived by user (pdf_missing={pdf_missing})")
    except InvalidTransitionError as e:
        logger.warning(f"[{job_id}] Unarchive blocked: {e}")
        raise HTTPException(400, str(e))
    return {"ok": True, "status": "review", "pdf_missing": pdf_missing}

@router.delete("/{job_id}")
async def delete_job(job_id: str, keep_pdf: bool = False, request: Request = None):
    """彻底删除 job — 删除数据库记录 + PDF 文件。

    生产环境清理必需，避免数据无限累积。

    Args:
        keep_pdf: True 时保留 PDF 原文件（用于审计），False 时一并删除
    """
    # P2-1: DELETE 破坏性端点守卫（与 cancel/archive 等统一）。
    # request=None 时跳过守卫（单元测试直接调用路径）。
    # Runtime resolution — tests monkeypatch api.jobs.{_ACTIVE_STATUSES, db_lock}.
    from api.jobs import (
        _ACTIVE_STATUSES,
        db_lock,
    )
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    import shutil

    db = await get_db()
    cursor = await db.execute("SELECT pdf_path, status, filename FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")

    # 安全检查：拒绝删除正在运行的 job。运行中的 pipeline task 仍会
    # 向 DB / 文件系统写入，强行删除会留下孤儿 task 与不一致状态。
    # 用户应先 cancel 等待终态后再删除。
    if row["status"] in _ACTIVE_STATUSES:
        logger.warning(
            f"[{job_id}] Delete blocked: job is active (status={row['status']})"
        )
        if row["status"] == "cancelling":
            # P1-2: 文案与实现一致 — 单次 LLM 调用最长 240s（4 分钟），
            # 取消检查点位于调用之间，取消后还需等当前调用自然结束
            msg = "任务正在取消中，请等待当前 LLM 调用结束（最长约 4 分钟）后再删除。"
        else:
            msg = f"任务正在处理中（状态: {row['status']}），请先取消并等待任务进入终态后再删除。"
        raise HTTPException(409, msg)

    pdf_path = row["pdf_path"]
    # P-ADV3 修复：DELETE 操作的 audit_log INSERT + 级联 DELETE 必须在 db_lock
    # 内原子执行，与 pipeline 的 transition_status / _record_llm_call 共享
    # 同一锁，防止 aiosqlite 单连接上的事务边界被穿插（一个 commit 可能
    # 提前提交另一方的未完成写入）。
    async with db_lock:
        # Record deletion in a separate audit row (survives cascade delete)
        # GMP traceability: record destruction must itself be traceable
        await db.execute(
            "INSERT INTO audit_log (job_id, action, detail, created_at) VALUES (?, ?, ?, datetime('now','localtime'))",
            ("_system", "job_deleted", f"job_id={job_id} filename={row['filename']} status={row['status']} keep_pdf={keep_pdf}"),
        )
        logger.info(f"[{job_id}] Delete requested: filename={row['filename']} keep_pdf={keep_pdf}")

        # 删除数据库记录（级联删除 page_cache, findings, audit_log, llm_call_audit）
        try:
            await db.execute("DELETE FROM page_cache WHERE job_id = ?", (job_id,))
            await db.execute("DELETE FROM findings WHERE job_id = ?", (job_id,))
            await db.execute("DELETE FROM audit_log WHERE job_id = ?", (job_id,))
            await db.execute("DELETE FROM llm_call_audit WHERE job_id = ?", (job_id,))
            await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    # 删除 PDF 文件
    if not keep_pdf and pdf_path:
        # 先失效渲染缓存：fitz 句柄在 Windows 上锁住 PDF，rmtree 会
        # WinError 32 失败导致 output 目录残留（E2E 实测复现）。
        _invalidate_pdf_doc(job_id)
        pdf = Path(pdf_path)
        # Security: validate job_dir is inside output_dir to prevent
        # rmtree on arbitrary paths if pdf_path was tampered with.
        output_root = Path(config["app"].output_dir).resolve()
        try:
            job_dir = pdf.parent.resolve()
            job_dir.relative_to(output_root)
            # 对抗审查(cr-8): 纵深防御 — pdf_path 恰为 output 根时
            # relative_to 返回 '.' 通过校验，rmtree 会删除全部 job 目录
            if job_dir == output_root:
                raise ValueError("job_dir is output root")
        except (ValueError, RuntimeError):
            logger.error(
                f"[{job_id}] Refused delete: job_dir {pdf.parent} "
                f"outside output_root {output_root}"
            )
            raise HTTPException(400, "Refused to delete: path outside output dir")
        if pdf.exists():
            # Phase 7: rmtree failures (file locked on Windows) no longer
            # leave the system inconsistent — DB records are already deleted
            # above, so we log a warning + return partial_success. The
            # orphaned dir is harmless (no DB references it) and can be
            # cleaned up by the user or a future gc pass.
            try:
                shutil.rmtree(job_dir, ignore_errors=False)
                logger.info(f"[{job_id}] Removed job_dir: {job_dir}")
            except (OSError, shutil.Error) as e:
                logger.warning(
                    f"[{job_id}] rmtree failed (DB already cleaned): {e}"
                )
                # Mark as best-effort: DB is consistent, file remains.
                # Caller can retry deletion or manually clean output_dir.
                return {
                    "ok": True, "deleted": True, "keep_pdf": keep_pdf,
                    "warning": f"数据库记录已删除，但文件清理失败: {e}",
                }
        else:
            logger.warning(f"[{job_id}] PDF missing on delete: {pdf_path}")
    logger.info(f"[{job_id}] Delete complete")

    return {"ok": True, "deleted": True, "keep_pdf": keep_pdf}
