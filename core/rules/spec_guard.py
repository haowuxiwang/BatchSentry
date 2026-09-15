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

抑制必须留痕（P0-2，法规必需）
------------------------------
**抑制 ≠ 删除**。EU GMP Annex 11 §16 与中国附录《计算机化系统》第 15/16 条要求
关键数据的修改经批准并记录理由；PIC/S PI 041-1 认可"经验证的异常报告"替代逐页
复核，前提是留痕可追溯。因此 :func:`drop_unfounded_spec_findings` **返回明细而非
仅计数**：每一条被抑制的条目都带非空 ``reason`` 与结构化 ``evidence``（命中的三元组
及各自的判定），由调用方落库（``finding_suppressions``）并在复核页可查、可回退。

纯函数、无 DB、无网络，可在 core/api 两侧自由引用。
"""
from __future__ import annotations

import json
import re

from core.rules.parsing import (
    _decimal_loss_factor,
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

# 名称比对前先归一：LLM 文案与结构化列名的分隔符常不一致
# （结构化 "T2101a_压力" vs 文案 "T2101a 压力"），原样子串匹配会漏配 →
# 误报被 fail-closed 保留（M8 实测 p9 因此漏掉 7 条 critical 误报）。
_NORM_DROP = str.maketrans({c: "" for c in " \t\u3000_-—–:：/（）()[]【】"})


def _norm(s: str) -> str:
    return str(s or "").strip().lower().translate(_NORM_DROP)


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
    """返回 ``"in"`` / ``"soft"`` / ``"out"`` / ``"unknown"``。

    * ``in``：规则层判为合规（LLM 结论为误报）；
    * ``soft``：疑似 OCR 丢小数点，规则层已以 info+hint 独立呈现同一三元组，
      故 LLM 的重复条目可一并剔除（避免同一条目 severity 口径不一）；
    * ``out``：规则层独立佐证为真超差 → 保留；
    * ``unknown``：规格/实测值不可解析，或符号约定存疑 → **不得**据此剔除。
    """
    bounds = _parse_spec(spec)
    if bounds is None:
        return "unknown"
    num = _parse_number(actual)
    if num is None:
        return "unknown"
    # 符号约定存疑（负表压 vs 印版正限值）与规则层同源判定 → 不可据此剔除。
    if _sign_convention_uncertain(bounds, num):
        return "unknown"
    if _judge(bounds, num):
        return "in"
    if _decimal_loss_factor(bounds, num, actual) is not None:
        return "soft"
    return "out"


def drop_unfounded_spec_findings(
    findings: list, structured: dict
) -> tuple[list, list[dict]]:
    """剔除 LLM 无法被规则层复核为"超差"的 ``param_out_of_spec`` 结论。

    返回 ``(保留的 findings, 被抑制条目明细)``。**第二项是明细列表而非计数**
    —— 抑制必须留痕（见模块 docstring）。每条明细形如::

        {"finding": <原始 finding dict>,
         "reason": "<非空理由，带命中的三元组>",
         "evidence": {"rule": ..., "states": [...], "matched": [...]}}

    判定规则：

    * 非 ``param_out_of_spec`` 类 finding：原样保留；
    * 文案（description + ocr_text）中能定位到结构化三元组，且所有命中的三元组
      状态均为 ``in``/``soft``（合规、或规则层已独立以 info 呈现的疑似 OCR 丢
      小数点）：抑制（LLM 误报 / 重复条目）；
    * 命中三元组中存在被判**超差**者：保留（真实偏差，规则层已独立佐证）；
    * 定位不到任何三元组，或命中三元组状态为 ``unknown``：保留（fail-closed）。
    """
    index = index_specs(structured)
    kept: list = []
    suppressed: list[dict] = []
    _DROP_SAFE = {"in", "soft"}
    for f in findings:
        if not isinstance(f, dict):
            kept.append(f)
            continue
        if str(f.get("type") or "") not in _SPEC_TYPES:
            kept.append(f)
            continue
        text = f"{f.get('description', '')} {f.get('ocr_text', '')}"
        ntext = _norm(text)
        matched = [
            (name, spec, actual)
            for name, spec, actual in index
            if name and (name in text or _norm(name) in ntext)
        ]
        if not matched:
            kept.append(f)
            continue
        # 用实测值进一步收敛到具体行（同名多行时），但只做"缩小"不做"扩大"：
        # 若文案里出现了某三元组的 actual，则只保留这些更贴合的候选。
        precise = [t for t in matched if str(t[2]) and str(t[2]) in text]
        if precise:
            matched = precise
        pairs = [(n, s, a, _triple_state(s, a)) for n, s, a in matched]
        states = {st for *_, st in pairs}
        if states and states <= _DROP_SAFE:
            suppressed.append({
                "finding": f,
                "reason": _suppression_reason(pairs),
                "evidence": {
                    "rule": "core.rules.spec_guard._triple_state",
                    "states": sorted(states),
                    "matched": [
                        {"name": n, "spec": s, "actual": a, "state": st}
                        for n, s, a, st in pairs
                    ],
                },
            })
            continue
        kept.append(f)
    return kept, suppressed


# ── 抑制留痕（P0-2）────────────────────────────────────────────────────────

_STATE_REASON = {
    "in": "规则层复核为合规",
    "soft": "规则层已以 info 独立呈现同一三元组（疑似 OCR 丢失小数点）",
}


def _suppression_reason(pairs: list[tuple[str, str, str, str]]) -> str:
    """由命中的三元组构造**非空**抑制理由（供法规留痕与人工抽检）。"""
    detail = "；".join(
        f"{name}：实测 {actual or '—'} 对规格 {spec or '—'} → {_STATE_REASON[st]}"
        for name, spec, actual, st in pairs
    )
    return (
        f"LLM 自报超限结论经规则层同一解析器复核后不成立（{detail}），"
        f"按降噪规则抑制；原文保留备查，可回退为正式问题"
    )


# finding_suppressions 的列顺序（stage2 / api 共用同一构造点，避免两处漂移）
SUPPRESSION_INSERT_SQL = (
    "INSERT OR IGNORE INTO finding_suppressions "
    "(job_id, page, type, severity, description, ocr_text, source, reason, evidence, "
    "created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))"
)


def suppression_rows(
    job_id: str, page: int, suppressed: list[dict]
) -> list[tuple]:
    """把抑制明细映射为 ``finding_suppressions`` 行（**唯一**构造点）。

    ``reason`` 必须非空（法规留痕的硬要求）：为空即抛 ``ValueError`` —— 宁可
    让写入路径显式失败，也不允许出现"无理由的抑制"这种不可抽检的记录。
    """
    rows: list[tuple] = []
    for item in suppressed or []:
        f = item.get("finding") if isinstance(item, dict) else None
        if not isinstance(f, dict):
            continue
        reason = str(item.get("reason") or "").strip()
        if not reason:
            raise ValueError(
                "抑制留痕缺失理由（finding_suppressions.reason 不得为空）"
            )
        rows.append((
            str(job_id), int(page), str(f.get("type") or ""),
            str(f.get("severity") or "info"), str(f.get("description") or ""),
            str(f.get("ocr_text") or ""), str(f.get("source") or "llm_page"),
            reason, json.dumps(item.get("evidence") or {}, ensure_ascii=False),
        ))
    return rows
