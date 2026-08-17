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
import math
import re
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


# Match 2015.01.27 / 2015-01-27 / 2015/01/27 / 2015.1.7 (with optional HH:MM or HH:MM:SS).
# Year prefix (19|20) lets us detect suspiciously old years (e.g. 1990) so R4
# can flag them — restricting to 20xx would hide them.
_DATE_RE = re.compile(
    r"(?<!\d)"                                       # no leading digit (avoid matching inside long digit run)
    r"((?:19|20)\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})"  # year-mon-day
    r"(?:[\sT]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"      # optional HH:MM or HH:MM:SS (ISO T separator)
)
# Match Chinese date format: 2024年5月7日 / 2024年05月07日 (with optional HH:MM)
_CN_DATE_RE = re.compile(
    r"((?:19|20)\d{2})年(\d{1,2})月(\d{1,2})日"
    r"(?:\s*(\d{1,2})[时:](\d{2})分?)?"
)
# Match a standalone HH:MM or HH:MM:SS
_TIME_ONLY_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")
# Match MM-DD HH:MM (no year — needs fallback_date for year)
# Used when high-quality scan has "07-17 14:30" without year prefix
_MD_TIME_RE = re.compile(
    r"(?<!\d)(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$"
)
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
      - "2015.01.27 14:30:45" (with seconds)
      - "2015-01-27T14:30:00" (ISO 8601)
      - "2015.01.27"        / "2015-01-27"      / "2015/1/7"
      - "2024年5月7日" / "2024年05月07日 14时30分" (Chinese format)
      - "07-17 14:30"        -> uses fallback_date for year (no year prefix)
      - "11:04" / "11:04:30" -> uses fallback_date (e.g. page production_date)
      - "2022/4/202205.07"   -> OCR noise, clean to "2022.05.07"
      - "庞明女署2027.01.17"  -> extract via regex

    Returns None on parse failure (rule layer skips, does NOT raise).
    """
    if not s or not isinstance(s, str):
        return None
    raw = s.strip()
    if not raw:
        return None

    # 1) Standalone HH:MM or HH:MM:SS
    m = _TIME_ONLY_RE.match(raw)
    if m and fallback_date:
        fb = _parse_time(fallback_date)
        if fb:
            ss = int(m.group(3)) if m.group(3) else 0
            # 对抗审查 P1-5：_TIME_ONLY_RE 允许 "99:99" 类越界值（OCR 手写
            # 时间噪声高频出现），datetime.replace(hour=99) 抛 ValueError —
            # 本分支是全部 5 个分支中唯一漏掉 try/except 的，异常会传播出
            # 规则层直接炸掉整个 Stage 3（job error 而非 partial_review）。
            # docstring 承诺 "parse failure → None, does NOT raise"。
            try:
                return fb.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=ss)
            except ValueError:
                return None
        return None
    if m and not fallback_date:
        return None

    # 1b) MM-DD HH:MM (no year — needs fallback_date)
    m = _MD_TIME_RE.match(raw)
    if m and fallback_date:
        fb = _parse_time(fallback_date)
        if fb:
            try:
                ss = int(m.group(5)) if m.group(5) else 0
                return fb.replace(month=int(m.group(1)), day=int(m.group(2)),
                                  hour=int(m.group(3)), minute=int(m.group(4)), second=ss)
            except ValueError:
                return None
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

    # 4) Normal date / datetime (YYYY.MM.DD / YYYY-MM-DD / YYYY/MM/DD, optional HH:MM:SS)
    m = _DATE_RE.search(raw)
    if m:
        year, mon, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mm = int(m.group(5)) if m.group(5) else 0
        ss = int(m.group(6)) if m.group(6) else 0
        try:
            return datetime(year, mon, day, hh, mm, ss)
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
    """Expand scientific notation to literal numeric values.

    Supports:
    - "10^3" / "10³" → "1000"
    - "1.5×10^3" / "1.5*10^3" → "1500"
    - "1.5e3" / "1.5E3" → "1500"
    - "10**3" → "1000"

    Only expands when the base is 10 (common in pharma specs like ≤10^3 cfu/g).
    Other bases (e.g. 2^5) are left untouched to avoid surprising semantics.
    """
    # Step 1: Expand "10**3" / "10^3" / "10³" to literal (base must be 10)
    def repl_caret(m):
        base = float(m.group(1))
        exp = int(m.group(2))
        if base == 10:
            return str(int(base ** exp))
        return m.group(0)
    text = re.sub(r"\b(10)\s*(?:\*\*|\^)+\s*(\d+)", repl_caret, text)
    # 10⁵ (unicode superscript, no caret) — must run before generic digit
    # translate to avoid "10³" collapsing to "103" instead of "1000".
    text = re.sub(r"\b(10)([⁰¹²³⁴⁵⁶⁷⁸⁹]+)",
                  lambda m: str(10 ** int(m.group(2).translate(_SUPERSCRIPT_MAP))), text)

    # Step 2: Expand "系数×10^指数" or "系数*10^指数" patterns.
    # Matches "1.5×1000" or "1.5*1000" (after step 1 expanded 10^3 to 1000).
    def repl_sci(m):
        coeff = float(m.group(1))
        base = float(m.group(2))
        # Only handle if base is a power of 10 (10, 100, 1000, 10000, ...)
        if base != int(base) or base <= 0:
            return m.group(0)
        exp = math.log10(base)
        if exp != int(exp):
            return m.group(0)
        result = coeff * base
        # Return as int if whole number, else float
        return str(int(result)) if result == int(result) else str(result)
    text = re.sub(r"(\d+\.?\d*)\s*[×*]\s*(\d+\.?\d*)", repl_sci, text)

    # Step 3: Expand "1.5e3" / "1.5E3" notation
    def repl_e(m):
        mantissa = float(m.group(1))
        exp = int(m.group(2))
        result = mantissa * (10 ** exp)
        return str(int(result)) if result == int(result) else str(result)
    text = re.sub(r"(\d+\.?\d*)[eE]([-+]?\d+)", repl_e, text)

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
    # Fail-closed: unknown ops are treated as non-compliant to trigger
    # human review (GMP safety principle — never silently pass unknown rules).
    return False


# ---------------------------------------------------------------------------
# Unit-aware comparison — avoids false positives when spec and actual use
# different but convertible units (e.g. spec="≤50%" vs actual="99ppm").
# ---------------------------------------------------------------------------

# Regex to extract trailing unit from a spec/value string (after the number).
# First char must be a letter or % (not / or .) so we don't capture standalone
# separators; subsequent chars may include / for compound units (mg/L, mg/mL).
_UNIT_RE = re.compile(r"-?\d+\.?\d*\s*([a-zA-Z%²³μµ][a-zA-Z%²³μµ/^\.]*)")

# Known conversions: actual_unit -> {spec_unit: multiplier}
# actual_value_in_spec_unit = actual_value * multiplier
# All multipliers express "1 actual_unit = N spec_unit"
_UNIT_CONVERSIONS = {
    # === 浓度类 ===
    "ppm": {"%": 0.0001, "ppb": 1000},
    "ppb": {"%": 0.0000001, "ppm": 0.001},
    "%":  {"ppm": 10000, "ppb": 10000000},

    # === 质量类（制药常用）===
    # 1 g = 1000 mg = 1000000 µg = 0.001 kg
    "g":  {"mg": 1000, "µg": 1000000, "ug": 1000000, "kg": 0.001},
    "mg": {"g": 0.001, "µg": 1000, "ug": 1000, "kg": 0.000001},
    "µg": {"mg": 0.001, "g": 0.000001, "ug": 1},
    "ug": {"mg": 0.001, "g": 0.000001, "µg": 1},
    "kg": {"g": 1000, "mg": 1000000},

    # === 体积类（制药常用）===
    # 1 L = 1000 mL
    "l":  {"ml": 1000},
    "ml": {"l": 0.001},

    # === 浓度（质量/体积）===
    # mg/L = mg/L ；mg/mL = g/L （1 mg/mL = 1 g/L = 1000 mg/L）
    "mg/l":   {"mg/ml": 0.001, "g/l": 0.001},
    "mg/ml":  {"mg/l": 1000, "g/l": 1},
    "g/l":    {"mg/l": 1000, "mg/ml": 1},

    # === 压力类 ===
    # 1 MPa = 10 bar = 1000 kPa
    "mpa": {"bar": 10, "kpa": 1000},
    "bar": {"mpa": 0.1, "kpa": 100},
    "kpa": {"mpa": 0.001, "bar": 0.01},
}


def _extract_unit(text: Optional[str]) -> str:
    """Extract trailing unit from a value/spec string.

    "99.0ppm" -> "ppm" ; "≤50%" -> "%" ; "0.974" -> "" ; "46.0bar" -> "bar"
    "1.5mg/L" -> "mg/l" ; "100mg/mL" -> "mg/ml"
    """
    if not text:
        return ""
    m = _UNIT_RE.search(str(text))
    return (m.group(1) if m else "").lower().strip(" .,;")


def _try_unit_normalize(actual_num: float, actual_unit: str, spec_str: str):
    """If actual and spec use convertible units, return (converted_actual, note).

    If units differ but no conversion exists, return (None, "unit_mismatch") to
    trigger fail-closed behavior (force human review — never silently compare
    incompatible units, which is a GMP safety risk).
    """
    au = (actual_unit or "").lower().strip(" .,;")
    if not au:
        return actual_num, ""  # no unit on actual — compare as-is
    spec_unit = _extract_unit(spec_str)
    if not spec_unit or spec_unit == au:
        return actual_num, ""  # same unit or no spec unit
    conv = _UNIT_CONVERSIONS.get(au, {})
    if spec_unit in conv:
        converted = actual_num * conv[spec_unit]
        return converted, f"unit normalized: {actual_num}{au} → {converted}{spec_unit}"
    # Units differ but no conversion available — fail-closed
    logger.warning(
        f"Unit mismatch without conversion rule: "
        f"actual={actual_num}{au} vs spec={spec_str} "
        f"(flagging for human review — GMP safety)"
    )
    return None, "unit_mismatch"


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


# ---------------------------------------------------------------------------
# Page normalization
# ---------------------------------------------------------------------------


def _only_dicts(lst) -> list[dict]:
    """Keep only dict elements — Stage 3 rules call .get() on every element,
    so non-dict entries (type-polluted LLM output) would crash the whole job.
    P1-2 second line of defense (page_analyzer._sanitize_page_result is the
    source-side filter; this covers legacy page_cache rows written before it)."""
    return [x for x in lst if isinstance(x, dict)]


def _sanitize_year_groups(eyg):
    """Keep only year-group entries that are lists of int/parseable-int values.

    R2 (_check_year_contradiction) calls int(y) directly; a non-numeric string
    like "2022年" would raise ValueError and kill Stage 3. Non-conformant
    entries are emptied (fail-closed: lose the signal, never crash the job).
    """
    if not isinstance(eyg, dict):
        return {}
    clean = {}
    for k, v in eyg.items():
        if not isinstance(v, list):
            clean[k] = []
            continue
        years = []
        for y in v:
            if isinstance(y, bool):
                continue
            if isinstance(y, int):
                years.append(y)
            elif isinstance(y, str):
                try:
                    int(y)
                except ValueError:
                    continue
                years.append(y)
        clean[k] = years
    return clean


def _normalize_pages(page_structures: list[dict]) -> list[dict]:
    """Flatten raw DB rows into a uniform dict per page for rules."""
    out = []
    for ps in page_structures:
        data = ps.get("data") if isinstance(ps, dict) else None
        if not data or data.get("_parse_error"):
            continue
        # P1-2 兜底: 过滤 steps/findings 顶层非 dict 元素；对每个 step 再
        # 过滤 parameters/measurements/signatures 子元素（规则层对每个
        # 元素直接调 .get()，字符串元素会 AttributeError 崩掉整个 Stage 3）。
        steps = []
        for s in data.get("steps", []) or []:
            if not isinstance(s, dict):
                continue
            for field in ("parameters", "measurements", "signatures"):
                if field in s and isinstance(s[field], list):
                    s[field] = [x for x in s[field] if isinstance(x, dict)]
            # P1-2 兜底(标量): 规则层对 step 标量字段 .lower()/[:40] 切片，
            # truthy 非 str（如 operation:123）会 TypeError 崩掉 Stage 3。
            for field in ("operation", "operator", "reviewer", "start_time", "end_time"):
                v = s.get(field)
                if v is not None and not isinstance(v, str):
                    s[field] = str(v)
            for sig in s.get("signatures", []) or []:
                for field in ("role", "name"):
                    v = sig.get(field)
                    if v is not None and not isinstance(v, str):
                        sig[field] = str(v)
            steps.append(s)
        # P1-2 兜底(标量): page_info 非 dict → {}；overall_confidence 非 str → str()
        page_info = data.get("page_info")
        if page_info is not None and not isinstance(page_info, dict):
            data["page_info"] = {}
        if "overall_confidence" in data and data["overall_confidence"] is not None \
                and not isinstance(data["overall_confidence"], str):
            data["overall_confidence"] = str(data["overall_confidence"])
        out.append({
            "page": ps.get("page"),
            "data": data,
            "page_info": data.get("page_info", {}) or {},
            "steps": steps,
            "findings": _only_dicts(data.get("findings", []) or []),
            "event_year_groups": _sanitize_year_groups(data.get("event_year_groups")),
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
    if converted is None and note == "unit_mismatch":
        # Units differ but no conversion available — fail-closed, human review
        findings.append({
            "page": page,
            "type": "completeness",
            "severity": "warning",
            "description": (
                f"第{page}页 参数 {name} 单位不一致且无换算规则"
                f"（spec={spec}, actual={actual}），需人工确认"
            ),
            "ocr_text": f"{name}: spec={spec} value={actual}",
            "operator": "",
            "source": "rule",
        })
        return
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
    if converted is None and note == "unit_mismatch":
        # Units differ but no conversion available — fail-closed, human review
        findings.append({
            "page": page,
            "type": "completeness",
            "severity": "warning",
            "description": (
                f"第{page}页 {col} 在 {t} 时实测值单位不一致且无换算规则"
                f"（spec={spec}, actual={actual}），需人工确认"
            ),
            "ocr_text": f"{t} {col}: spec={spec} actual={actual}",
            "operator": "",
            "source": "rule",
        })
        return
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
            has_operation = bool(step.get("operation"))
            if not has_time:
                # Skip cover/toc/heading steps without operation description
                if not has_operation:
                    continue
                # Steps with operation but no time — flag as GMP gap
                # (missing date means execution time cannot be traced)
                step_no = step.get("step_no", "?")
                key = (pno, str(step_no), "no_time")
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "warning",
                        "description": (
                            f"第{pno}页 工序{step_no} "
                            f"{(step.get('operation') or '')[:40]} "
                            f"有操作描述但缺少执行时间记录"
                        ),
                        "ocr_text": f"step_no={step_no} time=空",
                        "operator": "",
                        "source": "rule",
                    })
                continue
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
            has_qa = any(
                (s.get("role") or "").lower() in ("qa", "qa_reviewer", "质量保证", "qa审核", "批准人", "放行人")
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
            if not has_qa:
                # QA signature required for GMP compliance on executed steps
                key = (pno, str(step_no), "qa")
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "info",
                        "description": (
                            f"第{pno}页 工序{step_no} "
                            f"{(step.get('operation') or '')[:40]} "
                            f"缺少 QA 签名"
                        ),
                        "ocr_text": f"step_no={step_no} qa=空",
                        "operator": "",
                        "source": "rule",
                    })
    return findings


# ---------------------------------------------------------------------------
# R9: handwritten notes — OCR of handwritten fields is unreliable, so surface
# steps with handwritten content for manual verification against the PDF page
# image. Info-level only: never blocks, but tells the reviewer where to look.
# ---------------------------------------------------------------------------


def _check_handwritten_notes(pages: list[dict]) -> list[dict]:
    findings = []
    seen: set[tuple] = set()  # dedup by (page, step_no, first note)
    for page in pages:
        pno = page["page"]
        for step in page["steps"]:
            notes = [str(n).strip() for n in (step.get("handwritten") or []) if str(n).strip()]
            if not notes:
                continue
            step_no = step.get("step_no", "?")
            key = (pno, str(step_no), notes[0])
            if key in seen:
                continue
            seen.add(key)
            preview = notes[0][:30]
            extra = len(notes) - 1
            findings.append({
                "page": pno,
                "type": "handwritten",
                "severity": "info",
                "description": (
                    f"第{pno}页 工序{step_no} 含 {len(notes)} 条手写内容"
                    f"（如「{preview}」{f' 等 {extra} 条' if extra else ''}）"
                    f"，手写体 OCR 易误读，请对照 PDF 原页人工核对"
                ),
                "ocr_text": "；".join(notes)[:200],
                "operator": "",
                "source": "rule",
            })
    return findings


# ---------------------------------------------------------------------------
# R7: batch number consistency — all pages should share the same batch_no.
# Mixed batch numbers across pages indicate binding errors or cross-batch
# contamination (serious GMP deviation).
# ---------------------------------------------------------------------------


def _check_batch_consistency(pages: list[dict]) -> list[dict]:
    """Check that all pages with a batch_no use the same value.
    Pages without batch_no (cover, toc, appendix) are skipped."""
    findings = []
    # Collect (page, batch_no) pairs for pages that have a batch_no
    batch_pages: dict[str, list[int]] = {}  # batch_no -> [page numbers]
    for page in pages:
        pno = page["page"]
        # batch_no 由 _normalize_pages 嵌套在 page_info 中（生产 schema）
        bno = ((page.get("page_info") or {}).get("batch_no") or "").strip()
        if not bno:
            continue
        batch_pages.setdefault(bno, []).append(pno)

    if len(batch_pages) <= 1:
        return findings  # all same (or none) — consistent

    # Multiple different batch numbers found
    summary_parts = [f"{bno}(第{','.join(str(p) for p in pn)}页)" for bno, pn in batch_pages.items()]
    findings.append({
        "page": min(min(pn) for pn in batch_pages.values()),  # first page with batch_no
        "type": "batch_inconsistency",
        "severity": "critical",
        "description": (
            f"跨页批号不一致：检测到 {len(batch_pages)} 个不同批号 — "
            f"{'；'.join(summary_parts[:3])}"
            f"{'…' if len(summary_parts) > 3 else ''}，"
            f"请核对是否装订错误或混批"
        ),
        "ocr_text": f"batch_nos={list(batch_pages.keys())}",
        "operator": "",
        "source": "rule",
    })
    logger.warning(
        f"R7 batch inconsistency: {len(batch_pages)} different batch numbers found"
    )
    return findings


# ---------------------------------------------------------------------------
# R8: low-confidence parameter values — flag for human review.
# OCR may misread handwritten values (e.g. "0.974" → "0.914"). Low confidence
# values that pass spec check could mask a real out-of-spec result. We flag
# them for human verification rather than silently trusting the OCR output.
# ---------------------------------------------------------------------------


def _check_low_confidence_params(pages: list[dict]) -> list[dict]:
    """Flag parameter values with low OCR confidence for human review.

    A low-confidence value that passes spec check might actually be
    out-of-spec if the OCR misread a handwritten digit. We surface these
    cases so reviewers can verify against the original document.
    """
    findings = []
    seen: set[tuple] = set()
    for page in pages:
        pno = page["page"]
        # overall_confidence is emitted by the per-page LLM (v3 prompt). In
        # _normalize_pages it stays nested under page["data"]; unit tests
        # pass it at the top level. Check both locations so the rule fires
        # in production and in tests.
        overall_conf = (
            page.get("overall_confidence")
            or (page.get("data") or {}).get("overall_confidence")
            or ""
        ).lower()
        # If overall page confidence is low, flag the whole page
        if overall_conf == "low":
            key = (pno, "page_low_confidence")
            if key not in seen:
                seen.add(key)
                findings.append({
                    "page": pno,
                    "type": "completeness",
                    "severity": "info",
                    "description": (
                        f"第{pno}页 整体识别置信度较低，"
                        f"建议人工核对 OCR 结果（可能存在手写体或印章干扰）"
                    ),
                    "ocr_text": f"overall_confidence={overall_conf}",
                    "operator": "",
                    "source": "rule",
                })
    return findings


def _check_measurement_time_sequence(pages: list[dict]) -> list[dict]:
    """R-M1: 同一工序（step_no）的跨页测量时间序列应单调不减。

    批记录的参数矩阵常跨页（大表在 51 页实测文件中跨页出现），R1-a/b 只
    比较 step.start_time/end_time，逐行测量时间（measurements[].time）从未
    被校验——缺行/行序错乱/时间倒序会静默通过。此规则把同 step_no 的所有
    页测量行按 (页序, 行序) 拼接后检查严格倒序（time_prev > time_curr）。
    相等的重复测量（同一分钟多次取样）合理，不报告；解析失败点打断连续
    性（避免把跨无效时间的行误判为倒序）。
    """
    findings: list[dict] = []
    groups: dict[str, list[dict]] = {}
    for page in pages:
        pno = page["page"]
        fb_date = page["page_info"].get("production_date")
        for step in page["steps"]:
            key = str(step.get("step_no", "?"))
            for m in step.get("measurements", []) or []:
                if not isinstance(m, dict):
                    continue
                groups.setdefault(key, []).append({
                    "page": pno,
                    "time_raw": m.get("time"),
                    "t": _parse_time(m.get("time"), fb_date),
                })
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        prev: dict | None = None
        for row in rows:
            if row["t"] is None:
                prev = None  # 解析失败点打断连续性，避免误报
                continue
            if prev is not None and prev["t"] > row["t"]:
                findings.append({
                    "page": row["page"],
                    "type": "time_reversal",
                    "severity": "warning",
                    "description": (
                        f"工序{key} 测量时间跨页倒序：第{prev['page']}页 "
                        f"{prev['time_raw']} 晚于 第{row['page']}页 {row['time_raw']}"
                        f" — 可能表格缺行、行序错乱或 OCR 提取错误，请人工核对参数矩阵"
                    ),
                    "ocr_text": f"{prev['time_raw']} > {row['time_raw']}",
                    "operator": "",
                    "source": "rule",
                })
            prev = row
    return findings


def _check_measurement_column_consistency(pages: list[dict]) -> list[dict]:
    """R-M2: 同一工序（step_no）跨页参数矩阵列集合应一致。

    大表跨页时每页表头重印；若某页测量行 values 键缺失（整列丢失），
    R3 的逐格判定无法发现"整列缺失"（它只看有值的格）。此规则只报
    "缺列"（多出来的列可能是新增参数，不误报），且要求该 step 跨 ≥2 页
    且有 ≥2 行数据，避免小样本噪音。
    """
    findings: list[dict] = []
    groups: dict[str, dict] = {}
    for page in pages:
        pno = page["page"]
        for step in page["steps"]:
            key = str(step.get("step_no", "?"))
            g = groups.setdefault(key, {"pages": set(), "rows": 0, "page_cols": {}})
            rows = step.get("measurements", []) or []
            g["rows"] += len(rows)
            cols: set[str] = set()
            for m in rows:
                if isinstance(m, dict) and isinstance(m.get("values"), dict):
                    cols.update(m["values"].keys())
            if cols:
                g["pages"].add(pno)
                g["page_cols"][pno] = cols
    for key, g in groups.items():
        if len(g["pages"]) < 2 or g["rows"] < 2:
            continue
        all_cols: set[str] = set()
        for cols in g["page_cols"].values():
            all_cols.update(cols)
        for pno in sorted(g["page_cols"]):
            missing = all_cols - g["page_cols"][pno]
            if not missing:
                continue
            missing_sorted = sorted(missing)
            findings.append({
                "page": pno,
                "type": "completeness",
                "severity": "info",
                "description": (
                    f"工序{key} 参数矩阵跨页列不一致：第{pno}页缺列 "
                    f"{'、'.join(missing_sorted)}（其余页均有）— "
                    f"可能表格截断或提取丢失，请人工核对"
                ),
                "ocr_text": "missing: " + "、".join(missing_sorted),
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


def _flag_llm_queue_for_review(llm_queue: list[dict], *, reason: str) -> list[dict]:
    """Fail-closed: when LLM fallback is unavailable, flag all queued params
    for human review rather than silently passing them (GMP safety principle)."""
    findings = []
    for q in llm_queue:
        findings.append({
            "page": q["page"],
            "type": "completeness",
            "severity": "warning",
            "description": (
                f"第{q['page']}页 参数 {q['name']}={q['actual']}{q.get('unit','')} "
                f"规格 {q['spec']} 无法自动判定（{reason}），需人工确认"
            ),
            "ocr_text": f"{q['name']}: spec={q['spec']} actual={q['actual']}",
            "operator": "",
            "source": "rule",
        })
    return findings


# LLM fallback 单次调用的最大参数数量。
# 超过此数量时直接 flag 为 human review，避免 prompt 过大导致 LLM 调用超时/重试
# （实测 449 个参数会导致 SiliconFlow API 重试并卡住 Stage 3 数分钟）。
# 50 个参数的单次 LLM 调用 prompt 约 3-4KB，在 4000 max_tokens 内可完成。
_LLM_FALLBACK_BATCH_MAX = 50


async def _llm_fallback_check(llm_queue: list[dict], *, job_id: str = "") -> list[dict]:
    if not llm_queue:
        return []

    # 防止 LLM 调用过载：队列过大时直接 flag 为 human review。
    # 449 个参数的 prompt 会超过 LLM 的处理能力，导致重试和超时。
    # GMP 安全原则：无法自动判定时 flag 为人工复核，而非让 pipeline 卡死。
    if len(llm_queue) > _LLM_FALLBACK_BATCH_MAX:
        logger.warning(
            f"[{job_id}] LLM fallback queue too large ({len(llm_queue)} > "
            f"{_LLM_FALLBACK_BATCH_MAX}), flagging all as human review"
        )
        return _flag_llm_queue_for_review(
            llm_queue,
            reason=f"参数数量过多（{len(llm_queue)}），超出自动判定上限，需人工确认"
        )

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
        logger.warning(f"LLM fallback failed: {e} — flagging {len(llm_queue)} params for human review")
        return _flag_llm_queue_for_review(llm_queue, reason=f"LLM 调用失败: {type(e).__name__}")
    if isinstance(result, dict) and result.get("_parse_error"):
        logger.warning(f"LLM fallback: JSON parse failure — flagging {len(llm_queue)} params for human review")
        return _flag_llm_queue_for_review(llm_queue, reason="LLM 返回 JSON 解析失败")
    items = result if isinstance(result, list) else (
        result.get("items", []) if isinstance(result, dict) else []
    )
    findings = []
    for item in items:
        # P1-2: type-polluted fallback output must not crash Stage 3
        if not isinstance(item, dict):
            logger.warning(f"LLM fallback: skipping non-dict item: {item}")
            continue
        try:
            idx = int(item.get("index", 0)) - 1
        except (TypeError, ValueError):
            idx = -1
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
给定一份批生产记录所有页的结构化数据摘要（已由规则层跑过规则，
请只关注规则层无法判定的语义异常，例如签名一致性、批次逻辑、跨页参数漂移等。

输出 JSON 数组，每个 finding 包含：
{"page":页码,"type":"signature_mismatch|param_out_of_spec|completeness|user_rule","severity":"critical|warning|info","description":"问题描述","ocr_text":"原文摘录","operator":"涉及人员","rule_id":"命中规则的ID"}

type 为 "user_rule" 时表示命中用户自定义合规规则（按用户规则逐条核对），
且 rule_id 必须填写命中的那条规则的 ID（取自下方规则清单中的 [规则ID: xxx]，逐字照抄；未命中用户规则时该字段填 null）。"""


def _user_rules_section(rules: list[dict] | None = None) -> tuple[str, str]:
    """构造用户自定义合规规则提示段 + 内容 hash。

    返回 (section_text, rules_hash)：无启用规则时返回 ("", "none")。
    hash 用于 llm_call_audit 的 prompt_version 留痕（GMP 可追溯）。
    传入预加载的启用规则列表可避免每 job 重复读 config.json（性能）。
    """
    if rules is None:
        from config import load_user_rules
        rules = [r for r in load_user_rules() if r.get("active")]
    if not rules:
        return "", "none"
    # 对抗审查 P2-8：手改 config.json（文档支持）可造出缺 id/text 的规则，
    # r['id'] 下标访问 → KeyError → 整个 Stage 3 崩溃。防御式取值并跳过
    # 无文本的条目（无法执行的规则不应进 prompt）。
    rules = [r for r in rules if isinstance(r, dict) and str(r.get("text", "")).strip()]
    if not rules:
        return "", "none"
    lines = [f"- [规则ID: {r.get('id', '?')}] {r.get('text')}" for r in rules]
    section = (
        "<USER_RULES>\n"
        "用户自定义合规规则（必须逐条核对记录是否满足，"
        "违反时输出 type=user_rule 的 finding，按影响定级，rule_id 照抄对应规则 ID）：\n"
        + "\n".join(lines)
        + "\n</USER_RULES>"
    )
    import hashlib
    rules_hash = hashlib.md5(section.encode("utf-8")).hexdigest()[:8]
    return section, rules_hash


async def _llm_based_check(summary: str, *, job_id: str = "",
                           user_rules: list[dict] | None = None) -> list[dict]:
    if not summary.strip():
        return []
    # 每次 job 只读一次 config.json（analyze_cross_page 预加载后传入）
    if user_rules is None:
        from config import load_user_rules
        user_rules = [r for r in load_user_rules() if r.get("active")]
    user_section, rules_hash = _user_rules_section(user_rules)
    prompt = f"{user_section}\n\n{summary}" if user_section else summary
    prompt_version = f"semantic_v2+rules{rules_hash}" if rules_hash != "none" else "semantic_v2"
    client = get_llm_client()
    try:
        result = await client.chat_json(
            SYSTEM_PROMPT, prompt, max_tokens=4000, temperature=0.1, timeout=180.0,
            audit_ctx={"job_id": job_id, "page": None, "stage": "cross_page_llm",
                       "prompt_version": prompt_version},
        )
    except Exception as e:
        logger.warning(f"LLM semantic check failed: {e} — flagging for human review")
        return [{
            "page": 0,
            "type": "completeness",
            "severity": "info",
            "description": (
                f"跨页 LLM 语义检查不可用（LLM 调用失败: {type(e).__name__}），"
                f"规则层未覆盖的跨页异常（如签名一致性、批次逻辑、参数漂移）需人工复核"
            ),
            "ocr_text": "",
            "operator": "",
            "source": "rule",
        }]
    if isinstance(result, dict) and result.get("_parse_error"):
        logger.warning("Cross-page LLM analysis: JSON parse failure — flagging for human review")
        return [{
            "page": 0,
            "type": "completeness",
            "severity": "info",
            "description": (
                "跨页 LLM 语义检查返回 JSON 解析失败，规则层未覆盖的跨页异常"
                "（如签名一致性、批次逻辑、参数漂移）需人工复核"
            ),
            "ocr_text": "",
            "operator": "",
            "source": "rule",
        }]
    if isinstance(result, list):
        findings = result
    elif isinstance(result, dict):
        findings = result.get("findings", [])
    else:
        findings = []
        logger.warning(f"Cross-page LLM analysis: unexpected result type {type(result).__name__}, no findings")
    valid = []
    required = {"page", "type", "severity", "description"}
    # 启用规则 id 集合：user_rule finding 的 rule_id 只接受集合内的 id
    enabled_rule_ids = {r["id"] for r in user_rules}
    for f in findings:
        if not isinstance(f, dict) or not required.issubset(f.keys()):
            logger.warning(f"Skipping invalid finding (missing fields): {f}")
            continue
        f.setdefault("ocr_text", "")
        f.setdefault("operator", "")
        # user_rule 类型的 finding 标记独立 source，便于复核页区分来源
        f["source"] = "user_rule" if f.get("type") == "user_rule" else "llm_cross"
        # rule_id 防伪：超出启用规则集合（含缺失/幻觉 id）一律置 None
        rid = f.get("rule_id")
        if f["source"] == "user_rule" and rid in enabled_rule_ids:
            f["rule_id"] = rid
        else:
            f["rule_id"] = None
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
