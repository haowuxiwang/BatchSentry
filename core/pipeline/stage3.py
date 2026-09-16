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
    dual_diff: list[dict] | None = None,
) -> None:
    """Cross-page analysis + findings persistence + final status. (refactor)

    dual_diff: 门禁 3 双后端对比差异列表（[{page, reason}, ...]，整份路径
    传入）。非空时逐页写入 completeness findings 并强制 partial_review —
    双后端结果不一致本身就是"需人工对照原图"的证据，不得自动通过。
    """
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
    empty_pages_count = 0
    # M2/T2.4：写入期置信度需要"该页是否带完整性告警"——与 api/review.py
    # 读取期判据共用 core.finding_quality.page_is_flagged（单一来源）。
    from core.finding_quality import page_is_flagged
    flagged_pages: set[int] = set()
    for row in await cursor.fetchall():
        if row["structured_json"]:
            try:
                data = json.loads(row["structured_json"])
                if page_is_flagged(data):
                    flagged_pages.add(row["page"])
                # Skip pages with parse errors
                if not data.get("_parse_error") and not data.get("_ocr_empty"):
                    page_structures.append({"page": row["page"], "data": data})
                else:
                    empty_pages_count += 1
            except json.JSONDecodeError:
                logger.warning(f"[{job_id}] Stage 3: Failed to parse page_cache page={row['page']}")
                empty_pages_count += 1

    # P-C3 修复：analyze_cross_page 调用前检查取消状态，
    # 避免取消后仍进入跨页分析（cancelling → review 非法转换）
    if await _run_is_cancelled(job_id):
        return

    # Stage 3 子进度上报（SSE"跨页分析"文案）— 规则校验/LLM 兜底/
    # LLM 语义三里程碑写入 ocr_progress.cross，结束后清除。
    from core.pipeline.state import _update_cross_progress

    async def _cross_progress_cb(done: int, total: int, label: str):
        await _update_cross_progress(job_id, done, total, label)

    findings = await _run_analyze_cross(
        page_structures, job_id=job_id, progress_cb=_cross_progress_cb
    )
    # R1 降噪：自指元噪声（"本页含手写内容"/"整体识别置信度较低"）是**工具可读性**
    # 提示而非记录缺陷，整份文档聚合为一条。先于 completeness 降噪执行 —— 两者
    # 治理对象不重叠（前者按文档属性聚合，后者按结构缺失 kind 细分）。
    from core.finding_noise import (
        reduce_completeness_noise,
        reduce_self_referential_noise,
    )
    findings, _self_ref_report = reduce_self_referential_noise(findings)
    # M2 降噪：completeness 结构性缺失的"抽取不确定降级 + 文档级聚合"。
    # 在 gmp_basis/kb_refs 富集**之前**执行 —— 只对精简后的集合做检索/映射，
    # 省掉数百条同质条目的无谓开销。审计可追溯（report 落 audit）。
    findings, _noise_report = reduce_completeness_noise(
        findings,
        flagged_pages=flagged_pages,
        total_pages=len(page_structures),
    )
    if _self_ref_report["aggregated_total"]:
        logger.info(
            f"[{job_id}] self-referential noise reduced: "
            f"aggregated={_self_ref_report['aggregated_total']} "
            f"({list(_self_ref_report['aggregated'])})"
        )
    if _noise_report["aggregated_total"] or \
            _noise_report["downgraded_extraction_uncertain"]:
        logger.info(
            f"[{job_id}] noise reduction: downgraded="
            f"{_noise_report['downgraded_extraction_uncertain']}, "
            f"aggregated={_noise_report['aggregated_total']} "
            f"({list(_noise_report['aggregated'])})"
        )
    # GMP 依据引用（v7）：按 type 映射法规依据（幂等；无映射不设键，
    # ocr_noise/user_rule 不映射 — user_rule 依据是其自身规则文本）
    from core.rules.gmp_basis import attach_gmp_basis
    attach_gmp_basis(findings)
    # 知识库条文引用（v8）：GMP2010 后置富集 —— 幂等、纯内存 BM25 检索，
    # 零 token 成本；命中则随行落库 findings.kb_refs（JSON）。
    from core.kb.retriever import attach_kb_refs
    import json as _json

    def _refs_json(f: dict):
        refs = f.get("kb_refs")
        if isinstance(refs, list) and refs:
            return _json.dumps(refs, ensure_ascii=False)
        return None

    attach_kb_refs(findings)
    await _update_cross_progress(job_id, 0, 0, "")
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
    await db.execute(
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
    # 降噪 N1（本轮）：规则层已覆盖的同 (page, type) 不再重复接受
    # llm_cross/llm_fallback 的语义重复报告 —— 同一问题在复核 UI 出现
    # rule+LLM 两三份是用户可见噪声的主要来源之一。rule 为权威版本；
    # 被抑制数量写入日志与审计（GMP 可追溯"为什么少了一条"）。
    # M2/T2.6：类型白名单 —— 归一后的 type 才是落库/去重/统计口径；原始 type
    # 仅在发生归一时留痕到 raw_type（GMP 可追溯 LLM 实际输出）。
    from core.finding_quality import confidence_for, normalize_finding_type

    def _norm(f: dict) -> str:
        return normalize_finding_type(f.get("type"))

    rule_covered: set[tuple] = {
        (f["page"], _norm(f)) for f in findings
        if f.get("source") == "rule"
    }
    suppressed_overlap = 0
    suppressed_detail: list[dict] = []
    batch_rows: list[tuple] = []
    for f in findings:
        # 跳过已在 Stage 2 写入的 page-level LLM findings
        if f.get("source") == "llm_page":
            skipped_llm_page += 1
            continue
        src = f.get("source", "rule")
        ftype = _norm(f)
        # user_rule 豁免：用户显式规则与规则层撞 (page,type) 是正常共存
        if src in ("llm_cross", "llm_fallback") and \
                (f["page"], ftype) in rule_covered:
            suppressed_overlap += 1
            # M2/T2.8：抑制可解释 —— 记录"哪条被谁覆盖"的明细（审计可回看）
            suppressed_detail.append({
                "page": f["page"], "type": ftype, "source": src,
                "description": (f.get("description") or "")[:120],
                "covered_by": "rule",
            })
            logger.debug(
                f"[{job_id}] overlap suppressed: p{f['page']} {ftype} "
                f"({src}) — rule already covers this page/type"
            )
            continue
        # robustness-B4: retry 会重新执行 Stage 3，确定性生成的 findings
        # 按 (job_id, source, page, type, description) 指纹去重。
        fingerprint = (
            job_id, f.get("source", "rule"), f["page"], ftype, f["description"],
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
            job_id, f["page"], ftype, f["severity"], f["description"],
            f.get("ocr_text"), f.get("operator"), f.get("source", "rule"),
            f.get("rule_id") if f.get("source") == "user_rule" else None,
            f.get("gmp_basis"), _refs_json(f),
            confidence_for(f, f["page"] in flagged_pages),
            f["type"] if ftype != f.get("type") else None,
        ))
        inserted += 1
    if batch_rows:
        # 对抗审查 T3.2：批量写 findings 与其他写事务（transition/audit/
        # 进度更新）串行化 —— 多 job 并行进入 Stage 3 时防止 executemany+
        # commit 与其他写穿插交错事务边界。
        async with db_lock:
            await db.executemany(
                "INSERT OR IGNORE INTO findings "
                "(job_id, page, type, severity, description, ocr_text, operator, source, user_rule_id, gmp_basis, kb_refs, confidence, raw_type, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))",
                batch_rows,
            )
            await db.commit()
    logger.info(
        f"[{job_id}] DB: findings inserted ({inserted} new + {skipped_llm_page} "
        f"llm_page skipped + {suppressed_overlap} LLM-overlap suppressed, "
        f"severity={severity_counts})"
    )
    if suppressed_overlap:
        # M2/T2.8 抑制可解释：除计数外落明细（哪页/哪类/被谁覆盖），
        # 让"为什么少了一条"在审计里可逐条回看（GMP 可追溯）。
        await _audit_log(
            db, job_id, "findings_overlap_suppressed",
            f"count={suppressed_overlap} — llm findings whose (page,type) "
            f"already covered by deterministic rule layer; "
            f"detail={json.dumps(suppressed_detail[:50], ensure_ascii=False)}",
        )

    # 门禁 3：双后端差异页逐页写入 completeness finding（复用 rule 链路的
    # 去重索引 / 复核 UI / 报告导出）。INSERT OR IGNORE + UNIQUE 指纹保证
    # retry 幂等。
    dual_diff = dual_diff or []
    if dual_diff:
        from core.rules.gmp_basis import GMP_BASIS_MAP

        dual_dicts = [
            {
                "page": d["page"], "type": "completeness", "severity": "warning",
                "description": (
                    f"第{d['page']}页 双后端 OCR 结果存在显著差异"
                    f"（{d['reason']}），请对照 PDF 原图人工核对"
                ),
                "ocr_text": f"dual_compare: primary vs secondary — {d['reason']}",
                "operator": "", "source": "rule",
                "gmp_basis": GMP_BASIS_MAP.get("completeness"),
            }
            for d in dual_diff
        ]
        attach_kb_refs(dual_dicts)
        dual_rows = [
            (
                job_id, x["page"], x["type"], x["severity"], x["description"],
                x["ocr_text"], x["operator"], x["source"], None,
                x.get("gmp_basis"), _refs_json(x),
                confidence_for(x, x["page"] in flagged_pages), None,
            )
            for x in dual_dicts
        ]
        async with db_lock:
            await db.executemany(
                "INSERT OR IGNORE INTO findings "
                "(job_id, page, type, severity, description, ocr_text, operator, source, user_rule_id, gmp_basis, kb_refs, confidence, raw_type, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))",
                dual_rows,
            )
            await db.commit()
        logger.info(
            f"[{job_id}] DB: dual-compare findings inserted ({len(dual_rows)} rows)"
        )

    # Determine final status
    total_cost_ms = int((time.time() - pipeline_start) * 1000)
    dual_diff = dual_diff or []
    # 门禁 3：双后端差异页 / 解析错误页强制人工复核
    # 空页（_ocr_empty）不强制 partial_review — 空页不是失败，只是无内容
    # （如封面/目录页），review UI 会显示 _ocr_empty 横幅供人工确认。
    final_status = (
        "partial_review" if (failed_pages or dual_diff) else "review"
    )

    await db.execute(
        "UPDATE jobs SET finished_at = datetime('now','localtime'), "
        "stage1_ms = ?, stage2_ms = ?, stage3_ms = ?, failed_pages = ? "
        "WHERE id = ?",
        (stage1_ms, stage2_ms, stage3_ms,
         json.dumps(failed_pages) if failed_pages else None, job_id),
    )
    status_detail = (
        f"流水线完成：{len(findings)} 条问题，{len(failed_pages)} 页失败"
        + (f"，双后端差异 {len(dual_diff)} 页" if dual_diff else "")
        + (f"，{empty_pages_count} 页内容为空/解析错误" if empty_pages_count else "")
    )
    await transition_status(db, job_id, final_status, status_detail)

    # 飞书通知（旁路：失败不影响主流程；成功/部分完成均推送）
    try:
        from core.notify import notify_job
        await notify_job(job_id, final_status)
    except Exception:
        pass  # notify_job 自身已兜底，此处双保险防异常逃逸

    logger.info(f"[{job_id}] Pipeline complete: status={final_status}, "
                 f"{len(findings)} findings, {len(failed_pages)} failed pages, "
                 f"{len(dual_diff)} dual-compare diffs, "
                 f"total={total_cost_ms}ms (OCR={stage1_ms} LLM={stage2_ms} Cross={stage3_ms})")
    await _audit_log(db, job_id, "pipeline_complete",
                     f"status={final_status} findings={len(findings)} "
                     f"failed={len(failed_pages)} total={total_cost_ms}ms")
