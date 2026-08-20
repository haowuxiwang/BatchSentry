from __future__ import annotations

import logging

from core.rules.parsing import (
    _extract_unit,
    _judge,
    _parse_number,
    _parse_spec,
    _try_unit_normalize,
)

logger = logging.getLogger(__name__)



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
        # 手写体存疑原则：边缘超范围（相对偏差 ≤10%）可能是 OCR 对手写
        # 数值的误读（如 5.2 读成 5.7），降级为 info 并提示人工核对；
        # 明显超范围（>10%）维持 warning 铁口判定。spec 基准本身是
        # 印刷体（High Trust），此处只软化"实际值"这一侧的判定力度。
        severity, hint = _severity_for_out_of_spec(
            bounds, compare_num, spec, p.get("value_source"))
        findings.append({
            "page": page,
            "type": "param_out_of_spec",
            "severity": severity,
            "description": (
                f"第{page}页 参数 {name}={actual_num}{p.get('unit') or ''} "
                f"不在规格 {spec} 内{note}{hint}"
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
        severity, hint = _severity_for_out_of_spec(
            bounds, compare_num, spec, val.get("value_source"))
        findings.append({
            "page": page,
            "type": "param_out_of_spec",
            "severity": severity,
            "description": (
                f"第{page}页 {col} 在 {t} 时实测 {actual_num}{val.get('unit') or ''} "
                f"不在规格 {spec} 内{note}{hint}"
            ),
            "ocr_text": f"{t} {col}: spec={spec} actual={actual}",
            "operator": "",
            "source": "rule",
        })


# Edge margin for out-of-spec softening: when the measured value deviates from
# the spec bound by ≤10% (relative), OCR misread of a handwritten value is a
# real possibility — downgrade to info + hint instead of a hard warning.
_EDGE_MARGIN = 0.10


def _severity_for_out_of_spec(bounds, actual: float, spec: str,
                              value_source: str = "unknown") -> tuple[str, str]:
    """Decide severity + hint for an out-of-spec value.

    Returns (severity, hint_suffix). The printed spec is high-trust; the
    measured value may be handwritten OCR (low trust). Within 10% of the
    violated bound → info + "可能为手写 OCR 误读，请人工核对"; beyond → warning.
    value_source="printed" values are document-level printouts (the spec
    bounds themselves are wrong) → hard warning, no OCR softening.
    """
    if value_source == "printed":
        return "warning", ""
    bound = None
    if bounds.op == "between":
        if actual < bounds.low:
            bound = bounds.low
        else:
            bound = bounds.high
    elif bounds.op in ("le", "lt"):
        bound = bounds.high
    elif bounds.op in ("ge", "gt"):
        bound = bounds.low
    if bound is None or bound == 0:
        return "warning", ""
    rel_dev = abs(actual - bound) / abs(bound)
    if rel_dev <= _EDGE_MARGIN:
        return "info", "，超差幅度较小（≤10%），可能为手写 OCR 误读，请对照 PDF 原页人工核对"
    return "warning", ""
