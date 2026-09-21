"""B1-1 同根因 finding 聚合（降噪）。

## 要解决的问题

单个 OCR 误读会在**每个时间点各产 1 条** finding。真实语料实测
（job `6f80145a-47f`，`devlogs/_verify/probe_b11.py`）：第 8 页「进料压力」列
在 7 个时间点上被测得同一个误读值 `46bar` ⇒ R3 产出 **7 条**同根因 finding
（`core/rules/rule_spec.py` 逐值判定，全链路无聚合层）。复核页被同一条问题的
7 份副本占据，真实问题被淹没。

## 为什么不用描述文本反推聚合键

实测反例：对 `缺少 QA 签名` / `缺少复核人签名` 这类成对条目，若按
"page + type + 描述前 N 字" 分组，会被**误并成一条**（二者根因不同、不可合并）
—— 同一探针报告"10 组可合并"里有 9 组是这类**过度合并**。
⇒ **聚合键必须由判定代码派生**（判定与分组同一来源），不得由描述反推。

## 机制

1. 判定点（`rule_spec._judge_param` / `_judge_cell`）通过 `make_oos_finding`
   **唯一构造点**产出 finding，同时挂上两个**不落库**的派生字段：
   - `AGG_PARTS`：重建文案所需的**结构化部件**（page/主语/取值/规格/后缀/根因）；
   - `AGG_KEY`：由部件派生的分组键（含根因与取值 ⇒ 取值不同者**不会**被并）。
2. `aggregate_by_root_cause()` 按 `AGG_KEY` 分组：**组内 ≥2 才合并**，
   单条**逐字节原样通过**（不得因聚合而改变既有产出）。
3. 合并后：severity 取组内**最高**；描述由**同一** `render_description()`
   用"多时间点"措辞重建（**不做文本裁剪**）；`ocr_text` 拼接各成员明细
   （留痕：每条原始 spec/actual 都还在，可抽检）。

## 留痕

`aggregate_by_root_cause()` 返回 `(out, merged_rows)`，`merged_rows` 是
**明细而非计数**（沿用 `spec_guard.suppression_rows` 的范式）：含组键、成员数、
各成员 severity 与原始 ocr_text，供审计日志与"为什么少了几条"的复核。
"""

from __future__ import annotations

from typing import Any, Iterable

# severity 排序（合并时取最高）。与 `llm_finding_guard._SEV_RANK` 同一约定 ——
# 两处消费者各自持有常量，故在单测里**钉住二者相等**，任一侧漂移即红。
SEVERITY_RANK: dict[str, int] = {"critical": 3, "warning": 2, "info": 1}

#: 分组键（机判派生；下划线前缀 ⇒ 不会被落库路径取用）
AGG_KEY = "_agg_key"
#: 重建文案的结构化部件（同上，不落库）
AGG_PARTS = "_agg_parts"

# 根因标签（**唯一**定义处；聚合键与文案分支都引用它，禁止散落字符串）
CAUSE_UNIT_MISMATCH = "unit_mismatch"
CAUSE_SIGN_UNCERTAIN = "sign_uncertain"
CAUSE_PRINTED = "printed"
CAUSE_EDGE_MARGIN = "edge_margin"
CAUSE_DECIMAL_LOSS = "decimal_loss"
CAUSE_HARD = "hard"

KIND_PARAM = "param"
KIND_CELL = "cell"


def make_oos_finding(
    *,
    page: int,
    type_: str,
    severity: str,
    kind: str,
    subject: str,
    cause: str,
    desc_value: Any,
    raw_value: Any,
    unit: str,
    spec: Any,
    suffix: str = "",
    where: str = "",
) -> dict:
    """R3（规格判定族）finding 的**唯一构造点**。

    ``where`` 仅对矩阵单元格有意义（时间点）；单值参数传空串。
    ``desc_value`` 进描述（历史实现用解析/换算后的数值），``raw_value`` 进
    ``ocr_text``（历史实现用原文，便于人工对照 PDF）。二者**都**进聚合键，
    以杜绝"把不同取值并成一条"。
    """
    parts: dict = {
        "page": page,
        "type": type_,
        "kind": kind,
        "subject": subject,
        "cause": cause,
        "desc_value": desc_value,
        "raw_value": raw_value,
        "unit": unit,
        "spec": spec,
        "suffix": suffix,
        # 位置（时间点）是**变化维度**，故意不进 make_key；但必须随部件携带，
        # 以便聚合时重建清单 —— 避免任何"从描述文本反推"的做法。
        "where": where,
    }
    f = {
        "page": page,
        "type": type_,
        "severity": severity,
        "description": render_description(parts, _where_phrase(kind, [where])),
        "ocr_text": render_ocr_text(parts, where),
        "operator": "",
        "source": "rule",
    }
    f[AGG_PARTS] = parts
    f[AGG_KEY] = make_key(parts)
    return f


def make_key(parts: dict) -> tuple:
    """由**结构化部件**派生分组键（判定与分组同一来源）。

    含 cause / desc_value / raw_value / unit / spec / suffix ⇒
    「同一主语、同一根因、同一取值、同一规格、同一判级尾注」才可能同组。
    组内 severity 允许不同（取最高），因为同根因但手写/印刷来源不同时
    判级会分化，这属于**同一问题的不同严厉度**，应当合并展示。
    """
    return (
        parts.get("page"),
        parts.get("type"),
        parts.get("kind"),
        parts.get("subject"),
        parts.get("cause"),
        str(parts.get("desc_value")),
        str(parts.get("raw_value")),
        str(parts.get("unit")),
        str(parts.get("spec")),
        str(parts.get("suffix")),
    )


def _where_phrase(kind: str, wheres: Iterable[str], *, member_count: int = 1) -> str:
    """位置短语：单元格用"在 X 时"；多成员改写为时间点清单（唯一改写处）。

    ⚠️ 单值参数（``kind=param``）**没有**"时间点"这个维度，所以"共 N 处"的 N
    必须来自 ``member_count``（成员数），**不能**从 ``wheres`` 推 —— 后者的
    空串已被过滤，会恒得 0 ⇒ 该措辞永远发不出来（实测踩过）。
    """
    ws = [w for w in wheres if w not in (None, "")]
    if kind == KIND_CELL:
        if not ws:
            return ""
        if len(ws) == 1:
            return f"在 {ws[0]} 时"
        return f"在 {len(ws)} 个时间点（{'、'.join(ws)}）"
    if member_count <= 1:
        return ""
    return f"（本页共 {member_count} 处同名参数）"


def render_description(parts: dict, where_phrase: str) -> str:
    """文案**唯一**构造点（判定点与聚合点共用 ⇒ 不存在文本裁剪/拼接漂移）。"""
    p = parts.get("page")
    subject = parts.get("subject")
    value = parts.get("desc_value")
    unit = parts.get("unit") or ""
    spec = parts.get("spec")
    suffix = parts.get("suffix") or ""
    type_ = parts.get("type")
    cause = parts.get("cause")
    if type_ == "param_out_of_spec":
        if parts.get("kind") == KIND_CELL:
            return f"第{p}页 {subject} {where_phrase}实测 {value}{unit} 不在规格 {spec} 内{suffix}"
        # 单值参数没有"在 X 时"这种句中位置，故 where_phrase（"共 N 处"）**追加在句末**
        # —— 单条时为 ""，因此单条文案与历史实现逐字节一致。
        return f"第{p}页 参数 {subject}={value}{unit} 不在规格 {spec} 内{suffix}{where_phrase}"
    # spec_unverifiable —— 按根因分支（两条历史文案逐字保留）
    if cause == CAUSE_UNIT_MISMATCH:
        if parts.get("kind") == KIND_CELL:
            return (
                f"第{p}页 {subject} {where_phrase}实测值单位不一致且无换算规则"
                f"（spec={spec}, actual={parts.get('raw_value')}），需人工确认"
            )
        return (
            f"第{p}页 参数 {subject} 单位不一致且无换算规则"
            f"（spec={spec}, actual={parts.get('raw_value')}），需人工确认{where_phrase}"
        )
    if cause == CAUSE_SIGN_UNCERTAIN:
        if parts.get("kind") == KIND_CELL:
            return (
                f"第{p}页 {subject} {where_phrase}实测 {value}{unit} 为负值，"
                f"而规格 {spec} 为上限型正限值，符号约定（真空度/负压）存疑，需人工确认"
            )
        return (
            f"第{p}页 参数 {subject} 实测 {value}{unit} 为负值，"
            f"而规格 {spec} 为上限型正限值，符号约定（真空度/负压）存疑，需人工确认"
            f"{where_phrase}"
        )
    # 未知分支：显式暴露，不静默产出半成品文案
    raise ValueError(f"unsupported finding template: type={type_} cause={cause}")


def render_ocr_text(parts: dict, where: str) -> str:
    """ocr_text 的唯一构造点（保留原文取值，供人工对照 PDF）。"""
    p_spec = parts.get("spec")
    raw = parts.get("raw_value")
    if parts.get("kind") == KIND_CELL:
        return f"{where} {parts.get('subject')}: spec={p_spec} actual={raw}"
    return f"{parts.get('subject')}: spec={p_spec} value={raw}"


def aggregate_by_root_cause(
    findings: list[dict],
) -> tuple[list[dict], list[dict]]:
    """按派生键聚合 R3 同根因条目。

    Returns:
        ``(out, merged_rows)``

        - ``out``: 聚合后的 finding 列表。**未携带 ``AGG_KEY`` 的条目、以及
          组内只有 1 条的条目，逐字节原样通过**（对象同一性不变 ⇒ 不引入
          任何意外改写）。
        - ``merged_rows``: 明细留痕行（组键、成员数、成员 severity 分布、
          成员原始 ocr_text）—— "返回明细而非计数"。
    """
    order: list[Any] = []
    groups: dict[Any, list[dict]] = {}
    passthrough: list[dict] = []          # 无键者：保持原序原对象
    for f in findings:
        key = f.get(AGG_KEY) if isinstance(f, dict) else None
        if not key:
            passthrough.append(f)
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(f)

    out: list[dict] = []
    merged_rows: list[dict] = []
    for f in passthrough:
        out.append(f)
    for key in order:
        members = groups[key]
        if len(members) == 1:
            out.append(members[0])        # 单条：原样（含派生字段，落库路径不取）
            continue
        merged, row = _merge_group(key, members)
        out.append(merged)
        merged_rows.append(row)
    return out, merged_rows


def _merge_group(key: tuple, members: list[dict]) -> tuple[dict, dict]:
    parts = members[0].get(AGG_PARTS) or {}
    # 时间点直接取成员携带的部件（不解析任何文本）。
    where_list = [
        str((m.get(AGG_PARTS) or {}).get("where") or "") for m in members
    ]
    severity = max(
        (str(m.get("severity") or "info") for m in members),
        key=lambda s: SEVERITY_RANK.get(s, 0),
    )
    merged = {
        "page": parts.get("page"),
        "type": parts.get("type"),
        "severity": severity,
        "description": render_description(
            parts, _where_phrase(parts.get("kind"), where_list,
                                 member_count=len(members))
        ),
        "ocr_text": "；".join(
            str(m.get("ocr_text") or "") for m in members
        ),
        "operator": "",
        "source": "rule",
    }
    row = {
        "agg_key": key,
        "page": parts.get("page"),
        "type": parts.get("type"),
        "kind": parts.get("kind"),
        "subject": parts.get("subject"),
        "cause": parts.get("cause"),
        "count": len(members),
        "severities": sorted({str(m.get("severity") or "") for m in members}),
        "members": [str(m.get("ocr_text") or "") for m in members],
        "description": merged["description"],
    }
    return merged, row
