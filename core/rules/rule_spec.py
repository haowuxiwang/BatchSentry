from __future__ import annotations

import logging

from core.rules.parsing import (
    _decimal_loss_factor,
    _extract_unit,
    _judge,
    _parse_number,
    _parse_spec,
    _sign_convention_uncertain,
    _try_unit_normalize,
    _violated_bound,
)
from core.rules.finding_aggregate import (
    CAUSE_DECIMAL_LOSS,
    CAUSE_EDGE_MARGIN,
    CAUSE_HARD,
    CAUSE_PRINTED,
    CAUSE_SIGN_UNCERTAIN,
    CAUSE_UNIT_MISMATCH,
    KIND_CELL,
    KIND_PARAM,
    make_oos_finding,
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
        # Units differ but no conversion available — fail-closed, human review.
        # M2：类型修正 —— 这是"规格无法核定"，不是"记录不完整"（原误标
        # completeness 会污染类型分布与前端文案）。
        findings.append(make_oos_finding(
            page=page, type_="spec_unverifiable", severity="warning",
            kind=KIND_PARAM, subject=name, cause=CAUSE_UNIT_MISMATCH,
            desc_value=actual, raw_value=actual,
            unit=p.get("unit") or "", spec=spec,
        ))
        return
    compare_num = converted if converted is not None else actual_num
    if _sign_convention_uncertain(bounds, compare_num):
        # M8：真空度/负压符号约定存疑（负表压 vs 印版正限值）→ fail-closed 人工。
        findings.append(make_oos_finding(
            page=page, type_="spec_unverifiable", severity="warning",
            kind=KIND_PARAM, subject=name, cause=CAUSE_SIGN_UNCERTAIN,
            desc_value=actual_num, raw_value=actual,
            unit=p.get("unit") or "", spec=spec,
        ))
        return
    in_spec = _judge(bounds, compare_num)
    p["in_spec"] = in_spec
    if not in_spec:
        # 手写体存疑原则：边缘超范围（相对偏差 ≤10%）可能是 OCR 对手写
        # 数值的误读（如 5.2 读成 5.7），降级为 info 并提示人工核对；
        # 明显超范围（>10%）维持 warning 铁口判定。spec 基准本身是
        # 印刷体（High Trust），此处只软化"实际值"这一侧的判定力度。
        severity, hint, cause = _grade_out_of_spec(
            bounds, compare_num, spec, p.get("value_source"), str(actual))
        findings.append(make_oos_finding(
            page=page, type_="param_out_of_spec", severity=severity,
            kind=KIND_PARAM, subject=name, cause=cause,
            desc_value=actual_num, raw_value=actual,
            unit=p.get("unit") or "", spec=spec, suffix=f"{note}{hint}",
        ))


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
        # Units differ but no conversion available — fail-closed, human review.
        # M2：类型修正（同上，参数分支）—— 归为 spec_unverifiable。
        findings.append(make_oos_finding(
            page=page, type_="spec_unverifiable", severity="warning",
            kind=KIND_CELL, subject=col, cause=CAUSE_UNIT_MISMATCH,
            desc_value=actual, raw_value=actual,
            unit=val.get("unit") or "", spec=spec, where=t,
        ))
        return
    compare_num = converted if converted is not None else actual_num
    if _sign_convention_uncertain(bounds, compare_num):
        # M8：同 _judge_param —— 负表压 vs 印版正限值，符号约定存疑 → 人工。
        findings.append(make_oos_finding(
            page=page, type_="spec_unverifiable", severity="warning",
            kind=KIND_CELL, subject=col, cause=CAUSE_SIGN_UNCERTAIN,
            desc_value=actual_num, raw_value=actual,
            unit=val.get("unit") or "", spec=spec, where=t,
        ))
        return
    in_spec = _judge(bounds, compare_num)
    val["in_spec"] = in_spec
    if not in_spec:
        severity, hint, cause = _grade_out_of_spec(
            bounds, compare_num, spec, val.get("value_source"), str(actual))
        findings.append(make_oos_finding(
            page=page, type_="param_out_of_spec", severity=severity,
            kind=KIND_CELL, subject=col, cause=cause,
            desc_value=actual_num, raw_value=actual,
            unit=val.get("unit") or "", spec=spec, suffix=f"{note}{hint}", where=t,
        ))


# Edge margin for out-of-spec softening: when the measured value deviates from
# the spec bound by ≤10% (relative), OCR misread of a handwritten value is a
# real possibility — downgrade to info + hint instead of a hard warning.
_EDGE_MARGIN = 0.10


def _grade_out_of_spec(bounds, actual: float, spec: str,
                       value_source: str = "unknown",
                       raw: str = "") -> tuple[str, str, str]:
    """越界值的**唯一**判级实现：返回 ``(severity, hint, cause)``。

    ``cause`` 是 B1-1 聚合所需的**根因标签**（由判级本身派生，不从文案反推）。
    """
    if value_source == "printed":
        return "warning", "", CAUSE_PRINTED
    bound = _violated_bound(bounds, actual)
    if bound is None or bound == 0:
        return "warning", "", CAUSE_HARD
    rel_dev = abs(actual - bound) / abs(bound)
    if rel_dev <= _EDGE_MARGIN:
        return ("info",
                "，超差幅度较小（≤10%），可能为手写 OCR 误读，请对照 PDF 原页人工核对",
                CAUSE_EDGE_MARGIN)
    # OCR 丢小数点：M8 真实 p08 实测 —— 手写 "4.6" 被 OCR 读成 "46"，而规格是
    # 3.0~5.0bar（同页其余压力 2~4bar，46bar 在 TFF 上不可能）。特征：数值与规格
    # 差约 10 的幂次，缩放后即可落回规格内。仍作 finding 表面化（info + 提示），
    # 不静默丢弃，避免掩盖真实偏差。
    if _decimal_loss_factor(bounds, actual, raw) is not None:
        return ("info",
                "，实测值与规格相差约 10 倍（疑似 OCR 丢失小数点，如 4.6 读成 46），请对照 PDF 原页人工核对",
                CAUSE_DECIMAL_LOSS)
    return "warning", "", CAUSE_HARD


def _severity_for_out_of_spec(bounds, actual: float, spec: str,
                              value_source: str = "unknown",
                              raw: str = "") -> tuple[str, str]:
    """``(severity, hint)`` —— ``_grade_out_of_spec`` 的兼容包装。

    保留 2 元组契约（既有调用点与测试依赖它）；根因标签按需从
    ``_grade_out_of_spec`` 取第三项，**不存在第二份判级逻辑**。
    """
    severity, hint, _cause = _grade_out_of_spec(
        bounds, actual, spec, value_source, raw)
    return severity, hint
