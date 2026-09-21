"""core.rules — Stage 3 cross-page rule engine (module refactor).

Split from the former 1700-line core/cross_page_analyzer.py into cohesive
rule-domain modules (2026-08):

- parsing.py     : spec/time/unit parsing primitives (SpecBounds, _parse_*)
- base.py        : page structure normalization utilities
- rule_time.py   : R1 time_reversal, R2 year_contradiction, R4 suspicious_date,
                    R5 signature_time_anomaly
- rule_spec.py   : R3 param_out_of_spec (cell + LLM-queue judgment)
- rule_doc.py    : R6 completeness, R7 batch_consistency, R8 low_confidence,
                    R9 handwritten_notes, R-M1/R-M2 measurement rules
- rule_gmp.py    : R11 mass_balance … R17 alteration (M4 GMP 运营合规)
- registry.py    : RuleSpec/RULE_REGISTRY — 规则元数据单一来源（M4/T4.0）
- llm_checks.py  : LLM fallback + semantic check + user-rule prompt assembly

M4 (T4.0): the orchestration below is **registry-driven** — rule execution
order, metadata (id/type/severity/basis) and per-rule enable/disable all come
from `registry.RULE_REGISTRY` / `enabled_rule_specs()`, so adding a rule is a
one-line registry append instead of editing this file's body.

core/cross_page_analyzer.py remains as a backward-compat shim.
"""
from __future__ import annotations

import logging

from core.rules.base import _collect_per_page_findings, _normalize_pages
from core.rules.llm_checks import (
    _build_summary,
    _llm_based_check,
    _llm_fallback_check,
)
from core.rules.rule_doc import (
    _check_batch_consistency,
    _check_check_consistency,
    _check_completeness,
    _check_handwritten_notes,
    _check_low_confidence_params,
    _check_measurement_column_consistency,
    _check_measurement_time_sequence,
)
from core.rules.rule_spec import _check_param_out_of_spec
from core.rules.rule_time import (
    _check_signature_order,
    _check_signature_time_anomaly,
    _check_step_number_gaps,
    _check_suspicious_dates,
    _check_time_reversal_cross_page,
    _check_time_reversal_in_page,
    _check_year_contradiction,
)
from core.rules.registry import RuleContext, enabled_rule_specs
from core.rules.parsing import SpecBounds as SpecBounds  # re-export for shim/tests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


async def analyze_cross_page(
    page_structures: list[dict], job_id: str = "", progress_cb=None
) -> list[dict]:
    """Analyze all pages and return findings list.

    Args:
        page_structures: list of {page, data} dicts from page_cache.
        job_id: passed through to LLM audit_ctx for GMP traceability.
        progress_cb: async (done, total, label) — Stage 3 子进度上报
            （SSE"跨页分析"文案），4 个里程碑：规则校验 / LLM 兜底 /
            LLM 语义 / 完成。None 时静默。

    Rule layer is driven by `registry.RULE_REGISTRY` (M4/T4.0): each enabled
    RuleSpec runs in registry order, its findings are logged under the rule id,
    and cross-rule signals (OOS pages) accumulate in the shared RuleContext.
    """
    if not page_structures:
        return []

    _CROSS_TOTAL = 3
    if progress_cb:
        try:
            await progress_cb(0, _CROSS_TOTAL, "规则校验")
        except Exception:
            pass

    pages = _normalize_pages(page_structures)
    logger.info(
        f"[{job_id}] Cross-page analysis start: {len(pages)} pages "
        f"(from {len(page_structures)} structures)"
    )
    # value_source 覆盖率统计（手写体存疑原则的可观测性）：LLM 语义标注
    # 优先，_backfill_value_source 按列名启发式兜底；覆盖率过低说明 OCR
    # 列名识别质量差，会影响降噪路径（边缘超差 → info）的可信度。
    n_param = 0
    n_param_vs = 0
    n_cell = 0
    n_cell_vs = 0
    for pg in pages:
        for s in pg["steps"]:
            for p in s.get("parameters") or []:
                n_param += 1
                if p.get("value_source"):
                    n_param_vs += 1
            for m in s.get("measurements") or []:
                for v in (m.get("values") or {}).values():
                    n_cell += 1
                    if v.get("value_source"):
                        n_cell_vs += 1
    if n_param or n_cell:
        logger.info(
            f"[{job_id}] value_source coverage: parameters {n_param_vs}/{n_param}, "
            f"cells {n_cell_vs}/{n_cell}"
        )
    # Round 7: OCR 结构化手写信号覆盖统计 — 携带 _ocr_low_conf_cols 的页数。
    # 信号页少（或全无）说明 OCR 未产出低置信度占位符 → value_source 依赖
    # LLM 标注/关键词兜底；信号页多则手写判定有机器事实支撑。
    n_sig_pages = sum(1 for pg in pages if (pg["data"].get("_ocr_low_conf_cols")))
    if n_sig_pages:
        logger.info(
            f"[{job_id}] OCR handwriting signal: {n_sig_pages}/{len(pages)} pages "
            f"carry low-confidence column/label tokens"
        )

    # ── 规则层：由注册表驱动（M4/T4.0）──────────────────────────────────
    ctx = RuleContext(job_id=job_id)
    rule_findings: list[dict] = []
    specs = enabled_rule_specs()
    for spec in specs:
        found = spec.check(pages, ctx)
        rule_findings.extend(found)
        ctx.record(found)
        logger.info(f"[{job_id}] {spec.id} {spec.type}: {len(found)}")
    logger.info(
        f"[{job_id}] rule layer: {len(rule_findings)} findings across "
        f"{len(specs)} rules, {len(ctx.llm_queue)} queued for LLM fallback"
    )

    # Per-page LLM findings pass-through:
    # When per-page LLM (v3) produces findings directly (e.g. page2 time_reversal
    # when steps=[]), surface them in the cross-page output so they reach the
    # review page. The rule layer and per-page LLM are complementary: rules run
    # on structured steps/measurements; per-page LLM catches what didn't make
    # it into structured fields. Correct page number to the actual page.
    per_page_findings = _collect_per_page_findings(pages)
    rule_findings.extend(per_page_findings)

    # LLM fallback: judge params that rules could not
    if progress_cb:
        try:
            await progress_cb(1, _CROSS_TOTAL, "LLM 兜底判定")
        except Exception:
            pass
    llm_fallback_findings = await _llm_fallback_check(ctx.llm_queue, job_id=job_id)
    rule_findings.extend(llm_fallback_findings)

    # LLM semantic check (catches what rules missed)
    # 用户规则每 job 只读一次 config.json，避免 _user_rules_section /
    # enabled_rule_ids 各读一遍（每次文件 IO + JSON 解析，51 页大文件时浪费）。
    from config import config as _app_config, load_user_rules
    user_rules = [r for r in load_user_rules() if r.get("active")]
    # Round 7: 跨页摘要按模型上下文窗口推导预算（config.app.llm_context_window，
    # LLM_CONTEXT_WINDOW env / config.json 顶层键；缺失时 llm_checks 用默认值）。
    summary = _build_summary(
        pages,
        context_window=getattr(_app_config["app"], "llm_context_window", None),
    )
    if progress_cb:
        try:
            await progress_cb(2, _CROSS_TOTAL, "LLM 语义分析")
        except Exception:
            pass
    llm_findings = await _llm_based_check(summary, job_id=job_id, user_rules=user_rules)

    # B1-1 同根因聚合（降噪）：单个 OCR 误读会在每个时间点各产 1 条 finding
    # （真实 p08「进料压力」7 个时间点 7 条同根因）。聚合**只对**携带派生键
    # （`finding_aggregate.AGG_KEY`，由判定点 `make_oos_finding` 挂上）的条目生效，
    # 其余逐字节原样通过。合并留痕：明细而非计数（见 aggregate_by_root_cause）。
    from core.rules.finding_aggregate import aggregate_by_root_cause
    n_before = len(rule_findings)
    rule_findings, merged_rows = aggregate_by_root_cause(rule_findings)
    if merged_rows:
        for row in merged_rows:
            logger.info(
                f"[{job_id}] B1-1 aggregated {row['count']} same-root-cause findings "
                f"→ 1: p{row['page']} {row['type']} {row['subject']} "
                f"(cause={row['cause']}, severities={row['severities']})"
            )
        logger.info(
            f"[{job_id}] B1-1 root-cause aggregation: {n_before} → {len(rule_findings)} "
            f"rule-layer findings ({len(merged_rows)} groups merged)"
        )

    all_findings = rule_findings + llm_findings
    if progress_cb:
        try:
            await progress_cb(_CROSS_TOTAL, _CROSS_TOTAL, "完成")
        except Exception:
            pass
    logger.info(
        f"[{job_id}] Cross-page analysis done: {len(rule_findings)} rule + {len(llm_findings)} LLM "
        f"({len(llm_fallback_findings)} from LLM fallback, "
        f"{len(per_page_findings)} from per-page LLM pass-through) = {len(all_findings)} total"
    )
    return all_findings
