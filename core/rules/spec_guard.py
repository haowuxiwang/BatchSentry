"""LLM 自报 ``param_out_of_spec`` 的规则层复核（M8 / P0）。

背景
----
LLM 逐页分析（``core/page_analyzer.py``）既抽取结构化字段
（``parameters`` / ``measurements``），又自报一份 ``findings`` 数组。规则层会用
同一份结构化数据经 :func:`core.rules.parsing._parse_spec` / ``_judge`` 重判一遍，
但 LLM 自报的 ``param_out_of_spec`` 是**直接落库、未经任何可判性校验**的，于是
产生成片误报。

M8 真实 51 页实测（p14）：OCR 把印版 ``40±3°C`` 读成 ``40 3°C``，LLM 未走可判性
校验即把 42.1°C（落在 [37, 43] 内）报成超差；同理 ``7.5±0.2`` 被读成 ``7.5 0.2``，
7.49 被报超差。同一页 6 条 LLM 超差结论中 5 条为误报，而**同一批数据经规则层判定
后 0 误报**——差异只在"是否走同一解析器"。

本模块用规则层**同一个**解析器复核 LLM 自报结论：能定位到结构化字段且判为合规则
的，视为 LLM 误报予以剔除；定位不到、或解析/判定不出的，一律**保留**（fail-closed，
交人工复核）——绝不因"复核不确定"而吞掉潜在真实偏差。

纯函数、无 DB、无网络，可在 core/api 两侧自由引用。
"""
from __future__ import annotations

import re

from core.rules.parsing import (
    _judge,
    _parse_number,
    _parse_spec,
    _sign_convention_uncertain,
)

# LLM 自报超差结论可能使用的 type 字面量（含规范化的别名）。
_SPEC_TYPES = frozenset({
    "param_out_of_spec",
    "param_out_of_range",
    "out_of_spec",
    "parameter_violation",
})

_NUM = re.compile(r"-?\d+\.?\d*")


def _base_name(name: str) -> str:
    """测量列名常带括号规格，如 ``温度 ( 40 3°C )`` → ``温度``。"""
    return name.split("(")[0].split("（")[0].strip()


def index_specs(structured: dict) -> list[tuple[str, str, str]]:
    """从结构化页数据抽出 ``(名称, spec, actual)`` 三元组全集。

    覆盖 ``steps[].parameters[]``（单值参数）与 ``steps[].measurements[].values``
    （矩阵单元格）；测量列名同时给出原始名与去括号基名，便于与 LLM 文案比对。
    """
    out: list[tuple[str, str, str]] = []

    def _add(name, spec, actual):
        spec, actual = str(spec or ""), str(actual or "")
        if not spec and not actual:
            return
        for nm in {str(name or "").strip(), _base_name(str(name or ""))}:
            if nm:
                out.append((nm, spec, actual))

    for step in structured.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        for p in step.get("parameters", []) or []:
            if isinstance(p, dict):
                _add(p.get("name"), p.get("spec_range"), p.get("value"))
        for m in step.get("measurements", []) or []:
            if not isinstance(m, dict):
                continue
            for col, val in (m.get("values") or {}).items():
                if isinstance(val, dict):
                    _add(col, val.get("spec"), val.get("actual"))
    return out


def _triple_state(spec: str, actual: str) -> str:
    """返回 ``"in"`` / ``"out"`` / ``"unknown"``。

    ``unknown`` = 规格或实测值不可解析 → **不得**据此剔除 LLM 结论（fail-closed）。
    """
    bounds = _parse_spec(spec)
    if bounds is None:
        return "unknown"
    num = _parse_number(actual)
    if num is None:
        # 实测值里可能夹着备注（如 "7.49/A"），退一步取首个数字。
        m = _NUM.search(actual)
        if not m:
            return "unknown"
        num = float(m.group(0))
    # 符号约定存疑（负表压 vs 印版正限值）与规则层同源判定 → 不可据此剔除。
    if _sign_convention_uncertain(bounds, num):
        return "unknown"
    return "in" if _judge(bounds, num) else "out"


def drop_unfounded_spec_findings(
    findings: list, structured: dict
) -> tuple[list, int]:
    """剔除 LLM 无法被规则层复核为"超差"的 ``param_out_of_spec`` 结论。

    返回 ``(保留的 findings, 剔除条数)``。判定规则：

    * 非 ``param_out_of_spec`` 类 finding：原样保留；
    * 文案（description + ocr_text）中能定位到结构化三元组，且所有命中的三元组
      都被规则层判为**合规**：剔除（LLM 误报）；
    * 命中三元组中存在被判**超差**者：保留（真实偏差，规则层已独立佐证）；
    * 定位不到任何三元组，或命中三元组状态为 ``unknown``：保留（fail-closed）。
    """
    index = index_specs(structured)
    kept: list = []
    dropped = 0
    for f in findings:
        if not isinstance(f, dict):
            kept.append(f)
            continue
        if str(f.get("type") or "") not in _SPEC_TYPES:
            kept.append(f)
            continue
        text = f"{f.get('description', '')} {f.get('ocr_text', '')}"
        nums = {m.group(0) for m in _NUM.finditer(text)}
        matched = [
            (name, spec, actual)
            for name, spec, actual in index
            if name and name in text
        ]
        if not matched:
            kept.append(f)
            continue
        # 用实测值进一步收敛到具体行（同名多行时），但只做"缩小"不做"扩大"：
        # 若文案里出现了某三元组的 actual，则只保留这些更贴合的候选。
        precise = [t for t in matched if str(t[2]) and str(t[2]) in text]
        if precise:
            matched = precise
        states = {_triple_state(spec, actual) for _, spec, actual in matched}
        if states and states <= {"in"}:
            dropped += 1
            continue
        kept.append(f)
    return kept, dropped
