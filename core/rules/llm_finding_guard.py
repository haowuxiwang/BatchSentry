"""LLM finding 收权复核（B1-6）—— LLM 只"取值"，判定由确定性代码裁决。

背景（Round 43 视觉直读原页实测）
--------------------------------
LLM 同时做了两件它不该单独做的事：**自己从 OCR 文本里挑值** + **自己下判定**。
由此产生五类假阳性，根因是同一个：

| 形态 | 实例（真实 51 页 e2e） | 该形态的确定性反证 |
|---|---|---|
| ① 日期方向反 | p2「审核 2025.01.30 **晚于** 当前 2026.09.18」 | 2025 < 2026 ⇒ 声明的方向与事实相反 |
| ② 列串位 | p17「P3 实际值为 `'A'`」／「F3 为 `'2025.01.21'`」 | 声明的值在结构化抽取里查无此值 |
| ③ 跨字段串位 | p6「接收结束 `13:6` 早于开始 `09:00`」 | 结构化里 `09:00 → 09:52`，根本不存在倒序 |
| ④ 类型错配（勾选读反） | p39「参数值为**否**，不符合规格范围」 | 布尔值不是数值，不构成"超差" |
| ⑤ 凭空数字 | p6「操作者签名日期 `18222`」 | 该数字不在 OCR 原文中 |

对策：把"判定"从 LLM 手里收回到规则层 —— LLM findings 落库前过四层复核。
**再调提示词是治标**（LLM 的输出形态不可穷举，而判据是确定的）。

四层复核
--------
- **L1 值形态合法性**：`param_out_of_spec` 声明的"实测值"必须是数值形态，
  且必须能在该页结构化抽取的 `(名称, spec, actual)` 三元组里定位到。
  非法形态 / 查无此值 ⇒ **抑制**（判定在逻辑上不成立）。
- **L2 溯源可判性**：finding 文案里的长数字必须能在 OCR 原文中定位（复用
  `page_analyzer._value_grounded` 同源实现）。不可定位 ⇒ **降级**
  （疑似幻觉，保留给人工看，但不给 critical）。
- **L3 判据重算**：对有确定性判据的类型，用规则层**同一套代码**重算：
  `time_reversal` 用 `parsing._parse_time_interval`/`_interval_after` 重算；
  `suspicious_date`/`signature_time_anomaly` 核对话里声明的先后方向。
  重算不出该结论（或无据）⇒ **抑制**；重算证实 ⇒ 标记 `corroborated`。
- **L4 severity 封顶**：LLM 独断的 `critical`，若无 L3 确定性背书 ⇒ 降为
  `warning` 并注明"未经规则层复核，待人工核对"。规则层的 `critical`
  （source='rule'）不经过本模块，不受影响。

设计原则（工程约束）
--------------------
- **绝不删除**：抑制一律走调用方的 `finding_suppressions` 台账（留 reason +
  evidence，可回退、可抽检）。本模块只**产出明细**，不碰 DB。
- **fail-open**：判不了的一律保留。本模块只做"有反证才降级/抑制"，
  绝不因"没证据支持"而丢条目 —— 漏检比误报在 GMP 场景代价更高。
- **纯函数 / 零 LLM 调用 / 零 IO**：可用真实数据离线回放验收（见
  `devlogs/_replay/`），也可被变异验证直接打断言。

调用方
------
- `core/pipeline/stage2.py`（`source='llm_page'`，单页）
- `core/pipeline/stage3.py`（`source='llm_cross'/'llm_fallback'`，跨页）
"""
from __future__ import annotations

import logging
import re

from core.rules.parsing import (
    _interval_after,
    _parse_number,
    _parse_time_interval,
)
from core.rules.spec_guard import _SPEC_TYPES, index_specs

logger = logging.getLogger(__name__)

#: 本模块复核的 finding source（规则层 rule/user_rule 不在此列 —— 它们是
#: 确定性产出，本身就是判据来源，不需要被复核）。
LLM_SOURCES = frozenset({"llm_page", "llm_cross", "llm_fallback"})

_SEV_RANK = {"critical": 3, "warning": 2, "info": 1}

# ── L1：值形态 ─────────────────────────────────────────────────────────

# 引号内的值：LLM 常写成 实际值为 'A' / “否”
_QUOTED_VALUE_RE = re.compile(r"['\"“”‘’]([^'\"“”‘’]{1,40})['\"“”‘’]")
# 无引号形态：「参数值为否，不符合规格范围」/「实际值为否」
_BARE_VALUE_RE = re.compile(r"(?:实际值|参数值|值)\s*[为是:：]?\s*([^\s,，。；;、）)]{1,24})")
# 日期/时间形态 —— 出现在"实测值"位置即为类型错配（列串位）
_DATEISH_RE = re.compile(r"\d{4}\s*[.\-/年]\s*\d{1,2}")
# 布尔/选项形态（勾选类），非数值
_BOOLEANISH = frozenset({"是", "否", "n/a", "na", "无法识别", "√", "×", "☑", "☐"})

#: 值位置的**评价词** —— 它们是判据措辞，不是实测值。提取器若把
#: 「T2101a~d 压力值超出规格范围」的"超出规格范围"当成实测值，会误判为
#: 非数值形态而抑制一条**真超差**（p9 实测）。命中即视为"取不到值"（fail-open）。
_NOT_A_VALUE = (
    "超出规格", "不符合规格", "超出范围", "不符合范围", "低于规格", "高于规格",
    "不符合标准", "不符合要求", "超差", "超标", "超限", "无法识别", "格式异常",
    "不符合操作指导", "未在规格",
)

#: time_reversal 的**倒序语义**词表 —— 只有文案确实在断言"结束早于开始"时，
#: 才用规则层的倒序判据重算。否则（如"时间重复"）是另一类主张，不得借倒序
#: 判据否定它（p22 实测误杀）。
_REVERSAL_TEXT_RE = re.compile(
    r"晚于|迟于|早于|先于|倒序|逆序|颠倒|after|later\s+than|before|earlier\s+than",
    re.IGNORECASE,
)

#: **推测性/不确定表述** —— LLM"没看清"时的措辞。
#:
#: 这类文案不能作为异常结论：「识别不出」与「记录有问题」是两件事。v4 prompt
#: 的「完整性检查规范」已明令禁止输出「无法准确识别」「可能缺失」「疑似遗漏」
#: 等表述，但 LLM 仍会输出（实测 p6：「操作者签名日期 18222 无法识别」被报为
#: finding，且原文里那些数字是 OCR 粘连产物）⇒ **提示词约束不住，只能代码收权**。
_SPECULATIVE_RE = re.compile(
    r"无法识别|无法判断|无法确定|无法确认|识别不清|未能识别|识别失败|"
    r"疑似|可能|或许|不确定|不清晰|模糊|看不"
)

#: 声明先后关系的词表 → 期望的真值关系（"a <词> b" 断言 a 与 b 的关系）
_ORDER_WORDS = {
    "晚于": "after", "迟于": "after", "after": "after", "later than": "after",
    "早于": "before", "先于": "before", "before": "before", "earlier than": "before",
}
# 日期字面量（支持 2025.01.30 / 2025-01-30 / 2025年01月20日 / 2025.01）
_DATE_LITERAL_RE = re.compile(
    r"(\d{4})\s*[.\-/年]\s*(\d{1,2})(?:\s*[.\-/月]\s*(\d{1,2})\s*日?)?"
)
# 时刻字面量 HH:MM（用于 time_reversal 的文案复核）
_CLOCK_RE = re.compile(r"(\d{1,2})\s*[:时]\s*(\d{1,2})")


def _finding_text(f: dict) -> str:
    """finding 的可读文本（描述 + 原文摘录），判据解析统一入口。"""
    return f"{f.get('description') or ''} {f.get('ocr_text') or ''}"


def _plain_text(raw_html: str) -> str:
    """去 HTML 标签 —— grounding 用完整原始文本，不受发给 LLM 的截断影响。"""
    return re.sub(r"<[^>]+>", " ", raw_html or "")


def _grounding_tools(raw_html: str):
    """惰性取 grounding 工具（复用 page_analyzer 同源实现，避免第二份漂移）。"""
    from core.page_analyzer import (  # 局部导入：避免 rules 包在 import 期耦合
        _normalize_grounding_text,
        _normalize_preserving_tokens,
    )

    plain = _plain_text(raw_html)
    return _normalize_grounding_text(plain), _normalize_preserving_tokens(plain)


def _value_is_grounded(raw_html: str, value: str) -> bool:
    """value 的数字分量能否在 OCR 原文中定位（复用 page_analyzer 判据）。"""
    from core.page_analyzer import _value_grounded

    text, tokens = _grounding_tools(raw_html)
    return _value_grounded(text, value, tokens)


def _declared_spec_value(f: dict) -> str | None:
    """从 LLM 文案里取出它声明的"实测值"字面量。

    取不到、或取到的其实是**判据措辞**（"超出规格范围"等评价词）时返回
    ``None`` —— 后者不是"值形态非法"，而是"本模块无从判断"，必须 fail-open
    （否则会把一条真超差当作"非数值"抑制掉，p9 实测）。
    """
    text = f"{f.get('description') or ''} {f.get('ocr_text') or ''}"
    value = None
    m = _QUOTED_VALUE_RE.search(text)
    if m:
        value = m.group(1).strip()
    else:
        m = _BARE_VALUE_RE.search(text)
        if m:
            value = m.group(1).strip()
    if not value:
        return None
    if any(w in value for w in _NOT_A_VALUE):
        return None
    return value


def _check_spec_value_shape(f: dict, structured: dict | None) -> str | None:
    """L1（强证据 · 抑制）：`param_out_of_spec` 声明的实测值**形态非法**。

    形态非法 = 判定在逻辑上不成立：
    - 勾选/布尔值（"否"）不是数值 ⇒ 不构成"超出规格范围"（p39 类型错配）
    - 日期值（``2025.01.21``）出现在数值列 ⇒ 列串位（p17）
    - 无任何数字的字面量 ⇒ 不是数值

    「声明的值在结构化抽取中查无此值」属**弱证据**，见
    :func:`_spec_value_unlocatable`（降级而非抑制）。

    返回抑制理由；``None`` 表示不适用或判不了（fail-open）。
    """
    if str(f.get("type") or "") not in _SPEC_TYPES:
        return None
    value = _declared_spec_value(f)
    if not value:
        return None  # 取不到值 ⇒ 判不了 ⇒ fail-open
    low = value.strip().lower()
    # ④ 类型错配：布尔/选项值不构成"超出规格范围"
    if low in _BOOLEANISH:
        return (
            f"声明的实测值「{value}」为勾选/布尔形态，不构成数值超差"
            f"（疑似类型错配：应归 completeness/equipment_state 而非 param_out_of_spec）"
        )
    # ② 列串位：日期形态出现在"实测值"位置
    if _DATEISH_RE.search(value):
        return (
            f"声明的实测值「{value}」为日期形态，不是数值"
            f"（疑似列串位：日期被串进数值列）"
        )
    # 纯非数值（无任何数字）
    if _parse_number(value) is None:
        return f"声明的实测值「{value}」非数值形态，超差判定不成立（疑似列串位）"
    return None


def _spec_value_unlocatable(f: dict, structured: dict | None) -> str | None:
    """L1（弱证据 · 降级）：声明的实测值在该页结构化抽取中查无此值。

    LLM 判"超差"所依据的值不存在于记录（p17 的 ``'2025.01.21'``、p26 的
    ``45.6°C``）—— 但这**也可能是抽取层丢了值**，故只降级（保留给人工看），
    不抑制：GMP 场景漏检代价高于误报。
    """
    if str(f.get("type") or "") not in _SPEC_TYPES:
        return None
    if not structured:
        return None
    value = _declared_spec_value(f)
    if not value or _parse_number(value) is None:
        return None  # 形态非法的已由 L1 强证据分支处理
    actuals = {
        str(a).strip().replace(" ", "")
        for _n, _s, a in index_specs(structured) if str(a).strip()
    }
    if not actuals:
        return None
    v = value.replace(" ", "")
    if v in actuals or any(v in a for a in actuals):
        return None
    return (
        f"声明的实测值「{value}」在该页结构化抽取中查无此值"
        f"（判定所依据的值不在记录里，疑似串位/幻觉，请人工核对原页）"
    )


# ── L2：溯源（grounding）────────────────────────────────────────────────

#: 需要溯源的数字最小长度 —— 与 page_analyzer._GROUNDING_MIN_DIGITS 同源口径。
#: 短数字（页码/年份）在原文中大量巧合命中，不具区分度。
_LONG_NUM_RE = re.compile(r"\d{4,}(?:\.\d+)?")


def _check_grounding(f: dict, raw_html: str) -> str | None:
    """L2：文案里的长数字必须能在 OCR 原文中定位。

    返回降级理由；``None`` 表示通过。
    """
    if not raw_html:
        return None  # 无原文可比 ⇒ fail-open
    text = f"{f.get('description') or ''} {f.get('ocr_text') or ''}"
    ungrounded = [
        tok for tok in _LONG_NUM_RE.findall(text) if not _value_is_grounded(raw_html, tok)
    ]
    if not ungrounded:
        return None
    shown = ", ".join(ungrounded[:3])
    return f"文案中的数字（{shown}）在 OCR 原文中无法定位，疑似提取幻觉"


# ── L3：判据重算 ───────────────────────────────────────────────────────

def _recompute_time_reversal(structured: dict | None) -> bool:
    """规则层同源判据：结构化数据里是否真的存在"开始晚于结束"。"""
    if not structured:
        return False
    fb_date = (structured.get("page_info") or {}).get("production_date")
    for step in structured.get("steps") or []:
        if not isinstance(step, dict):
            continue
        iv_start = _parse_time_interval(step.get("start_time"), fb_date)
        iv_end = _parse_time_interval(step.get("end_time"), fb_date)
        if iv_start and iv_end and _interval_after(iv_start, iv_end):
            return True
    return False


def _parse_date_literal(y: str, mo: str, d: str | None = None) -> tuple[int, int, int] | None:
    """``(年, 月, 日)``；日缺省记 0（只精确到月的比较）。解析失败返回 None。"""
    try:
        return int(y), int(mo), int(d or 0)
    except (TypeError, ValueError):
        return None


def _check_declared_order(f: dict) -> str | None:
    """L3：核对文案里声明的先后关系是否与日期事实一致。

    典型反例 p2「2025.01.30 晚于 2026.09.18」—— 2025 < 2026，声明方向与
    事实相反；p38「2027.01.17 早于 2025年01月20日」同理。返回抑制理由；
    ``None`` 表示方向自洽或无法判定（fail-open）。
    """
    text = str(f.get("description") or "")
    m = re.search(
        r"晚于|迟于|早于|先于|after|later\s+than|before|earlier\s+than",
        text, re.IGNORECASE,
    )
    if not m:
        return None
    word = re.sub(r"\s+", " ", m.group(0).strip().lower())
    expected = _ORDER_WORDS.get(word)
    if expected is None:
        return None
    left = _DATE_LITERAL_RE.findall(text[: m.start()])
    right = _DATE_LITERAL_RE.findall(text[m.end():])
    if not left or not right:
        return None
    a = _parse_date_literal(*left[-1])   # 关系词**左侧最近**的日期
    b = _parse_date_literal(*right[0])   # 关系词**右侧最近**的日期
    if a is None or b is None or a == b:
        return None
    actual = "after" if a > b else "before"
    if actual == expected:
        return None  # 方向自洽 —— 不干预
    lit_a = "".join(x for x in left[-1] if x)
    lit_b = "".join(x for x in right[0] if x)
    return (
        f"文案声明「{lit_a} {m.group(0).strip()} {lit_b}」，"
        f"但按日期事实 {lit_a} 应为「{'晚于' if actual == 'after' else '早于'}」{lit_b}"
        f" —— 判据方向与事实相反"
    )


def _check_speculative(f: dict) -> str | None:
    """L1'（弱证据 · 降级）：文案属**推测性表述**，不构成异常结论。

    「识别不出」与「记录有问题」是两件事（p6「签名日期 18222 无法识别」实测）。
    降级而非抑制 —— 它仍是有价值的人工核对线索，只是不该以 critical/warning
    的确定性语气出现。
    """
    m = _SPECULATIVE_RE.search(_finding_text(f))
    if not m:
        return None
    return (
        f"文案为推测性表述（命中「{m.group(0)}」），不能作为异常结论"
        f"（v4 prompt 已禁止此类表述）⇒ 降级为待人工核对"
    )


# ── 主入口 ─────────────────────────────────────────────────────────────

def review_llm_findings(
    findings: list,
    *,
    structured_by_page: dict | None = None,
    raw_by_page: dict | None = None,
) -> tuple[list, list[dict], list[dict]]:
    """对 LLM 产出的 findings 做四层复核（收权）。

    Args:
        findings: LLM 生成的 finding dict 列表（source ∈ ``LLM_SOURCES``）。
            非 LLM source 的条目原样保留（规则层是判据来源，不被复核）。
        structured_by_page: ``{page: structured_dict}`` —— 页级抽取结果，
            供 L1 定位实测值、L3 重算判据。
        raw_by_page: ``{page: raw_html}`` —— OCR 原文，供 L2 溯源。

    Returns:
        ``(kept, downgraded, suppressed)``

        - ``kept``: 原样保留的 finding（含被 L3 证实者，附
          ``_guard_corroborated=True`` 标记，仅供调用方观察，不入库）。
        - ``downgraded``: ``[{"finding", "from_severity", "to_severity",
          "reason", "rule"}]`` —— 保留但 severity 降低。
        - ``suppressed``: ``[{"finding", "reason", "evidence"}]`` ——
          与 ``spec_guard.suppression_rows`` 同形，可直接写抑制台账。
    """
    structured_by_page = structured_by_page or {}
    raw_by_page = raw_by_page or {}
    kept: list = []
    downgraded: list[dict] = []
    suppressed: list[dict] = []

    for f in findings:
        if not isinstance(f, dict):
            kept.append(f)
            continue
        if str(f.get("source") or "") not in LLM_SOURCES:
            kept.append(f)  # 规则层产出：判据来源，不复核
            continue

        page = f.get("page")
        structured = structured_by_page.get(page)
        raw_html = raw_by_page.get(page, "")

        # ── 抑制类（强证据 —— 判定在逻辑上不成立）────────────────────
        reason = _check_spec_value_shape(f, structured)              # L1 形态非法
        if (
            reason is None
            and str(f.get("type") or "") == "time_reversal"
            and _REVERSAL_TEXT_RE.search(_finding_text(f))           # 仅当确实在断言倒序
            and structured
            and not _recompute_time_reversal(structured)             # L3 同源重算
        ):
            reason = (
                "规则层用同源判据复核该页结构化数据，未发现时间倒序"
                "（LLM 结论无据，疑似跨行/跨字段串位）"
            )
        if reason is None:
            reason = _check_declared_order(f)                        # L3 方向重算
        if reason is not None:
            suppressed.append({
                "finding": f,
                "reason": reason,
                "evidence": {
                    "rule": "core.rules.llm_finding_guard",
                    "page": page,
                    "type": f.get("type"),
                    "guard_layer": _layer_of(reason),
                },
            })
            continue

        # ── 降级类（弱证据 —— 保留给人工，但不给高严重度）────────────
        sev = str(f.get("severity") or "info")
        weak = (
            _check_grounding(f, raw_html)          # L2 溯源（完全凭空）
            or _spec_value_unlocatable(f, structured)   # L1 查无此值（弱证据）
            or _check_speculative(f)               # L1' 推测性表述
        )
        to_sev = None
        downgrade_reason = weak
        if sev == "critical":
            # L4 封顶：LLM 的 critical 没有规则层确定性背书 ⇒ 不给 critical
            to_sev = "warning"
            if not downgrade_reason:
                downgrade_reason = (
                    "LLM 独断 critical，未经规则层确定性判据复核 ⇒ 降为 warning 待人工核对"
                )
        elif sev == "warning" and weak:
            to_sev = "info"   # 弱证据命中的 warning 再降一档（保留可见，去误导）
        if to_sev is not None and _SEV_RANK.get(to_sev, 2) < _SEV_RANK.get(sev, 1):
            downgraded.append({
                "finding": f,
                "from_severity": sev,
                "to_severity": to_sev,
                "reason": downgrade_reason,
                "rule": "core.rules.llm_finding_guard",
            })
            continue

        kept.append(f)

    if suppressed or downgraded:
        logger.info(
            "LLM finding 收权复核：抑制 %d 条、降级 %d 条（L1 形态 / L2 溯源 / "
            "L3 判据重算 / L4 severity 封顶）",
            len(suppressed), len(downgraded),
        )
    return kept, downgraded, suppressed


def _layer_of(reason: str) -> str:
    """从理由文案反推复核层级（供审计 evidence 标记）。"""
    if "勾选/布尔形态" in reason or "日期形态" in reason or "非数值形态" in reason:
        return "L1-value-shape"
    if "时间倒序" in reason:
        return "L3-time-reversal"
    if "方向与事实相反" in reason:
        return "L3-declared-order"
    return "L?"


def apply_review(
    findings: list,
    *,
    structured_by_page: dict | None = None,
    raw_by_page: dict | None = None,
) -> tuple[list, list[dict]]:
    """便捷封装：落库前的最终 findings 列表 + 抑制明细。

    返回 ``(final_findings, suppressed)`` —— ``final_findings`` 已应用降级后的
    severity（不改动入参 dict，返回新副本）。
    """
    kept, downgraded, suppressed = review_llm_findings(
        findings, structured_by_page=structured_by_page, raw_by_page=raw_by_page,
    )
    out = list(kept)
    for d in downgraded:
        nf = dict(d["finding"])
        nf["severity"] = d["to_severity"]
        # 降级理由对复核者可见（GMP：结论变更须可解释）
        note = (nf.get("description") or "").rstrip()
        nf["description"] = f"{note}｜{d['reason']}"
        out.append(nf)
    return out, suppressed
