from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from core.rules.parsing import _extract_year, _parse_time

logger = logging.getLogger(__name__)



# ---------------------------------------------------------------------------
# R1-a: per-step time_reversal
# ---------------------------------------------------------------------------


def _check_time_reversal_in_page(pages: list[dict]) -> list[dict]:
    findings = []
    for page in pages:
        pno = page["page"]
        fb_date = page["page_info"].get("production_date")
        for step in page["steps"]:
            t_start = _parse_time(step.get("start_time"), fb_date)
            t_end = _parse_time(step.get("end_time"), fb_date)
            if t_start and t_end and t_start > t_end:
                findings.append({
                    "page": pno,
                    "type": "time_reversal",
                    "severity": "critical",
                    "description": (
                        f"第{pno}页 工序{step.get('step_no','?')} "
                        f"开始时间({step.get('start_time')}) 晚于结束时间({step.get('end_time')})"
                    ),
                    "ocr_text": f"{step.get('start_time')} → {step.get('end_time')}",
                    "operator": step.get("operator") or "",
                    "source": "rule",
                })
    return findings


# ---------------------------------------------------------------------------
# R1-b: cross-step time_reversal (across pages)
# ---------------------------------------------------------------------------


def _check_time_reversal_cross_page(pages: list[dict]) -> list[dict]:
    """R1-b: cross-step time_reversal across pages.

    Robustness improvements (avoid false positives):
    1. Sort steps by step_no within each page — pages may list steps out of
       numeric order, but the rule should compare in step order, not row order.
    2. Skip comparison when curr.step_no <= prev.step_no AND pages differ —
       this indicates an appendix/supplement re-listing an earlier step, not a
       real sequence violation.
    3. When start and end differ by >2 years on what should be the same batch,
       flag as extraction_error (warning) instead of time_reversal (critical):
       the LLM/OCR likely leaked the production_date year (e.g. 2015 vs 2025).
    """
    findings = []
    ordered = []
    unparseable_times: list[tuple[int, str, str, str]] = []  # (page, step_no, operation, raw_time)
    for page in pages:
        pno = page["page"]
        fb_date = page["page_info"].get("production_date")
        # Sort steps within page by step_no (numeric when possible) so the
        # cross-page sequence follows step order, not table row order.
        page_steps = list(page["steps"])
        try:
            page_steps.sort(key=lambda s: _step_sort_key(s.get("step_no")))
        except Exception:
            pass  # keep original order if sorting fails
        for step in page_steps:
            t_start = _parse_time(step.get("start_time"), fb_date)
            t_end = _parse_time(step.get("end_time"), fb_date)
            # Detect unparseable time strings (OCR errors) — flag for human review
            raw_start = step.get("start_time", "")
            raw_end = step.get("end_time", "")
            if (raw_start or raw_end) and not (t_start and t_end):
                unparseable_times.append((pno, str(step.get("step_no", "?")),
                                          (step.get("operation") or "")[:40],
                                          f"start={raw_start} end={raw_end}"))
            if t_start and t_end:
                ordered.append({
                    "page": page["page"],
                    "step_no": step.get("step_no"),
                    "step_key": _step_sort_key(step.get("step_no")),
                    "start_time": step.get("start_time"),
                    "end_time": step.get("end_time"),
                    "t_start": t_start,
                    "t_end": t_end,
                    "operator": step.get("operator") or "",
                })
    for i in range(1, len(ordered)):
        prev, curr = ordered[i - 1], ordered[i]
        # Skip when this looks like an appendix re-listing an earlier step:
        # different page, step number decreases or stays the same.
        if curr["page"] != prev["page"] and curr["step_key"] <= prev["step_key"]:
            continue
        # Skip duplicate step entries on the same page (same step_no split
        # across two rows — e.g. p11 工序6 appears twice). Comparing a step
        # against itself produces meaningless "start < end" findings.
        if curr["page"] == prev["page"] and curr["step_key"] == prev["step_key"]:
            continue
        if curr["t_start"] >= prev["t_end"]:
            continue
        # Year-mismatch detection: if the two timestamps differ by >2 years,
        # treat as extraction_error (warning) rather than time_reversal (critical).
        year_delta = abs(curr["t_start"].year - prev["t_end"].year)
        if year_delta > 2:
            findings.append({
                "page": curr["page"],
                "type": "time_reversal",
                "severity": "warning",
                "description": (
                    f"第{curr['page']}页 工序{curr['step_no']} 开始({curr['start_time']}) "
                    f"早于第{prev['page']}页 工序{prev['step_no']} 结束({prev['end_time']})，"
                    f"年份相差 {year_delta} 年，可能为 OCR/LLM 提取错误（原值 2015/2025 混淆等），请人工核对"
                ),
                "ocr_text": f"{curr['start_time']} < {prev['end_time']} (Δ{year_delta}y)",
                "operator": curr["operator"],
                "source": "rule",
            })
        else:
            findings.append({
                "page": curr["page"],
                "type": "time_reversal",
                "severity": "critical",
                "description": (
                    f"第{curr['page']}页 工序{curr['step_no']} 开始({curr['start_time']}) "
                    f"早于第{prev['page']}页 工序{prev['step_no']} 结束({prev['end_time']})"
                ),
                "ocr_text": f"{curr['start_time']} < {prev['end_time']}",
                "operator": curr["operator"],
                "source": "rule",
            })
    # Flag unparseable time strings for human review (OCR quality issue)
    for pno, step_no, op, raw in unparseable_times:
        findings.append({
            "page": pno,
            "type": "completeness",
            "severity": "info",
            "description": (
                f"第{pno}页 工序{step_no} {op} "
                f"时间格式无法解析（{raw}），请人工核对时间顺序"
            ),
            "ocr_text": raw,
            "operator": "",
            "source": "rule",
        })
    return findings


def _step_sort_key(step_no) -> float:
    """Convert step_no to a numeric sort key.

    Handles values like "1", "2", "附表1", "3.1" by extracting the leading
    number; non-numeric steps sort after all numeric ones.
    """
    if step_no is None:
        return 9999.0
    m = re.search(r"(\d+(?:\.\d+)?)", str(step_no))
    if m:
        return float(m.group(1))
    return 9999.0


# ---------------------------------------------------------------------------
# R2: year_contradiction within same event_type
# ---------------------------------------------------------------------------


def _check_year_contradiction(pages: list[dict]) -> list[dict]:
    findings = []
    for page in pages:
        eyg = page["event_year_groups"]
        if not eyg:
            continue
        pno = page["page"]
        for event_type in ("draft", "production", "review", "approval", "issue", "other"):
            years = eyg.get(event_type) or []
            # de-dup but preserve multi-year signal
            uniq = sorted({int(y) for y in years if y is not None})
            if len(uniq) <= 1:
                continue
            findings.append({
                "page": pno,
                "type": "year_contradiction",
                "severity": "warning",
                "description": (
                    f"第{pno}页 {event_type} 事件内年份不一致: {uniq}，需人工确认"
                ),
                "ocr_text": str(uniq),
                "operator": "",
                "source": "rule",
            })
    return findings


# ---------------------------------------------------------------------------
# R4: suspicious_date
# ---------------------------------------------------------------------------


def _check_suspicious_dates(pages: list[dict]) -> list[dict]:
    findings = []
    current_year = datetime.now().year
    max_year = current_year + 1
    for page in pages:
        pno = page["page"]
        seen: set[str] = set()
        for ds in _collect_all_date_strings(page):
            if ds in seen:
                continue
            seen.add(ds)
            year = _extract_year(ds)
            if year is None:
                continue
            if year < 2000 or year > max_year:
                findings.append({
                    "page": pno,
                    "type": "suspicious_date",
                    "severity": "warning",
                    "description": (
                        f"第{pno}页 日期 {ds} 年份 {year} 异常"
                        f"（早于2000或晚于{max_year}），需人工确认"
                    ),
                    "ocr_text": ds,
                    "operator": "",
                    "source": "rule",
                })
    return findings


def _collect_all_date_strings(page: dict) -> list[str]:
    out = []
    pi = page["page_info"]
    if pi.get("production_date"):
        out.append(str(pi["production_date"]))
    for step in page["steps"]:
        for k in ("start_time", "end_time"):
            if step.get(k):
                out.append(str(step[k]))
        for sig in step.get("signatures", []) or []:
            if sig.get("sign_time"):
                out.append(str(sig["sign_time"]))
    return out


# ---------------------------------------------------------------------------
# R5: signature_time_anomaly (sign_time < op_time only)
# ---------------------------------------------------------------------------


def _check_signature_time_anomaly(pages: list[dict]) -> list[dict]:
    findings = []
    for page in pages:
        pno = page["page"]
        fb_date = page["page_info"].get("production_date")
        for step in page["steps"]:
            op_time = _parse_time(step.get("start_time") or step.get("end_time"), fb_date)
            for sig in step.get("signatures", []) or []:
                st = sig.get("sign_time")
                if not st:
                    continue
                sig_time = _parse_time(st, fb_date)
                if sig_time is None or op_time is None:
                    continue
                if sig_time < op_time:
                    findings.append({
                        "page": pno,
                        "type": "signature_time_anomaly",
                        "severity": "warning",
                        "description": (
                            f"第{pno}页 {sig.get('role','')} {sig.get('name','')} "
                            f"签名时间 {st} 早于操作时间 "
                            f"{step.get('start_time') or step.get('end_time')}"
                        ),
                        "ocr_text": f"{sig.get('name','')} {st}",
                        "operator": sig.get("name") or "",
                        "source": "rule",
                    })
    return findings
