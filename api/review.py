"""Review API — list/update findings with audit logging."""
import json
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Form, Request

from db.client import get_db
from core.pipeline import db_lock
from core.zh_map import zh_finding_type

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["review"])

# ── #8 字段级置信度 ────────────────────────────────────────────────────────
# M2/T2.4：评分逻辑下沉 core.finding_quality（stage3 写入期落库 + 此处读取期
# 兜底共用同一实现，避免口径漂移）。`_confidence_for` 保留为薄别名，
# 兼容既有调用点与历史引用。
from core.finding_quality import (  # noqa: E402
    confidence_for as _confidence_for,
    page_is_flagged as _page_is_flagged,
)


# ── 复核反馈统计（round-23 C）────────────────────────────────
# confirm/reject 数据本已落库（findings.status + reviewed_at）但从未聚合
# 利用。统计面回答三个问题：整体误报水平（驳回率）、哪类检查最易误报
# （高频驳回类型）、哪一层最需要调优（按来源驳回率 — 规则层驳回率持续
# 偏高即阈值过紧信号，LLM 层偏高即提示词收紧信号）。供 review-stats
# 端点与报告导出共用（纯函数：对 SELECT 出的行聚合，不自查 DB）。
_STATS_TOP_TYPES = 5


def _review_stats_from_rows(rows: list[dict]) -> dict:
    """聚合 findings 行（type/severity/source/status）为复核统计。"""
    total = len(rows)
    by_status: dict[str, int] = {}
    for r in rows:
        st = r.get("status") or "pending"
        by_status[st] = by_status.get(st, 0) + 1
    pending = by_status.get("pending", 0)
    adjudicated = total - pending
    confirmed = by_status.get("confirmed", 0)
    rejected = by_status.get("rejected", 0)
    corrected = by_status.get("corrected", 0)
    confirm_rate = round(confirmed / adjudicated, 4) if adjudicated else None
    reject_rate = round(rejected / adjudicated, 4) if adjudicated else None
    # 高频驳回类型（Top N，占驳回总数份额）
    rej_by_type: dict[str, int] = {}
    for r in rows:
        if (r.get("status") or "") == "rejected":
            rej_by_type[r.get("type") or "?"] = (
                rej_by_type.get(r.get("type") or "?", 0) + 1
            )
    top_rejected = [
        {
            "type": t,
            "type_zh": zh_finding_type(t),
            "count": c,
            "share": round(c / rejected, 4) if rejected else 0,
        }
        for t, c in sorted(
            rej_by_type.items(), key=lambda kv: (-kv[1], kv[0])
        )[:_STATS_TOP_TYPES]
    ]
    # 按来源驳回率（规则阈值调优信号）：total/rejected/pending 全量给出，
    # 未裁决完成时读者自行折算口径（驳回率分母=该来源全部 findings）。
    by_src: dict[str, dict] = {}
    for r in rows:
        src = r.get("source") or "rule"
        d = by_src.setdefault(src, {"total": 0, "rejected": 0, "pending": 0})
        d["total"] += 1
        st = r.get("status") or "pending"
        if st == "rejected":
            d["rejected"] += 1
        elif st == "pending":
            d["pending"] += 1
    by_source = [
        {
            "source": s,
            "total": d["total"],
            "rejected": d["rejected"],
            "pending": d["pending"],
            "reject_rate": round(d["rejected"] / d["total"], 4),
        }
        for s, d in sorted(
            by_src.items(), key=lambda kv: (-kv[1]["total"], kv[0])
        )
    ]
    return {
        "total": total,
        "adjudicated": adjudicated,
        "pending": pending,
        "by_status": {
            "pending": pending,
            "confirmed": confirmed,
            "rejected": rejected,
            "corrected": corrected,
        },
        "confirm_rate": confirm_rate,
        "reject_rate": reject_rate,
        "top_rejected_types": top_rejected,
        "by_source": by_source,
    }


@router.get("/jobs/{job_id}/review-stats")
async def get_review_stats(job_id: str, request: Request = None):
    """复核反馈统计（round-23 C）：确认/驳回率、高频驳回类型、按来源驳回率。

    反哺规则阈值调优：某来源驳回率持续偏高（如 rule 层 > 50%）即阈值
    过紧信号；高频驳回类型定位具体检查项。GET 本地守卫与其余读端点一致。
    """
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    cursor = await db.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,))
    if not (await cursor.fetchone()):
        raise HTTPException(404, "Job 不存在")
    cursor = await db.execute(
        "SELECT type, severity, source, status FROM findings WHERE job_id = ?",
        (job_id,),
    )
    stats = _review_stats_from_rows([dict(r) for r in await cursor.fetchall()])
    stats["job_id"] = job_id
    return stats


@router.get("/jobs/{job_id}/findings")
async def list_findings(
    job_id: str,
    status: Optional[str] = None,
    page: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
    order: Optional[str] = None,
    request: Request = None,
):
    """List findings for a job, optionally filtered by status and/or page.

    统一端点：支持 status 和 page 过滤（AJAX 翻页用 page 参数）。
    分页：limit（默认 50，max 200）+ offset，防止 100+ findings 一次返回卡顿。
    order=confidence：按置信度升序（最需要人工关注的排前面）。置信度为
    读时计算（#8 字段级置信度）：LLM 生成型来源 + 所在页带完整性警告
    标记 → 扣分；确定性规则层产出满分基准。不落库（无 schema 变更），
    信号变化（如页面横幅消除后重跑）自动反映。
    """
    # P2-1: GET 读端点守卫统一（request=None 时跳过，兼容单元测试直接调用）
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    # 钳制 limit 防止滥用
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    db = await get_db()
    severity_order = (
        "CASE severity WHEN 'critical' THEN 0 "
        "WHEN 'warning' THEN 1 "
        "WHEN 'info' THEN 2 ELSE 3 END"
    )
    source_order = (
        "CASE COALESCE(source, 'rule') WHEN 'rule' THEN 0 "
        "WHEN 'llm_fallback' THEN 1 "
        "WHEN 'llm_page' THEN 2 "
        "WHEN 'llm_cross' THEN 3 ELSE 4 END"
    )
    by_confidence = (order == "confidence")
    # 按页过滤时，仅按 severity+source 排序（不需要 page）
    if by_confidence:
        # 置信度排序需全量取回后在 Python 侧排序分页（SQL 无该列）；
        # 上限 2000 行防超大 job 内存失控。
        # 对抗审查（生产事故）：page 过滤在本分支同样生效 —— 此前
        # WHERE 只有 job_id，复核页每次翻页（loadPageData 固定带
        # order=confidence）都拿到全 job 的前 50 条，问题清单与当前页
        # 完全脱钩（每页 total 恒为全局数）。
        if page:
            cursor = await db.execute(
                "SELECT * FROM findings WHERE job_id = ? AND page = ? "
                f"ORDER BY {severity_order}, {source_order}, id LIMIT 2000",
                (job_id, page),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM findings WHERE job_id = ? "
                f"ORDER BY {severity_order}, {source_order}, id LIMIT 2000",
                (job_id,),
            )
    elif page:
        order_clause = f"ORDER BY {severity_order}, {source_order}, id"
        cursor = await db.execute(
            f"SELECT * FROM findings WHERE job_id = ? AND page = ? {order_clause} "
            f"LIMIT ? OFFSET ?",
            (job_id, page, limit, offset),
        )
    elif status:
        order_clause = f"ORDER BY page, {severity_order}, {source_order}, id"
        cursor = await db.execute(
            f"SELECT * FROM findings WHERE job_id = ? AND status = ? {order_clause} "
            f"LIMIT ? OFFSET ?",
            (job_id, status, limit, offset),
        )
    else:
        order_clause = f"ORDER BY page, {severity_order}, {source_order}, id"
        cursor = await db.execute(
            f"SELECT * FROM findings WHERE job_id = ? {order_clause} LIMIT ? OFFSET ?",
            (job_id, limit, offset),
        )
    rows = await cursor.fetchall()
    findings = [dict(r) for r in rows]

    # 页级完整性警告标记（置信度扣分信号）— 一次批量载入
    page_flags: dict[int, bool] = {}
    flag_cursor = await db.execute(
        "SELECT page, structured_json FROM page_cache WHERE job_id = ?",
        (job_id,),
    )
    for r in await flag_cursor.fetchall():
        flagged = False
        if r["structured_json"]:
            try:
                flagged = _page_is_flagged(json.loads(r["structured_json"]))
            except json.JSONDecodeError:
                pass
        page_flags[r["page"]] = flagged

    for f in findings:
        # M2/T2.4：优先用写入期已落库的 confidence（v10）；旧 job 该列为 NULL
        # 时按同一 core 实现现算兜底 —— 口径一致，历史数据功能不降级。
        stored = f.get("confidence")
        f["confidence"] = (
            float(stored) if stored is not None
            else _confidence_for(f, page_flags.get(f.get("page"), False))
        )

    if by_confidence:
        findings.sort(key=lambda x: (x["confidence"], x.get("id", 0)))
        total_est = len(findings)
        findings = findings[offset:offset + limit]
        total = total_est
    else:
        # 总数（用于前端显示 "x/y" + has_more 截断提示）。
        # 对抗审查：旧实现 count 只按 job_id 统计全局总数，而列表按
        # page/status 过滤 — 全局 >50 条时每个页面都显示"本页问题超过 50
        # 条"，与实际列表条数完全不符（如当前页仅 1 条的页 6 也提示超过
        # 50 条，GMP 复核误导）。total/has_more 必须按当前过滤集统计。
        if page:
            count_cursor = await db.execute(
                "SELECT COUNT(*) FROM findings WHERE job_id = ? AND page = ?",
                (job_id, page),
            )
        elif status:
            count_cursor = await db.execute(
                "SELECT COUNT(*) FROM findings WHERE job_id = ? AND status = ?",
                (job_id, status),
            )
        else:
            count_cursor = await db.execute(
                "SELECT COUNT(*) FROM findings WHERE job_id = ?", (job_id,)
            )
        total = (await count_cursor.fetchone())[0]

    # 三色复核分级（M6/T6.4）：与 SSR（main.py 复核页）共用同一 core 函数，
    # 前端只读 f.tier —— 避免"SSR 一套颜色、AJAX 又一套"的映射漂移。
    from core.finding_quality import attach_review_tier, tier_counts
    attach_review_tier(findings)
    tier_count = tier_counts(findings)

    return {
        "findings": findings,
        "count": len(findings),
        "total": total,
        "page": page,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < total,
        "tier_counts": tier_count,
    }


@router.get("/jobs/{job_id}/findings/{finding_id}")
async def get_finding(job_id: str, finding_id: int, request: Request = None):
    """Get a single finding detail."""
    # P2-1: GET 读端点守卫统一
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM findings WHERE id = ? AND job_id = ?", (finding_id, job_id)
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "问题记录不存在")
    # 与列表端点同源：单条详情也带三色分级（M6/T6.4）
    from core.finding_quality import attach_review_tier
    return attach_review_tier([dict(row)])[0]


@router.post("/jobs/{job_id}/findings/{finding_id}")
async def update_finding(
    job_id: str,
    finding_id: int,
    request: Request = None,
    status: Optional[str] = Form(default=None),
    reviewer_note: Optional[str] = Form(default=None),
    corrected_text: Optional[str] = Form(default=None),
):
    """Update a finding (confirm/reject/correct).

    Phase 3 fix: parameters use Form() because the review.html frontend posts
    application/x-www-form-urlencoded. Without Form(), FastAPI treats them as
    query params and returns 400 'No fields to update'.
    """
    # 对抗审查（cr-18）：Form 编码为 CORS 简单请求（无 preflight），
    # 恶意网页可跨站篡改 findings 状态/意见，污染 GMP 审计。与上传端点对齐。
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()

    # Validate status value
    valid_statuses = {"confirmed", "rejected", "corrected", "pending"}
    if status and status not in valid_statuses:
        logger.warning(f"[{job_id}] Invalid status update: finding={finding_id} status={status}")
        raise HTTPException(400, f"无效状态: {status}. 只能是 {valid_statuses} 之一")

    # GMP 数据卫生（对抗审查）：复核意见/修正文本无上限会推高 DB 体积，
    # 且多 MB 表单文本经 aiosqlite 单连接无缓冲写入易卡顿。上限 2000 字符。
    _MAX_NOTE_LEN = 2000
    for label, val in (("reviewer_note", reviewer_note), ("corrected_text", corrected_text)):
        if val is not None and len(val) > _MAX_NOTE_LEN:
            raise HTTPException(
                400,
                f"{label} 超过 {_MAX_NOTE_LEN} 字符上限 "
                f"(实际 {len(val)} 字符, 截断示例 {val[:_MAX_NOTE_LEN]!r}...)",
            )

    # Check finding exists
    cursor = await db.execute(
        "SELECT id, status FROM findings WHERE id = ? AND job_id = ?", (finding_id, job_id)
    )
    row = await cursor.fetchone()
    if not row:
        logger.warning(f"[{job_id}] Finding not found: {finding_id}")
        raise HTTPException(404, "问题记录不存在")

    old_status = row["status"]
    logger.info(
        f"[{job_id}] Finding update: id={finding_id} "
        f"{old_status}→{status or '(unchanged)'}"
        + (f" note={reviewer_note[:30]!r}" if reviewer_note else "")
        + (f" corrected={corrected_text[:30]!r}" if corrected_text else "")
    )

    sets = []
    params = []
    if status:
        sets.append("status = ?")
        params.append(status)
    if reviewer_note is not None:
        sets.append("reviewer_note = ?")
        params.append(reviewer_note)
    if corrected_text is not None:
        sets.append("corrected_text = ?")
        params.append(corrected_text)
    if status in ("confirmed", "rejected", "corrected"):
        sets.append("reviewed_at = datetime('now','localtime')")
    elif status == "pending":
        sets.append("reviewed_at = NULL")

    if not sets:
        raise HTTPException(400, "没有需要更新的字段")

    params.extend([finding_id, job_id])

    # P-ADV3 修复：UPDATE findings + INSERT audit_log + commit 必须在 db_lock
    # 内原子执行，与 pipeline 的并发写入共享同一锁，防止 aiosqlite 单连接
    # 上的事务边界被穿插。
    action_parts = []
    if status:
        action_parts.append(f"status: {old_status} → {status}")
    if reviewer_note is not None:
        action_parts.append(f"note: '{reviewer_note[:50]}'")
    if corrected_text is not None:
        action_parts.append(f"corrected: '{corrected_text[:50]}'")

    async with db_lock:
        await db.execute(
            f"UPDATE findings SET {', '.join(sets)} WHERE id = ? AND job_id = ?",
            params,
        )
        await db.execute(
            "INSERT INTO audit_log (job_id, finding_id, action, detail) VALUES (?, ?, ?, ?)",
            (job_id, finding_id, "finding_update", "; ".join(action_parts)),
        )
        await db.commit()
    logger.info(f"[{job_id}] Finding {finding_id} updated: {old_status} → {status or old_status}")
    return {"ok": True}


@router.post("/jobs/{job_id}/pages/{page}/exemption")
async def set_page_exemption(
    job_id: str,
    page: int,
    request: Request = None,
    reason: Optional[str] = Form(default=None),
    revoke: int = Form(default=0),
):
    """Set or revoke a manual OCR-integrity exemption for a page.

    门禁 2（docs/OCR_GOLDEN_CORPUS.md）：每页必须 integrity=ok 或带有人工
    确认的豁免及原因。复核者对照 PDF 原图确认该页可接受后，在此记录
    豁免原因 — 写入 page_cache.ocr_diagnostics.exemption，GMP 可追溯
    （audit_log 记录 action=ocr_exemption / ocr_exemption_revoke）。
    """
    # 与 finding 更新一致：is_local_request 防 CSRF。
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    _MAX_REASON_LEN = 500
    if not revoke:
        if not reason or not reason.strip():
            raise HTTPException(400, "豁免原因不能为空")
        if len(reason) > _MAX_REASON_LEN:
            raise HTTPException(
                400,
                f"豁免原因超过 {_MAX_REASON_LEN} 字符上限（实际 {len(reason)}）",
            )
    db = await get_db()
    cursor = await db.execute(
        "SELECT ocr_diagnostics FROM page_cache WHERE job_id = ? AND page = ?",
        (job_id, page),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "页面不存在")

    diag_str = row["ocr_diagnostics"] or "{}"
    try:
        diag = json.loads(diag_str) if isinstance(diag_str, str) else {}
    except json.JSONDecodeError:
        diag = {}
    if not isinstance(diag, dict):
        diag = {}

    if revoke:
        had = "exemption" in diag
        diag.pop("exemption", None)
        if not had:
            raise HTTPException(400, "该页面没有已记录的豁免")
    else:
        diag["exemption"] = {
            "reason": reason.strip()[: _MAX_REASON_LEN],
            "created_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        }
    async with db_lock:
        await db.execute(
            "UPDATE page_cache SET ocr_diagnostics = ? WHERE job_id = ? AND page = ?",
            (json.dumps(diag, ensure_ascii=False), job_id, page),
        )
        action = "ocr_exemption_revoke" if revoke else "ocr_exemption"
        detail = (
            f"page={page} 撤销豁免"
            if revoke
            else f"page={page} reason={reason.strip()[:100]!r}"
        )
        await db.execute(
            "INSERT INTO audit_log (job_id, action, detail) VALUES (?, ?, ?)",
            (job_id, action, detail),
        )
        await db.commit()
    logger.info(
        f"[{job_id}] Page exemption {'revoked' if revoke else 'set'}: page={page}"
    )
    return {"ok": True, "exemption": diag.get("exemption")}


@router.get("/jobs/{job_id}/audit")
async def get_audit_log(job_id: str, limit: int = 50, request: Request = None):
    """Get audit log entries for a job."""
    # P2-1: GET 读端点守卫统一
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM audit_log WHERE job_id = ? ORDER BY id DESC LIMIT ?",
        (job_id, limit),
    )
    rows = await cursor.fetchall()
    return {"entries": [dict(r) for r in rows], "count": len(rows)}


@router.get("/jobs/{job_id}/llm_audit")
async def get_llm_audit_log(job_id: str, limit: int = 100, request: Request = None):
    """Get LLM call audit log for a job (Phase 7 GMP traceability).

    Returns every LLM call made during this job's pipeline run, including
    provider / model / prompt_version / token usage / latency / success.
    Used to answer "which model version produced this finding?".
    """
    # P2-1: GET 读端点守卫统一
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM llm_call_audit WHERE job_id = ? ORDER BY id ASC LIMIT ?",
        (job_id, limit),
    )
    rows = await cursor.fetchall()
    return {"entries": [dict(r) for r in rows], "count": len(rows)}


@router.get("/jobs/{job_id}/pages/{page}")
async def get_page(job_id: str, page: int, request: Request = None):
    """Get raw OCR HTML and structured data for a page.

    全项目唯一单页数据端点（jobs.py 曾有一个路径冲突的 get_page_data
    死端点，已合并删除）。structured 返回完整 JSON，前端自行提取
    overall_confidence / _parse_error（review.js updatePageLevelUI）。
    """
    # P2-1: GET 读端点守卫统一
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    cursor = await db.execute(
        "SELECT raw_html, ocr_diagnostics, structured_json FROM page_cache WHERE job_id = ? AND page = ?",
        (job_id, page),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Page not found")
    # 对抗审查(cr-6): structured_json 可能非 JSON（旧版本/手动编辑），
    # 直接 json.loads 会让 review 页 500；与 jobs.get_page_data 一致降级。
    try:
        structured = json.loads(row["structured_json"]) if row["structured_json"] else None
    except json.JSONDecodeError:
        structured = None
    try:
        ocr_diagnostics = json.loads(row["ocr_diagnostics"]) if row["ocr_diagnostics"] else None
    except json.JSONDecodeError:
        ocr_diagnostics = None
    return {
        "job_id": job_id,
        "page": page,
        "raw_html": row["raw_html"],
        "ocr_diagnostics": ocr_diagnostics,
        "structured": structured,
    }


@router.get("/jobs/{job_id}/pages/{page}/measurements")
async def get_page_measurements(job_id: str, page: int, request: Request = None):
    """Return measurement matrix for rendering on review page (Phase 3).

    Extracts all step[].measurements[] from the page's structured_json so the
    review template can render a time × column table with in_spec cell colors.
    """
    # P2-1: GET 读端点守卫统一 — 守卫必须在 DB 查询之前（其余端点均前置；
    # 后置会让非本地请求先触发查询，且 404 先于 403 泄露页存在性）
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    cursor = await db.execute(
        "SELECT structured_json FROM page_cache WHERE job_id = ? AND page = ?",
        (job_id, page),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Page not found")
    # 对抗审查(cr-6): 同 get_page_data — 非 JSON 的 structured_json 降级为空。
    try:
        data = json.loads(row["structured_json"]) if row["structured_json"] else {}
    except json.JSONDecodeError:
        data = {}
    measurements = []
    column_set: dict[str, None] = {}  # ordered set of column names
    for step in data.get("steps", []) or []:
        for m in step.get("measurements", []) or []:
            values = m.get("values") or {}
            for col in values.keys():
                column_set.setdefault(col, None)
            measurements.append({
                "step_no": step.get("step_no"),
                "time": m.get("time"),
                "values": values,
            })
    return {
        "page": page,
        "columns": list(column_set.keys()),
        "measurements": measurements,
        "count": len(measurements),
    }
