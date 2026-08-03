"""Stage 3 — cross-page analysis.

Phase 2 rewrite: replaces the legacy "all-page mode year" rule with 5
deterministic rules (R1-R5) + 1 LLM fallback. See spike/phase2_design.md.

Rules:
- R1-a: per-step time_reversal (start > end)
- R1-b: cross-step time_reversal (start[i] < end[i-1])
- R2:   year_contradiction within same event_type (draft/production/review/...)
- R3:   param_out_of_spec (parse spec_range, judge cell-level)
- R4:   suspicious_date (year < 2000 or > current_year + 1)
- R5:   signature_time_anomaly (sign_time < op_time)

LLM fallback: params whose spec cannot be parsed by rules go to LLM.
"""
from __future__ import annotations

import html
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from llm.client import get_llm_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers: spec / time parsing
# ---------------------------------------------------------------------------


@dataclass
class SpecBounds:
    op: str  # between | lt | le | gt | ge
    low: Optional[float] = None
    high: Optional[float] = None


# Match 2015.01.27 / 2015-01-27 / 2015/01/27 / 2015.1.7 (with optional HH:MM).
# Year prefix (19|20) lets us detect suspiciously old years (e.g. 1990) so R4
# can flag them — restricting to 20xx would hide them.
_DATE_RE = re.compile(
    r"(?<!\d)"                                       # no leading digit (avoid matching inside long digit run)
    r"((?:19|20)\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})"  # year-mon-day
    r"(?:\s+(\d{1,2}):(\d{2}))?"                      # optional HH:MM
)
# Match Chinese date format: 2024年5月7日 / 2024年05月07日 (with optional HH:MM)
_CN_DATE_RE = re.compile(
    r"((?:19|20)\d{2})年(\d{1,2})月(\d{1,2})日"
    r"(?:\s*(\d{1,2})[时:](\d{2})分?)?"
)
# Match a standalone HH:MM
_TIME_ONLY_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
# Glued name+date like "庞明女署2027.01.17" — we already extract via _DATE_RE,
# but keep a fallback for cases without separator inside the date.
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
# OCR串扰: "2022/4/202205.07" -> take the trailing "2022.05.07".
# Allow optional separator between MM and DD (e.g. ".07" instead of "07").
_OCR_NOISE_DATE_RE = re.compile(
    r"(20\d{2})[/.\-]?(\d{1,2})?[/.\-](20\d{2})[.\-/]?(\d{2})[.\-/]?(\d{2})"
)


def _parse_time(s: Optional[str], fallback_date: Optional[str] = None) -> Optional[datetime]:
    """Parse a time string into datetime.

    Supported formats (in priority order):
      - "2015.01.27 14:30" / "2015-01-27 14:30" / "2015/1/7 9:05"
      - "2015.01.27"        / "2015-01-27"      / "2015/1/7"
      - "2024年5月7日" / "2024年05月07日 14时30分" (Chinese format)
      - "11:04"              -> uses fallback_date (e.g. page production_date)
      - "2022/4/202205.07"   -> OCR noise, clean to "2022.05.07"
      - "庞明女署2027.01.17"  -> extract via regex

    Returns None on parse failure (rule layer skips, does NOT raise).
    """
    if not s or not isinstance(s, str):
        return None
    raw = s.strip()
    if not raw:
        return None

    # 1) Standalone HH:MM
    m = _TIME_ONLY_RE.match(raw)
    if m and fallback_date:
        fb = _parse_time(fallback_date)
        if fb:
            return fb.replace(hour=int(m.group(1)), minute=int(m.group(2)))
        return None
    if m and not fallback_date:
        return None

    # 2) OCR noise pattern "2022/4/202205.07"
    m = _OCR_NOISE_DATE_RE.search(raw)
    if m:
        year = m.group(3)
        mon = m.group(4)
        day = m.group(5)
        try:
            return datetime(int(year), int(mon), int(day))
        except ValueError:
            pass

    # 3) Chinese date format "2024年5月7日" / "2024年05月07日 14时30分"
    m = _CN_DATE_RE.search(raw)
    if m:
        year, mon, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mm = int(m.group(5)) if m.group(5) else 0
        try:
            return datetime(year, mon, day, hh, mm)
        except ValueError:
            return None

    # 4) Normal date / datetime (YYYY.MM.DD / YYYY-MM-DD / YYYY/MM/DD)
    m = _DATE_RE.search(raw)
    if m:
        year, mon, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mm = int(m.group(5)) if m.group(5) else 0
        try:
            return datetime(year, mon, day, hh, mm)
        except ValueError:
            return None

    # 5) Bare year "2022"
    m = _YEAR_RE.search(raw)
    if m:
        try:
            return datetime(int(m.group(1)), 1, 1)
        except ValueError:
            return None

    return None


def _extract_year(s: Optional[str]) -> Optional[int]:
    """Extract just the year from a date-ish string."""
    dt = _parse_time(s)
    if dt:
        return dt.year
    m = _YEAR_RE.search(str(s or ""))
    if m:
        return int(m.group(1))
    return None


def _parse_number(s: Optional[str]) -> Optional[float]:
    """Extract the first number from a string, handle percentages and units.

    Examples: "0.974" -> 0.974 ; "53.6%" -> 53.6 ; "<0.3" -> 0.3 ; "17次" -> 17
    Supports power notation: "10^3" -> 1000, "10⁵" -> 100000
    """
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    text = str(s).strip()
    # Strip $ and {{ }} delimiters but keep content (e.g. "$0.974$" -> "0.974")
    text = re.sub(r"\$+", "", text)
    text = text.replace("{{", "").replace("}}", "")
    text = html.unescape(text)
    # Power notation: "10^3" / "10³" / "10⁵" (unicode super) -> 10**exp
    text = _expand_power_notation(text)
    m = re.search(r"-?\d+\.?\d*", text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


# Unicode superscript digits -> normal digits (for 10⁵ etc.)
_SUPERSCRIPT_MAP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def _expand_power_notation(text: str) -> str:
    """Expand '10^3' / '10³' / '10**3' to the literal numeric value.

    Only expands when the base is 10 (common in pharma specs like ≤10^3 cfu/g).
    Other bases (e.g. 2^5) are left untouched to avoid surprising semantics.
    """
    # 10^3 / 10**3 / 10^+3
    def repl_caret(m):
        base = float(m.group(1))
        exp = int(m.group(2))
        if base == 10:
            return str(int(base ** exp))
        return m.group(0)
    text = re.sub(r"\b(10)\s*\^+\s*(\d+)", repl_caret, text)
    # 10⁵ (unicode superscript, no caret)
    text = re.sub(r"\b(10)([⁰¹²³⁴⁵⁶⁷⁸⁹]+)", lambda m: str(10 ** int(m.group(2).translate(_SUPERSCRIPT_MAP))), text)
    return text


def _parse_spec(spec: Optional[str]) -> Optional[SpecBounds]:
    """Parse a spec_range string into SpecBounds.

    Returns None when the spec is non-numeric / free-text (e.g. "应澄清",
    "符合要求"), which means the rule layer cannot judge and the param goes
    to LLM fallback.

    Preprocessing:
      - html.unescape: &lt; -> <, &le; -> ≤, &gt; -> >, &ge; -> ≥
      - strip $...$ LaTeX残片
      - strip {{...}} template residue
      - 药典简写 NMT/NLT -> </>=
    """
    if not spec or not isinstance(spec, str):
        return None
    s = spec.strip()
    if not s:
        return None
    s = html.unescape(s)
    # Strip $...$ LaTeX delimiters but keep content (e.g. "$0.5-1.0$" -> "0.5-1.0")
    s = re.sub(r"\$+", "", s)
    # Strip {{...}} markers but keep content (e.g. "{{0.5-1.0}}" -> "0.5-1.0")
    s = s.replace("{{", "").replace("}}", "")
    s = s.replace("NMT", "<=").replace("NLT", ">=")
    s = s.replace("≤", "<=").replace("≥", ">=")
    # Expand power notation BEFORE numeric matching: "≤10^3cfu/g" -> "<=1000cfu/g"
    s = _expand_power_notation(s)
    s = s.strip(" .,;")

    # Numeric patterns are matched at start of string; trailing unit suffix
    # (e.g. " m^{3}/h", "MPa", "%") is allowed and ignored. Without this,
    # "0.5-1.0 $m^{3}/h$" would fail to match the between pattern.

    # between: "0.5-1.0" / "1300~3200" / "0.5–1.0" (en-dash)
    m = re.match(r"^(-?\d+\.?\d*)\s*[-~–]\s*(-?\d+\.?\d*)", s)
    if m:
        low, high = float(m.group(1)), float(m.group(2))
        return SpecBounds(op="between", low=low, high=high)

    # <0.3 / <=0.3 / < 0.3 / <=0.3MPa / <=1000cfu/g (after power expand)
    m = re.match(r"^<(=)?\s*(-?\d+\.?\d*)", s)
    if m:
        high = float(m.group(2))
        op = "le" if m.group(1) == "=" else "lt"
        return SpecBounds(op=op, low=None, high=high)

    # >5 / >=30 / ≥30% / >=30%
    m = re.match(r"^>(=)?\s*(-?\d+\.?\d*)", s)
    if m:
        low = float(m.group(2))
        op = "ge" if m.group(1) == "=" else "gt"
        return SpecBounds(op=op, low=low, high=None)

    # Chinese: 不超过100 / 不少于30 / 不超过10^3 (already expanded)
    m = re.match(r"^不超过\s*(-?\d+\.?\d*)", s)
    if m:
        return SpecBounds(op="le", low=None, high=float(m.group(1)))
    m = re.match(r"^不少于\s*(-?\d+\.?\d*)", s)
    if m:
        return SpecBounds(op="ge", low=float(m.group(1)), high=None)

    return None


def _judge(bounds: SpecBounds, actual: float) -> bool:
    if bounds.op == "between":
        return bounds.low <= actual <= bounds.high
    if bounds.op == "lt":
        return actual < bounds.high
    if bounds.op == "le":
        return actual <= bounds.high
    if bounds.op == "gt":
        return actual > bounds.low
    if bounds.op == "ge":
        return actual >= bounds.low
    return True


# ---------------------------------------------------------------------------
# Unit-aware comparison — avoids false positives when spec and actual use
# different but convertible units (e.g. spec="≤50%" vs actual="99ppm").
# ---------------------------------------------------------------------------

# Regex to extract trailing unit from a spec/value string (after the number).
_UNIT_RE = re.compile(r"-?\d+\.?\d*\s*([a-zA-Z%²³μµ/^\.]+[a-zA-Z%]*)")

# Known conversions: actual_unit -> {spec_unit: multiplier}
# actual_value_in_spec_unit = actual_value * multiplier
_UNIT_CONVERSIONS = {
    "ppm": {"%": 0.0001, "ppb": 1000},      # 1 ppm = 0.0001% = 1000 ppb
    "ppb": {"%": 0.0000001, "ppm": 0.001},  # 1 ppb = 0.0000001% = 0.001 ppm
    "%":  {"ppm": 10000, "ppb": 10000000},  # 1% = 10000 ppm = 1e7 ppb
}


def _extract_unit(text: Optional[str]) -> str:
    """Extract trailing alphabetic/% unit from a value/spec string.

    "99.0ppm" -> "ppm" ; "≤50%" -> "%" ; "0.974" -> "" ; "46.0bar" -> "bar"
    """
    if not text:
        return ""
    m = _UNIT_RE.search(str(text))
    return (m.group(1) if m else "").lower().strip(" .,;")


def _try_unit_normalize(actual_num: float, actual_unit: str, spec_str: str):
    """If actual and spec use convertible units, return (converted_actual, note).

    Returns (None, "") when no conversion is needed or possible.
    Caller should use original actual_num when converted is None.
    """
    spec_unit = _extract_unit(spec_str)
    au = (actual_unit or "").lower().strip(" .,;")
    su = spec_unit.lower().strip(" .,;")

    if not au or not su or au == su:
        return None, ""

    conv = _UNIT_CONVERSIONS.get(au)
    if conv and su in conv:
        converted = actual_num * conv[su]
        return converted, f"（已归一化 {actual_num}{au} → {converted}{su}）"
    return None, ""


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

    rule_findings: list[dict] = []
    llm_queue: list[dict] = []

    # R1-a + R1-b: time_reversal
    rule_findings.extend(_check_time_reversal_in_page(pages))
    rule_findings.extend(_check_time_reversal_cross_page(pages))
    # R2: year_contradiction (per event type)
    rule_findings.extend(_check_year_contradiction(pages))
    # R4: suspicious_date
    rule_findings.extend(_check_suspicious_dates(pages))
    # R5: signature_time_anomaly
    rule_findings.extend(_check_signature_time_anomaly(pages))
    # R6: completeness (missing operator/reviewer signatures)
    rule_findings.extend(_check_completeness(pages))
    # R3: param_out_of_spec (collects llm_queue as side effect)
    rule_findings.extend(_check_param_out_of_spec(pages, llm_queue))

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
    summary = _build_summary(pages)
    llm_findings = await _llm_based_check(summary, job_id=job_id)

    all_findings = rule_findings + llm_findings
    logger.info(
        f"Cross-page analysis: {len(rule_findings)} rule + {len(llm_findings)} LLM "
        f"({len(llm_fallback_findings)} from LLM fallback, "
        f"{len(per_page_findings)} from per-page LLM pass-through)"
    )
    return all_findings


# ---------------------------------------------------------------------------
# Page normalization
# ---------------------------------------------------------------------------


def _normalize_pages(page_structures: list[dict]) -> list[dict]:
    """Flatten raw DB rows into a uniform dict per page for rules."""
    out = []
    for ps in page_structures:
        data = ps.get("data") if isinstance(ps, dict) else None
        if not data or data.get("_parse_error"):
            continue
        out.append({
            "page": ps.get("page"),
            "data": data,
            "page_info": data.get("page_info", {}) or {},
            "steps": data.get("steps", []) or [],
            "findings": data.get("findings", []) or [],
            "event_year_groups": data.get("event_year_groups") or {},
        })
    return out


def _collect_per_page_findings(pages: list[dict]) -> list[dict]:
    """Pass through findings produced by per-page LLM (v3 prompt).

    Per-page LLM sometimes emits findings directly (e.g. page2 time_reversal
    when steps=[] and the time info didn't make it into structured fields).
    These findings are valuable — they are the only way we catch issues on
    pages where the LLM chose to emit a finding rather than structured data.

    We surface them in the cross-page output with corrected page number and
    source="llm_page" so the review page can distinguish them from rule
    findings.
    """
    out = []
    for page in pages:
        pno = page["page"]
        for f in page["findings"]:
            if not isinstance(f, dict):
                continue
            # Required fields check
            if not {"type", "severity", "description"}.issubset(f.keys()):
                continue
            new_f = dict(f)
            new_f["page"] = pno  # correct page number (LLM may set wrong)
            new_f.setdefault("ocr_text", "")
            new_f.setdefault("operator", "")
            new_f["source"] = "llm_page"
            out.append(new_f)
    return out


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
    for page in pages:
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
# R3: param_out_of_spec (rule-judgable only; rest go to llm_queue)
# ---------------------------------------------------------------------------


def _check_param_out_of_spec(pages: list[dict], llm_queue: list[dict]) -> list[dict]:
    findings = []
    for page in pages:
        pno = page["page"]
        for step in page["steps"]:
            step_no = step.get("step_no", "?")
            # single-value parameters
            for p in step.get("parameters", []) or []:
                _judge_param(p, pno, step_no, p.get("name", ""), findings, llm_queue)
            # matrix cells
            for m_idx, m in enumerate(step.get("measurements", []) or []):
                t = m.get("time", "")
                for col, val in (m.get("values") or {}).items():
                    _judge_cell(val, pno, step_no, col, t, findings, llm_queue)
    return findings


def _judge_param(p: dict, page: int, step_no, name: str,
                 findings: list[dict], llm_queue: list[dict]) -> None:
    actual = p.get("value")
    if actual is None or actual == "":
        return
    spec = p.get("spec_range")
    bounds = _parse_spec(spec)
    if bounds is None:
        llm_queue.append({
            "page": page, "step_no": step_no, "name": name,
            "spec": spec, "actual": actual, "unit": p.get("unit") or "",
            "kind": "param",
        })
        return
    actual_num = _parse_number(actual)
    if actual_num is None:
        llm_queue.append({
            "page": page, "step_no": step_no, "name": name,
            "spec": spec, "actual": actual, "unit": p.get("unit") or "",
            "kind": "param",
        })
        return
    # Unit-aware comparison: convert actual to spec's unit when possible
    actual_unit = _extract_unit(str(actual))
    converted, note = _try_unit_normalize(actual_num, actual_unit, spec)
    compare_num = converted if converted is not None else actual_num
    in_spec = _judge(bounds, compare_num)
    p["in_spec"] = in_spec
    if not in_spec:
        findings.append({
            "page": page,
            "type": "param_out_of_spec",
            "severity": "warning",
            "description": (
                f"第{page}页 参数 {name}={actual_num}{p.get('unit') or ''} "
                f"不在规格 {spec} 内{note}"
            ),
            "ocr_text": f"{name}: spec={spec} value={actual}",
            "operator": "",
            "source": "rule",
        })


def _judge_cell(val: dict, page: int, step_no, col: str, t: str,
               findings: list[dict], llm_queue: list[dict]) -> None:
    actual = val.get("actual")
    if actual is None or actual == "":
        return
    spec = val.get("spec")
    bounds = _parse_spec(spec)
    if bounds is None:
        llm_queue.append({
            "page": page, "step_no": step_no, "name": col, "time": t,
            "spec": spec, "actual": actual, "unit": val.get("unit") or "",
            "kind": "cell",
        })
        return
    actual_num = _parse_number(actual)
    if actual_num is None:
        llm_queue.append({
            "page": page, "step_no": step_no, "name": col, "time": t,
            "spec": spec, "actual": actual, "unit": val.get("unit") or "",
            "kind": "cell",
        })
        return
    # Unit-aware comparison: convert actual to spec's unit when possible
    actual_unit = _extract_unit(str(actual))
    converted, note = _try_unit_normalize(actual_num, actual_unit, spec)
    compare_num = converted if converted is not None else actual_num
    in_spec = _judge(bounds, compare_num)
    val["in_spec"] = in_spec
    if not in_spec:
        findings.append({
            "page": page,
            "type": "param_out_of_spec",
            "severity": "warning",
            "description": (
                f"第{page}页 {col} 在 {t} 时实测 {actual_num}{val.get('unit') or ''} "
                f"不在规格 {spec} 内{note}"
            ),
            "ocr_text": f"{t} {col}: spec={spec} actual={actual}",
            "operator": "",
            "source": "rule",
        })


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


# ---------------------------------------------------------------------------
# R6: completeness — missing operator/reviewer signatures on executed steps.
# A step with recorded start/end time but no operator signature is a GMP
# compliance gap (signature missing). Cover/toc pages legitimately lack
# signatures, so we only flag steps that have time data.
# ---------------------------------------------------------------------------


def _check_completeness(pages: list[dict]) -> list[dict]:
    findings = []
    seen: set[tuple] = set()  # dedup by (page, step_no, issue) — LLM may emit dup steps
    for page in pages:
        pno = page["page"]
        for step in page["steps"]:
            has_time = bool(step.get("start_time") or step.get("end_time"))
            if not has_time:
                continue  # cover/toc/heading steps without execution don't need sigs
            step_no = step.get("step_no", "?")
            sigs = step.get("signatures", []) or []
            has_operator = bool(step.get("operator")) or any(
                (s.get("role") or "").lower() in ("operator", "操作人", "操作员")
                for s in sigs
            )
            has_reviewer = bool(step.get("reviewer")) or any(
                (s.get("role") or "").lower() in ("reviewer", "复核人", "复核员", "workshop_reviewer")
                for s in sigs
            )
            if not has_operator:
                key = (pno, str(step_no), "operator")
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "warning",
                        "description": (
                            f"第{pno}页 工序{step_no} "
                            f"{(step.get('operation') or '')[:40]} "
                            f"已记录操作时间但缺少操作人签名"
                        ),
                        "ocr_text": f"step_no={step_no} operator=空",
                        "operator": "",
                        "source": "rule",
                    })
            if not has_reviewer:
                key = (pno, str(step_no), "reviewer")
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "info",
                        "description": (
                            f"第{pno}页 工序{step_no} "
                            f"{(step.get('operation') or '')[:40]} "
                            f"缺少复核人签名"
                        ),
                        "ocr_text": f"step_no={step_no} reviewer=空",
                        "operator": "",
                        "source": "rule",
                    })
    return findings


# ---------------------------------------------------------------------------
# LLM fallback for params rules could not judge
# ---------------------------------------------------------------------------


_FALLBACK_SYSTEM_PROMPT = """你是 GMP 批生产记录参数合规判定助手。
给定规则层无法判定的参数列表（spec 描述非数字范围，例如"应澄清"、"符合要求"），
请基于 GMP 常识判断 actual 是否符合 spec。

返回 JSON 数组，每个元素：
{"index":1,"in_spec":true|false|null,"reason":"..."}

- in_spec=true  表示合规
- in_spec=false 表示不合规
- in_spec=null  表示无法判定（需人工）

严格输出 JSON 数组，不要添加其他文本。"""


async def _llm_fallback_check(llm_queue: list[dict], *, job_id: str = "") -> list[dict]:
    if not llm_queue:
        return []
    items_text = "\n".join(
        f"{i+1}. 第{q['page']}页 工序{q.get('step_no','?')} "
        f"{q.get('time','') + ' ' if q.get('time') else ''}"
        f"{q['name']} | spec={q['spec']} | actual={q['actual']} | unit={q.get('unit','')}"
        for i, q in enumerate(llm_queue)
    )
    prompt = f"参数列表：\n{items_text}\n\n请逐条判定并返回 JSON 数组。"
    client = get_llm_client()
    try:
        result = await client.chat_json(
            _FALLBACK_SYSTEM_PROMPT, prompt, max_tokens=4000, temperature=0.1, timeout=120.0,
            audit_ctx={"job_id": job_id, "page": None, "stage": "cross_page_llm_fallback",
                       "prompt_version": "fallback_v1"},
        )
    except Exception as e:
        logger.warning(f"LLM fallback failed: {e}")
        return []
    if isinstance(result, dict) and result.get("_parse_error"):
        logger.warning("LLM fallback: JSON parse failure")
        return []
    items = result if isinstance(result, list) else result.get("items", [])
    findings = []
    for item in items:
        idx = int(item.get("index", 0)) - 1
        if idx < 0 or idx >= len(llm_queue):
            continue
        q = llm_queue[idx]
        in_spec = item.get("in_spec")
        if in_spec is False:
            findings.append({
                "page": q["page"],
                "type": "param_out_of_spec",
                "severity": "warning",
                "description": (
                    f"第{q['page']}页 参数 {q['name']}={q['actual']}{q.get('unit','')} "
                    f"不在规格 {q['spec']} 内（LLM 判定）"
                ),
                "ocr_text": f"{q['name']}: spec={q['spec']} actual={q['actual']}",
                "operator": "",
                "source": "llm_fallback",
            })
        elif in_spec is None:
            findings.append({
                "page": q["page"],
                "type": "completeness",
                "severity": "warning",
                "description": (
                    f"第{q['page']}页 参数 {q['name']} 规格描述模糊（spec={q['spec']}），"
                    f"需人工确认"
                ),
                "ocr_text": f"{q['name']}: spec={q['spec']} actual={q['actual']}",
                "operator": "",
                "source": "llm_fallback",
            })
    return findings


# ---------------------------------------------------------------------------
# LLM semantic check (catches what rules missed)
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """你是一个 GMP 批生产记录合规分析专家。
给定一份批生产记录所有页的结构化数据摘要（已由规则层跑过 R1-R5，
请只关注规则层无法判定的语义异常，例如签名一致性、批次逻辑、跨页参数漂移等。

输出 JSON 数组，每个 finding 包含：
{"page":页码,"type":"signature_mismatch|param_out_of_spec|completeness","severity":"critical|warning|info","description":"问题描述","ocr_text":"原文摘录","operator":"涉及人员"}"""


async def _llm_based_check(summary: str, *, job_id: str = "") -> list[dict]:
    if not summary.strip():
        return []
    client = get_llm_client()
    try:
        result = await client.chat_json(
            SYSTEM_PROMPT, summary, max_tokens=4000, temperature=0.1, timeout=180.0,
            audit_ctx={"job_id": job_id, "page": None, "stage": "cross_page_llm",
                       "prompt_version": "semantic_v1"},
        )
    except Exception as e:
        logger.warning(f"LLM semantic check failed: {e}")
        return []
    if isinstance(result, dict) and result.get("_parse_error"):
        logger.warning("Cross-page LLM analysis: JSON parse failure")
        return []
    findings = result if isinstance(result, list) else result.get("findings", [])
    valid = []
    required = {"page", "type", "severity", "description"}
    for f in findings:
        if not isinstance(f, dict) or not required.issubset(f.keys()):
            logger.warning(f"Skipping invalid finding (missing fields): {f}")
            continue
        f.setdefault("ocr_text", "")
        f.setdefault("operator", "")
        f["source"] = "llm_cross"
        valid.append(f)
    return valid


def _build_summary(pages: list[dict]) -> str:
    """Build compact text summary of all pages for LLM consumption."""
    lines = []
    for page in pages:
        pi = page["page_info"]
        lines.append(f"--- 第{page['page']}页 ---")
        if pi.get("title"):
            lines.append(f"  标题: {pi['title']}")
        if pi.get("production_date"):
            lines.append(f"  生产日期: {pi['production_date']}")
        if pi.get("batch_no"):
            lines.append(f"  批号: {pi['batch_no']}")
        eyg = page["event_year_groups"]
        if eyg:
            lines.append(f"  事件年份分组: {eyg}")
        for step in page["steps"]:
            line = f"  步骤{step.get('step_no','?')}: {step.get('operation','')[:60]}"
            if step.get("start_time"):
                line += f" | 开始:{step['start_time']}"
            if step.get("end_time"):
                line += f" | 结束:{step['end_time']}"
            if step.get("operator"):
                line += f" | 操作人:{step['operator']}"
            if step.get("reviewer"):
                line += f" | 复核人:{step['reviewer']}"
            for p in step.get("parameters", []) or []:
                if p.get("value"):
                    line += f" | {p.get('name','')}:{p['value']}{p.get('unit','')}"
            lines.append(line)
            for m in step.get("measurements", []) or []:
                t = m.get("time", "")
                cells = m.get("values") or {}
                first_keys = list(cells.keys())[:4]
                cells_str = ", ".join(
                    f"{k}={cells[k].get('actual','')}" for k in first_keys
                )
                lines.append(f"    @ {t} | {cells_str}{' ...' if len(cells) > 4 else ''}")
            for sig in step.get("signatures", []) or []:
                lines.append(
                    f"    签名: {sig.get('role','')}|{sig.get('name','')}|{sig.get('sign_time','')}"
                )
        if page["findings"]:
            lines.append(f"  已抓 findings ({len(page['findings'])} 条):")
            for f in page["findings"][:5]:
                lines.append(f"    - [{f.get('type','')}] {f.get('description','')[:80]}")
    return "\n".join(lines)
