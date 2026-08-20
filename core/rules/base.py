from __future__ import annotations

import logging

logger = logging.getLogger(__name__)



# ---------------------------------------------------------------------------
# Page normalization
# ---------------------------------------------------------------------------


def _only_dicts(lst) -> list[dict]:
    """Keep only dict elements — Stage 3 rules call .get() on every element,
    so non-dict entries (type-polluted LLM output) would crash the whole job.
    P1-2 second line of defense (page_analyzer._sanitize_page_result is the
    source-side filter; this covers legacy page_cache rows written before it)."""
    return [x for x in lst if isinstance(x, dict)]


# ---------------------------------------------------------------------------
# value_source inference (fallback when the LLM omits the field)
# ---------------------------------------------------------------------------

_HANDWRITTEN_KEYWORDS = ("实际", "实测", "记录", "填写", "手写", "结果", "偏差")
_PRINTED_KEYWORDS = ("规格", "标准", "范围", "指导", "要点", "要求", "检查项目", "项目")


def _infer_value_source(name: str, fallback: str = "unknown") -> str:
    """Infer value_source from a column/parameter name when the LLM omitted it.

    Column headers are printed text (high trust), so keyword matching on the
    header is a reliable proxy: "实际/实测/记录…" headers → handwritten cell
    values; "规格/标准/指导…" headers → printed reference values. No keyword
    hit → unknown (rules treat unknown conservatively, like handwritten).
    """
    if not isinstance(name, str) or not name:
        return fallback
    for kw in _HANDWRITTEN_KEYWORDS:
        if kw in name:
            return "handwritten"
    for kw in _PRINTED_KEYWORDS:
        if kw in name:
            return "printed"
    return fallback


def _backfill_value_source(data: dict) -> None:
    """Fill missing value_source on parameters/measurements in-place.

    Called from _normalize_pages before rules run; leaves LLM-provided values
    untouched (LLM semantic judgment wins over the header heuristic).
    """
    for s in data.get("steps", []) or []:
        if not isinstance(s, dict):
            continue
        for p in s.get("parameters", []) or []:
            if not isinstance(p, dict):
                continue
            if not p.get("value_source"):
                p["value_source"] = _infer_value_source(p.get("name", ""))
        for m in s.get("measurements", []) or []:
            if not isinstance(m, dict):
                continue
            for col, v in (m.get("values") or {}).items():
                if not isinstance(v, dict):
                    continue
                if not v.get("value_source"):
                    v["value_source"] = _infer_value_source(col)


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
        _backfill_value_source(data)
        # P1-2 兜底: 过滤 steps/findings 顶层非 dict 元素；对每个 step 再
        # 过滤 parameters/measurements/signatures 子元素（规则层对每个
        # 元素直接调 .get()，字符串元素会 AttributeError 崩掉整个 Stage 3）。
        steps = []
        for s in data.get("steps", []) or []:
            if not isinstance(s, dict):
                continue
            for field in ("parameters", "measurements", "signatures", "checks"):
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
            for c in s.get("checks", []) or []:
                for field in ("item", "selected", "marker"):
                    v = c.get(field)
                    if v is not None and not isinstance(v, str):
                        c[field] = str(v)
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
