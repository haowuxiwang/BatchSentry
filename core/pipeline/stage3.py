"""Stage 3 — cross-page analysis + findings persistence (module refactor)"""
from __future__ import annotations

import json
import logging
import time

from core.pipeline.state import _audit_log, transition_status

logger = logging.getLogger(__name__)
async def _run_stage3_cross_analysis(
    db, job_id: str, stage1_ms: int, stage2_ms: int,
    failed_pages: list[int], pipeline_start: float,
) -> None:
    """Cross-page analysis + findings persistence + final status. (refactor)"""
    # Runtime resolution — tests rebuild core.pipeline.db_lock.
    from core.pipeline import db_lock
    # Runtime resolution — tests patch core.pipeline.{_is_cancelled,
    # analyze_cross_page}.
    from core.pipeline import (
        _is_cancelled as _run_is_cancelled,
        analyze_cross_page as _run_analyze_cross,
    )
    # Check cancellation
    if await _run_is_cancelled(job_id):
        return None

    # ── Stage 3: Cross-page analysis ────────────────────────
    logger.info(f"[{job_id}] Stage 3: Cross-page analysis...")
    stage3_start = time.time()

    cursor = await db.execute(
        "SELECT page, structured_json FROM page_cache WHERE job_id = ? ORDER BY page",
        (job_id,),
    )
    page_structures = []
    for row in await cursor.fetchall():
        if row["structured_json"]:
            try:
                data = json.loads(row["structured_json"])
                # Skip pages with parse errors
                if not data.get("_parse_error") and not data.get("_ocr_empty"):
                    page_structures.append({"page": row["page"], "data": data})
            except json.JSONDecodeError:
                logger.warning(f"[{job_id}] Stage 3: Failed to parse page_cache page={row['page']}")

    # P-C3 修复：analyze_cross_page 调用前检查取消状态，
    # 避免取消后仍进入跨页分析（cancelling → review 非法转换）
    if await _run_is_cancelled(job_id):
        return
    findings = await _run_analyze_cross(page_structures, job_id=job_id)
    # P-C3 修复：analyze_cross_page 调用后再检查一次取消状态，
    # 避免在跨页分析期间用户点取消后继续写入 findings / 转 review
    if await _run_is_cancelled(job_id):
        return
    stage3_ms = int((time.time() - stage3_start) * 1000)
    logger.info(
        f"[{job_id}] Stage 3: Cross-page analysis done in {stage3_ms}ms "
        f"({len(findings)} findings from {len(page_structures)} pages)"
    )

    # 保存 findings — 按 severity 统计，便于审计和调试
    # 流式输出优化：source='llm_page' 的 findings 已在 Stage 2 每页完成时
    # 写入 DB，此处跳过避免重复。只写入 rule/llm_cross/llm_fallback findings。
    #
    # 对抗审查(cr-1): retry 场景 — B4 指纹查重仅对确定性生成的 rule/
    # llm_fallback 有效；llm_cross/user_rule 的 description 由 LLM 自然
    # 语言生成（temperature=0.1），同一异常两次运行措辞几乎必然不同，
    # 无法指纹判重 → 每次 partial_review/error 重试都会重复插入。方案：
    # Stage 3 重算前删除本 job 待审（pending）的 LLM 生成型 findings
    # （含 user_rule），已人工裁决（confirmed/rejected/corrected）的保留。
    cur = await db.execute(
        "DELETE FROM findings WHERE job_id = ? "
        "AND source IN ('llm_cross', 'llm_fallback', 'user_rule') AND status = 'pending'",
        (job_id,),
    )
    await db.commit()
    severity_counts = {"critical": 0, "warning": 0, "info": 0}
    skipped_llm_page = 0
    inserted = 0
    # 批量写入：rule/llm_cross 是确定性生成，内存指纹去重 + INSERT OR IGNORE
    # 双保险（idx_findings_dedup UNIQUE 索引兜底，v5）。避免逐条 select-then-
    # insert 的 2×N 次 DB 往返（51 页真实文件 400+ findings → 1000+ await）。
    dedup_seen: set[tuple] = set()
    batch_rows: list[tuple] = []
    for f in findings:
        # 跳过已在 Stage 2 写入的 page-level LLM findings
        if f.get("source") == "llm_page":
            skipped_llm_page += 1
            continue
        # robustness-B4: retry 会重新执行 Stage 3，确定性生成的 findings
        # 按 (job_id, source, page, type, description) 指纹去重。
        fingerprint = (
            job_id, f.get("source", "rule"), f["page"], f["type"], f["description"],
        )
        if fingerprint in dedup_seen:
            logger.debug(
                f"[{job_id}] findings dup skipped (page={f['page']} "
                f"source={f.get('source')} type={f['type']})"
            )
            continue
        dedup_seen.add(fingerprint)
        # A3 修复（Round 3）：severity 计数改为在去重/跳过之后统计
        # 实际写入行 — 原实现先计数后跳过，llm_page（Stage 2 已入库）
        # 和重复行被重复计入，日志与真实数据不符（GMP 审计误导）。
        sev = f.get("severity", "info")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
        batch_rows.append((
            job_id, f["page"], f["type"], f["severity"], f["description"],
            f.get("ocr_text"), f.get("operator"), f.get("source", "rule"),
            f.get("rule_id") if f.get("source") == "user_rule" else None,
        ))
        inserted += 1
    if batch_rows:
        # 对抗审查 T3.2：批量写 findings 与其他写事务（transition/audit/
        # 进度更新）串行化 —— 多 job 并行进入 Stage 3 时防止 executemany+
        # commit 与其他写穿插交错事务边界。
        async with db_lock:
            await db.executemany(
                "INSERT OR IGNORE INTO findings "
                "(job_id, page, type, severity, description, ocr_text, operator, source, user_rule_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))",
                batch_rows,
            )
            await db.commit()
    logger.info(
        f"[{job_id}] DB: findings inserted ({inserted} new + {skipped_llm_page} "
        f"llm_page skipped, severity={severity_counts})"
    )

    # Determine final status
    total_cost_ms = int((time.time() - pipeline_start) * 1000)
    final_status = "partial_review" if failed_pages else "review"

    await db.execute(
        "UPDATE jobs SET finished_at = datetime('now','localtime'), "
        "stage1_ms = ?, stage2_ms = ?, stage3_ms = ?, failed_pages = ? "
        "WHERE id = ?",
        (stage1_ms, stage2_ms, stage3_ms,
         json.dumps(failed_pages) if failed_pages else None, job_id),
    )
    await transition_status(db, job_id, final_status,
                            f"Pipeline complete: {len(findings)} findings, {len(failed_pages)} failed")

    # 飞书通知（旁路：失败不影响主流程；成功/部分完成均推送）
    try:
        from core.notify import notify_job
        await notify_job(job_id, final_status)
    except Exception:
        pass  # notify_job 自身已兜底，此处双保险防异常逃逸

    logger.info(f"[{job_id}] Pipeline complete: status={final_status}, "
                 f"{len(findings)} findings, {len(failed_pages)} failed pages, "
                 f"total={total_cost_ms}ms (OCR={stage1_ms} LLM={stage2_ms} Cross={stage3_ms})")
    await _audit_log(db, job_id, "pipeline_complete",
                     f"status={final_status} findings={len(findings)} "
                     f"failed={len(failed_pages)} total={total_cost_ms}ms")
