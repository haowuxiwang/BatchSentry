from __future__ import annotations

import logging

from typing import Optional

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
