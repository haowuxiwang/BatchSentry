from __future__ import annotations

import logging
import re
from typing import Optional

from core.rules.parsing import (
    _decimal_loss_factor,
    _extract_unit,
    _judge,
    _parse_number,
    _parse_spec,
    _sign_convention_uncertain,
    _try_unit_normalize,
    _violated_bound,
    bound_plausibility_reason,
    spec_shape_reason,
    value_shape_reason,
)
from core.rules.finding_aggregate import (
    CAUSE_DECIMAL_LOSS,
    CAUSE_EDGE_MARGIN,
    CAUSE_HARD,
    CAUSE_KEY_PARAM_UNVERIFIED,
    CAUSE_PRINTED,
    CAUSE_RATIO_IMPLAUSIBLE,
    CAUSE_SIGN_UNCERTAIN,
    CAUSE_SPEC_NOT_A_LIMIT,
    CAUSE_TIME_LITERAL,
    CAUSE_UNIT_MISMATCH,
    KIND_CELL,
    KIND_PARAM,
    expected_type,
    make_oos_finding,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 关键参数标记（TODO 0-72）
# ---------------------------------------------------------------------------

#: 原页用来标注「关键工艺参数」「质量控制点」的符号（页脚有图例：
#: 「▲有下划线标的为关键工艺参数，★有下划线标的为质量控制点」）。
_KEY_MARKERS = ("▲", "★")
#: 标记与规格字面量的最大字符距离。取 90 是因为实测行文形如
#: `▲pH为 6.8~7.0。` / `★...≤1.5`，而页脚图例里 ▲/★ 不跟规格字面量。
_MARK_WINDOW_BEFORE = 12
_MARK_WINDOW_AFTER = 90
_WS = re.compile(r"\s+")
_DASHES = re.compile(r"[~～\-－—–至]")


def _norm_literal(s) -> str:
    """抹空白 + 统一区间连接符，使 ``6.8~7.0`` / ``6.8 - 7.0`` 可比。"""
    return _DASHES.sub("-", _WS.sub("", str(s or "")))


def _key_param_marked(page_text: str, spec) -> bool:
    """该参数的规格字面量是否**紧邻**关键参数标记（▲/★）。

    判定完全确定性（可离线复算）。为什么用"规格字面量"而不是参数名做锚：
    参数名是 LLM 归并后的（`中和后 pH`），而原页指导栏写的是 `▲pH为 6.8~7.0`
    —— 名字对不上，规格字面量却能逐字符命中。页脚图例虽有 ▲/★，但窗口内
    没有规格字面量，故不会误命中。
    """
    lit = _norm_literal(spec)
    if len(lit) < 3 or not page_text:
        return False
    text = str(page_text)
    for mk in _KEY_MARKERS:
        start = 0
        while True:
            i = text.find(mk, start)
            if i < 0:
                break
            seg = text[max(0, i - _MARK_WINDOW_BEFORE): i + _MARK_WINDOW_AFTER]
            if lit in _norm_literal(seg):
                return True
            start = i + 1
    return False



# ---------------------------------------------------------------------------
# R3: param_out_of_spec (rule-judgable only; rest go to llm_queue)
# ---------------------------------------------------------------------------


def _check_param_out_of_spec(pages: list[dict], llm_queue: list[dict]) -> list[dict]:
    findings = []
    for page in pages:
        pno = page["page"]
        # 关键参数标记只在**原文**里（结构化参数没有这一列），故整页取一次。
        page_text = page.get("raw_html") or ""
        for step in page["steps"]:
            step_no = step.get("step_no", "?")
            # single-value parameters
            for p in step.get("parameters", []) or []:
                _judge_param(p, pno, step_no, p.get("name", ""), findings, llm_queue,
                             page_text=page_text)
            # matrix cells
            for m_idx, m in enumerate(step.get("measurements", []) or []):
                t = m.get("time", "")
                for col, val in (m.get("values") or {}).items():
                    _judge_cell(val, pno, step_no, col, t, findings, llm_queue,
                                page_text=page_text)
    return findings


def _judge_param(p: dict, page: int, step_no, name: str,
                 findings: list[dict], llm_queue: list[dict],
                 page_text: str = "") -> Optional[bool]:
    """单值参数判定 —— 只是 :func:`_judge_value` 的取字段壳（判定逻辑唯一）。"""
    res = _judge_value(
        page=page, step_no=step_no, name=name,
        spec=p.get("spec_range"), actual=p.get("value"),
        unit=p.get("unit") or "", kind=KIND_PARAM, where="",
        value_source=p.get("value_source"),
        findings=findings, llm_queue=llm_queue, page_text=page_text,
    )
    if res is not None:
        p["in_spec"] = res
    return res


def _judge_cell(val: dict, page: int, step_no, col: str, t: str,
                findings: list[dict], llm_queue: list[dict],
                page_text: str = "") -> Optional[bool]:
    """矩阵单元格判定 —— 同上，只是 ``where`` 带时间点。"""
    res = _judge_value(
        page=page, step_no=step_no, name=col,
        spec=val.get("spec"), actual=val.get("actual"),
        unit=val.get("unit") or "", kind=KIND_CELL, where=t,
        value_source=val.get("value_source"),
        findings=findings, llm_queue=llm_queue, page_text=page_text,
    )
    if res is not None:
        val["in_spec"] = res
    return res


def _judge_value(*, page: int, step_no, name: str, spec, actual, unit: str,
                 kind: str, where: str, value_source,
                 findings: list[dict], llm_queue: list[dict],
                 page_text: str = "") -> Optional[bool]:
    """参数/单元格的**唯一**判定实现。

    返回 ``True``（合规）/ ``False``（超差）/ ``None``（不可判，已分流）。
    不可判有两条去向，**必须区分**：

    * **送 LLM 兜底**（``llm_queue``）：规格/实测值解析不出、且没有形状级
      证据说明"这个比较不成立"。LLM 可能读得懂自由文本，所以不排除它能判。
    * **发 ``spec_unverifiable``**：系统**知道**这个比较站不住 —— 值槽是时刻、
      规格只写目标值、限值为 0、检测限高于限值、整数读数差 5 倍以上、
      疑似丢小数点、单位不可换算、符号约定存疑。此时若仍发
      ``param_out_of_spec``，就是"护栏知道错还发"（CR-12）。
    """
    if actual is None or actual == "":
        return None

    actual_num: Optional[float] = None

    def _emit_unverifiable(cause: str, extra: str = "") -> None:
        findings.append(make_oos_finding(
            page=page, type_=expected_type(cause), severity="warning",
            kind=kind, subject=name, cause=cause,
            desc_value=actual_num if actual_num is not None else actual,
            raw_value=actual, unit=unit or "", spec=spec,
            extra=extra, where=where,
        ))

    def _enqueue() -> None:
        entry = {
            "page": page, "step_no": step_no, "name": name,
            "spec": spec, "actual": actual, "unit": unit or "",
            "kind": kind,
        }
        if kind == KIND_CELL:
            entry["time"] = where
        llm_queue.append(entry)

    # ① 规格级：先确认"存在一个可比较的数值规格"。
    #    ⚠️ 顺序不能反：`接收开始时间`/`搅拌结束时间` 这类参数**本来就**是
    #    时刻值、且规格栏为空 —— 若先判"值槽是时刻"，会把 40 余条正常的时间
    #    参数全部标成"列串位"（R100 实测踩过：spec_unverifiable 从 16 涨到 54）。
    bounds = _parse_spec(spec)
    if bounds is None:
        # 规格栏只写目标值、无允许范围（真实 p24「浓缩液体积 50L」记 48 L）。
        # 这不是"规格没抽到"，而是"规格本身不是限值"。
        if spec_shape_reason(spec, actual):
            _emit_unverifiable(CAUSE_SPEC_NOT_A_LIMIT)
            return None
        _enqueue()
        return None

    # ② 形状级：规格是可比较的数值范围，而值槽取到的是时刻 ⇒ 列串位。
    #    必须早于 `_judge`：否则「07 时 30 分」被读成 7.5 并恰好合规，
    #    静默放过一次真实的列串位（真实 p38 是 18 时 30 分才侥幸暴露）。
    if value_shape_reason(actual):
        _emit_unverifiable(CAUSE_TIME_LITERAL)
        return None

    actual_num = _parse_number(actual)
    if actual_num is None:
        _enqueue()
        return None

    # Unit-aware comparison: convert actual to spec's unit when possible
    actual_unit = _extract_unit(str(actual))
    converted, note = _try_unit_normalize(actual_num, actual_unit, spec)
    if converted is None and note == "unit_mismatch":
        # Units differ but no conversion available — fail-closed, human review.
        # M2：类型修正 —— 这是"规格无法核定"，不是"记录不完整"（原误标
        # completeness 会污染类型分布与前端文案）。
        _emit_unverifiable(CAUSE_UNIT_MISMATCH)
        return None
    compare_num = converted if converted is not None else actual_num
    if _sign_convention_uncertain(bounds, compare_num):
        # M8：真空度/负压符号约定存疑（负表压 vs 印版正限值）→ fail-closed 人工。
        _emit_unverifiable(CAUSE_SIGN_UNCERTAIN)
        return None
    in_spec = _judge(bounds, compare_num)
    if in_spec:
        # TODO 0-72：**关键参数 + 手写**的「合格」不是证据支撑的结论。
        # 实测（2026-10-29）：同一格 pH 原页 1.88（越界 6.8~7.0），Paddle 整页、
        # Paddle 400dpi 裁切、MinerU 三次读全是 6.88 ⇒ 双引擎**同错**，
        # "二次读一致"不能当证据。既然"值对不对"不可自证，就只能交人工，
        # 而不是静默返回 True。取值范围：仅原页标了 ▲/★ 的行（见 _key_param_marked）。
        if value_source == "handwritten" and _key_param_marked(page_text, spec):
            findings.append(make_oos_finding(
                page=page, type_=expected_type(CAUSE_KEY_PARAM_UNVERIFIED),
                severity="info", kind=kind, subject=name,
                cause=CAUSE_KEY_PARAM_UNVERIFIED,
                desc_value=actual_num, raw_value=actual, unit=unit or "",
                spec=spec, where=where,
            ))
        return True

    # ③ 越界可信度 —— 这个"越界"本身可信吗？（CR-12 ①②）
    #    手写体存疑原则在此之上仍然有效：边缘超范围（≤10%）可能只是 OCR
    #    对手写数值的误读，降级 info 并提示人工核对；spec 基准本身是印刷体
    #    （High Trust），此处只软化"实际值"这一侧的判定力度。
    implausible = bound_plausibility_reason(bounds, compare_num, str(actual))
    if implausible is not None:
        extra = ""
        if implausible == CAUSE_RATIO_IMPLAUSIBLE:
            bound = _violated_bound(bounds, compare_num)
            if bound:
                extra = f"{abs(compare_num) / abs(bound):.1f}"
        _emit_unverifiable(implausible, extra=extra)
        return False

    severity, hint, cause = _grade_out_of_spec(
        bounds, compare_num, spec, value_source, str(actual))
    findings.append(make_oos_finding(
        page=page, type_=expected_type(cause), severity=severity,
        kind=kind, subject=name, cause=cause,
        desc_value=actual_num, raw_value=actual,
        unit=unit or "", spec=spec, suffix=f"{note}{hint}", where=where,
    ))
    return False


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
