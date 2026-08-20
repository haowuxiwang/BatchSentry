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
- llm_checks.py  : LLM fallback + semantic check + user-rule prompt assembly

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
    _check_suspicious_dates,
    _check_time_reversal_cross_page,
    _check_time_reversal_in_page,
    _check_year_contradiction,
)
from core.rules.parsing import SpecBounds  # re-export for shim/tests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


async def analyze_cross_page(page_structures: list[dict], job_id: str = "") -> list[dict]:
    """Analyze all pages and return findings list.

    Args:
        page_structures: list of {page, data} dicts from page_cache.
        job_id: passed through to LLM audit_ctx for GMP traceability.
    """
    if not page_structures:
        return []

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

    rule_findings: list[dict] = []
    llm_queue: list[dict] = []

    # R1-a + R1-b: time_reversal
    r1a = _check_time_reversal_in_page(pages)
    r1b = _check_time_reversal_cross_page(pages)
    rule_findings.extend(r1a)
    rule_findings.extend(r1b)
    logger.info(f"[{job_id}] R1 time_reversal: {len(r1a)} in-page + {len(r1b)} cross-page")
    # R2: year_contradiction (per event type)
    r2 = _check_year_contradiction(pages)
    rule_findings.extend(r2)
    logger.info(f"[{job_id}] R2 year_contradiction: {len(r2)}")
    # R4: suspicious_date
    r4 = _check_suspicious_dates(pages)
    rule_findings.extend(r4)
    logger.info(f"[{job_id}] R4 suspicious_date: {len(r4)}")
    # R5: signature_time_anomaly
    r5 = _check_signature_time_anomaly(pages)
    rule_findings.extend(r5)
    logger.info(f"[{job_id}] R5 signature_time_anomaly: {len(r5)}")
    # R9a: signature ORDER across roles (reviewer/QA must sign after operator)
    r9a = _check_signature_order(pages)
    rule_findings.extend(r9a)
    logger.info(f"[{job_id}] R9a signature_order: {len(r9a)}")
    # R6: completeness (missing operator/reviewer signatures)
    r6 = _check_completeness(pages)
    rule_findings.extend(r6)
    logger.info(f"[{job_id}] R6 completeness: {len(r6)}")
    # R7: batch number consistency across pages
    r7 = _check_batch_consistency(pages)
    rule_findings.extend(r7)
    logger.info(f"[{job_id}] R7 batch_consistency: {len(r7)}")
    # R8: low-confidence parameter values — flag for human review
    r8 = _check_low_confidence_params(pages)
    rule_findings.extend(r8)
    logger.info(f"[{job_id}] R8 low_confidence: {len(r8)}")
    # R9: handwritten notes — surface for manual verification
    r9 = _check_handwritten_notes(pages)
    rule_findings.extend(r9)
    logger.info(f"[{job_id}] R9 handwritten_notes: {len(r9)}")
    # R8b: check consistency (QA checkbox answered 否 / unrecognizable)
    r8b = _check_check_consistency(pages)
    rule_findings.extend(r8b)
    logger.info(f"[{job_id}] R8b check_consistency: {len(r8b)}")
    # R-M1: cross-page measurement time sequence (same step, monotonic rows)
    rm1 = _check_measurement_time_sequence(pages)
    rule_findings.extend(rm1)
    logger.info(f"[{job_id}] R-M1 measurement_time_sequence: {len(rm1)}")
    # R-M2: cross-page measurement column consistency (missing columns)
    rm2 = _check_measurement_column_consistency(pages)
    rule_findings.extend(rm2)
    logger.info(f"[{job_id}] R-M2 measurement_column_consistency: {len(rm2)}")
    # R3: param_out_of_spec (collects llm_queue as side effect)
    r3 = _check_param_out_of_spec(pages, llm_queue)
    rule_findings.extend(r3)
    logger.info(
        f"[{job_id}] R3 param_out_of_spec: {len(r3)} findings, "
        f"{len(llm_queue)} queued for LLM fallback"
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
    llm_fallback_findings = await _llm_fallback_check(llm_queue, job_id=job_id)
    rule_findings.extend(llm_fallback_findings)

    # LLM semantic check (catches what rules missed)
    # 用户规则每 job 只读一次 config.json，避免 _user_rules_section /
    # enabled_rule_ids 各读一遍（每次文件 IO + JSON 解析，51 页大文件时浪费）。
    from config import load_user_rules
    user_rules = [r for r in load_user_rules() if r.get("active")]
    summary = _build_summary(pages)
    llm_findings = await _llm_based_check(summary, job_id=job_id, user_rules=user_rules)

    all_findings = rule_findings + llm_findings
    logger.info(
        f"[{job_id}] Cross-page analysis done: {len(rule_findings)} rule + {len(llm_findings)} LLM "
        f"({len(llm_fallback_findings)} from LLM fallback, "
        f"{len(per_page_findings)} from per-page LLM pass-through) = {len(all_findings)} total"
    )
    return all_findings
