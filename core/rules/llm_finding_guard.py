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
- **L3 判据重算**：对有确定性判据的类型，用规则层**同一套代码**重算。
  ⚠️ **Round 46 修正**：两条 L3 路径的结论都**不能把"判不了"当成"判据确凿"**
  （旧版正是这样做的，实测冤枉抑制了 p43/p46 五条文案**明写**「开始时间晚于
  结束时间」的条目）：
  - `time_reversal` 用 `parsing._parse_time_interval`/`_interval_after` 重算，
    结果为**三态** —— 可解析工序均无倒序 ⇒ 抑制；确有倒序 ⇒ 保留；
    **一个可解析工序都没有 ⇒ 降级**（判不了）；
  - 文案声明的**先后方向**（`suspicious_date`/`signature_time_anomaly` 等）：
    两侧**都是记录内的日期** ⇒ **降级**（错的只是表述，日期对本身仍需人工核对）。
- **L3 日期语义**（B1-4，Round 48 新增）：把"日期比较"从 LLM 自由裁量收回来。
  ⚠️ 两条判据都**只否定可证伪的前提、绝不猜真值**（fail-open）：
  - ① `_check_current_date_reference`：比较的**参照物是「当前日期/当前年份」**
    ⇒ 该比较不构成异常（过去日期早于现在本是记录常态）⇒ **抑制**。
    与 Round 46 版的关键差别是**方向无关** —— 旧实现只在"方向反"时抑制，
    于是方向**恰好对**的条目（实测 23 条里 22 条）全部漏网。
    **例外**：记录内日期**晚于**自述当前日期 ⇒ 真未来日期 ⇒ 保留。
  - ② `_check_production_date_premise`：finding 引用「生产日期 X」而 X 恰是
    **文档级单源**解析出的当前日期 ⇒ 把当前日期当成了生产日期 ⇒ **抑制**。
    单源由 `_document_current_dates` **一次解析、全页复用**（实测同一次运行里
    7 页把当前日期当生产日期 ⇒ p28 说"生产日期 2026-09-18"、p38 说"2025年01月20日"
    自相矛盾）。**只做否定、不做选择**：候选里 `2025-01-25`(7 页) 比
    `2025-01-20`(6 页) 还多、该记录本是多子批复合体 ⇒ 无唯一真值，猜真值正是
    本缺陷要根除的失败模式。引用值与**本页**抽取不一致 ⇒ 降级（弱证据）。
- **L4 severity 封顶**：LLM 独断的 `critical`，若无 L3 确定性背书 ⇒ 降为
  `warning` 并注明"未经规则层复核，待人工核对"。规则层的 `critical`
  （source='rule'）不经过本模块，不受影响。
  ⚠️ 本封顶是**无条件**的 ⇒ "LLM 源不残留 critical" 这条断言**无判别力**
  （任何 LLM 输入都不可能有 critical）。验收必须落在"逐条处置是否正确"上，
  不得只看"0 critical"（见 `devlogs/_replay/verify_b14.py`）。

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

#: 比较的**参照物**是否为"当前日期/当前年份" —— 这类比较**本身不构成异常**
#: （过去日期早于现在本是记录常态，B1-4 的根因）。Round 48 起由
#: :func:`_check_current_date_reference` **独占**该语义（Round 46 曾把它放在
#: :func:`_check_declared_order` 里，且只在"方向反"时才抑制 ⇒ 方向**对**的
#: 条目全部漏网，实测 p3/p5/p49/p51 等 17 条）。
#: ⚠️ 刻意**不引入墙钟**：只用文案里出现的词与字面量判定 —— 既避开 B1-7
#: （墙钟基准 ⇒ finding 跨年份不可复现），也避免新增一份必须与规则层同步的
#: 阈值表。**参照物本身就以字面量写在文案里**（LLM 把它当成"今天是……"的依据），
#: 所以compare 两侧都取文案字面量即可，无需知道真实的今天。
_CURRENT_REF_RE = re.compile(
    r"当前日期|当前年份|当前的?日期|当前的?年份|今天|now|current\s+(?:date|year)",
    re.IGNORECASE,
)

#: finding 里**引用「生产日期」**时紧随其后的日期字面量（B1-4 ②：基准值单源）。
#: 允许「生产日期」与日期之间夹少量非数字字符（`生产日期：`/`生产日期为`）。
_CITED_PRODUCTION_DATE_RE = re.compile(
    r"生产日期[^0-9]{0,6}(\d{4})\s*[.\-/年]\s*(\d{1,2})"
    r"(?:\s*[.\-/月]\s*(\d{1,2})\s*日?)?"
)


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


def _check_grounding(
    f: dict,
    raw_html: str,
    exclude_dates: set[tuple[int, int, int]] | None = None,
) -> str | None:
    """L2：文案里的长数字必须能在 OCR 原文中定位。

    返回降级理由；``None`` 表示通过。

    ⚠️ **B2-12：系统注入的值不算"凭空"**。``exclude_dates`` 传文档级自述的
    「当前日期/当前年份」池（:func:`_document_current_dates`）—— 那类值来自
    **系统提示词注入的当天日期**（如 `2026-09-18`），**本来就不该出现在 OCR
    原文里**。旧实现对它们照样扣「疑似提取幻觉」的帽子 ⇒ **降级理由说谎**
    （Round 46 实测 19 条 L2 降级里 14 条属此类）。

    为什么按**年份**排除而不是整条日期：字面量粒度不一（文案常只写
    `当前年份 2026`，池里存的是 `(2026,0,0)`；也可能写 `2026.09.18`，池里是
    `(2026,9,18)`）。只比对年份是**保守方向**的取舍：宁可少扣一次"幻觉"，
    也不要把系统注入值说成编造 —— 这类条目仍会经 L4 封顶留在人工可见档。

    ⚠️ 顺序上有 B1-4 ① 先挡（`_check_current_date_reference` 会**抑制**这类
    条目），故本参数是**防御性**的第二道：一旦 ① 因凑不出两侧字面量而
    fail-open，理由也不会变成假话。回归护栏见
    ``tests/unit/test_llm_finding_guard.py::TestL2GroundingExcludesSystemDates``。
    """
    if not raw_html:
        return None  # 无原文可比 ⇒ fail-open
    pool_years = {"%04d" % v[0] for v in (exclude_dates or set())}
    text = f"{f.get('description') or ''} {f.get('ocr_text') or ''}"
    ungrounded = [
        tok
        for tok in _LONG_NUM_RE.findall(text)
        if not _value_is_grounded(raw_html, tok) and tok[:4] not in pool_years
    ]
    if not ungrounded:
        return None
    shown = ", ".join(ungrounded[:3])
    return f"文案中的数字（{shown}）在 OCR 原文中无法定位，疑似提取幻觉"


# ── L3：判据重算 ───────────────────────────────────────────────────────

def _recompute_time_reversal(structured: dict | None) -> bool | None:
    """规则层同源判据：结构化数据里是否真的存在"开始晚于结束"。

    返回**三态**（Round 46 对抗性审查修正 —— 旧版只返 ``bool``，把
    「判不了」与「确无倒序」混为一谈）：

    - ``True``  — 有可解析样本，且其中确有倒序 ⇒ LLM 结论成立（不抑制）；
    - ``False`` — 有可解析样本，且样本**全部**无倒序 ⇒ **强证据**（可抑制）；
    - ``None``  — **一个可解析样本都没有**（structured 缺失，或该页工序的
      start/end 字段全部解析不出）⇒ **判不了**，fail-open。

    ⚠️ 旧版把 ``None`` 的情形归入 ``False``，于是"该页工序时间一个都读不出"
    被当成"判据确凿地无倒序"而**抑制**。实测 p43（4 条）/p46（1 条）文案
    **明写「开始时间晚于结束时间」**、p38 的串位幻觉条目也走同一条路径 ——
    前者是**真倒序被冤枉抑制**，后者虽结论碰巧正确但**机制是蒙对的**。
    """
    if not structured:
        return None
    fb_date = (structured.get("page_info") or {}).get("production_date")
    samples = 0
    for step in structured.get("steps") or []:
        if not isinstance(step, dict):
            continue
        iv_start = _parse_time_interval(step.get("start_time"), fb_date)
        iv_end = _parse_time_interval(step.get("end_time"), fb_date)
        if not (iv_start and iv_end):
            continue          # 该工序读不出 ⇒ 不构成样本（既不计入、也不作反证）
        samples += 1
        if _interval_after(iv_start, iv_end):
            return True
    return False if samples else None


def _parse_date_literal(y: str, mo: str, d: str | None = None) -> tuple[int, int, int] | None:
    """``(年, 月, 日)``；日缺省记 0（只精确到月的比较）。解析失败返回 None。"""
    try:
        return int(y), int(mo), int(d or 0)
    except (TypeError, ValueError):
        return None


def _date_literals_with_pos(text: str) -> list[tuple[int, tuple[int, int, int], str]]:
    """按出现顺序列出 ``(位置, (年,月,日), 原字面量)``。"""
    out: list[tuple[int, tuple[int, int, int], str]] = []
    for m in _DATE_LITERAL_RE.finditer(text):
        v = _parse_date_literal(*m.groups())
        if v is not None:
            out.append((m.start(), v, "".join(x for x in m.groups() if x)))
    return out


def _date_in_pool(cited: tuple[int, int, int], pool: set[tuple[int, int, int]]) -> bool:
    """``cited`` 能否在某池子里找到同粒度或更细的匹配（B1-4 ② 的归一比较）。

    粒度规则（保守，宁可不匹配也不误伤）：
    - 年必须相等；
    - ``cited`` 有月份时，池内条目也必须有同一月份（**不做"仅记住年份"的通配**）；
    - ``cited`` 有日时，池内条目要么同为该日，要么只精确到月（``日=0``）。
    """
    cy, cm, cd = cited
    for ey, em, ed in pool:
        if cy != ey:
            continue
        if cm and em != cm:
            continue
        if cd and ed not in (0, cd):
            continue
        return True
    return False


#: 紧跟在「当前日期/当前年份」**之后**的那个字面量 —— 允许**只到年**
#: （「当前年份 2026」实测形态）。`_DATE_LITERAL_RE` 要求"年+月"，
#: 会把纯年份整个丢掉 ⇒ ① 对 p3/p12/p25/p37 等页失效。
_REF_SIDE_DATE_RE = re.compile(
    r"^\s*[:：为]?\s*(\d{4})"
    r"(?:\s*[.\-/年]\s*(\d{1,2})(?:\s*[.\-/月]\s*(\d{1,2})\s*日?)?)?"
)


def _ref_side_date(text: str, ref: re.Match) -> tuple[int, int, int] | None:
    """解析「当前日期/当前年份」**紧邻**的那个字面量（月/日缺省记 0）。

    ⚠️ **只取紧邻的那一个**：若扫 ref 之后的*全部*字面量，会把
    「…当前日期2026.09.18，**且早于生产日期2015.01.20**」里的 2015.01.20
    也当成"当前日期"，进而把 p3「生产日期 2015.01.20 早于当前年份 2026」
    以"2015-01-20 是当前日期"这种**错误理由**抑制掉（实测踩过）。
    """
    m = _REF_SIDE_DATE_RE.match(text[ref.end():])
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)
    except (TypeError, ValueError):
        return None


def _not_after(rec: tuple[int, int, int], cur: tuple[int, int, int]) -> bool:
    """``rec`` **不晚于** ``cur``（粒度不明时保守地返回 True = 视作不晚于）。

    粒度不明（参照物只到年、或记录侧只到年）时无法断言"未来"，按 fail-open
    的原则**不认定为未来**（否则会把「2026 年内的记录 vs 当前年份 2026」
    这种噪声当异常留下）。
    """
    ry, rm, rd = rec
    cy, cm, cd = cur
    if ry != cy:
        return ry < cy
    if cm == 0 or rm == 0 or cd == 0 or rd == 0:
        return True
    if rm != cm:
        return rm < cm
    return rd <= cd


def _check_current_date_reference(f: dict) -> str | None:
    """① B1-4：异常依据是「记录内日期 vs **当前日期/当前年份**」⇒ 判定不成立。

    Round 48 新增（并从 :func:`_check_declared_order` 中**移走**该语义 ——
    该判据只允许一处实现）。

    为什么"方向无关"：Round 46 的旧实现只在**声明方向与事实相反**时才抑制，
    于是方向**恰好对**的条目（「生产日期 2025.01.25 早于当前年份 2026.09.18」
    —— 方向对，但"过去日期早于现在"本来就是记录常态、根本不该报）全部漏网；
    实测 23 条当前日期类条目里只有 1 条被旧实现收掉。补上后收掉 15 条。

    **唯一的例外是未来日期**：若记录内日期**晚于**文案自述的当前日期
    （如「2027.01.17 晚于 当前日期 2026.09.18」），那是**真异常**（未来日期
    被写进记录），必须保留 —— 这是本判据的反向控制，不可省。

    判不了就返回 ``None``（fail-open）：文案里凑不出"当前日期字面量 + 记录内
    日期字面量"两侧时（如「起草人签名年份与当前年份不符」根本没给年份、
    「记录发放年份 > 当前年份+1」语序正相反），无从用字面量证伪，一律不干预。
    """
    text = str(f.get("description") or "")
    ref = _CURRENT_REF_RE.search(text)
    if not ref:
        return None
    cur = _ref_side_date(text, ref)
    if cur is None:
        return None
    before = [x for x in _date_literals_with_pos(text) if x[0] < ref.start()]
    if not before:
        return None
    rec = before[-1][1]
    if not _not_after(rec, cur):
        return None  # 记录内日期晚于自述当前日期 ⇒ 真未来日期，保留（反向控制）
    return (
        f"文案以「当前日期/当前年份」为参照物（记录内日期 {_fmt_date(rec)} "
        f"不晚于自述的 {_fmt_date(cur)}）—— 过去日期早于现在本是记录常态，"
        f"该比较不构成异常（依据是墙钟而非记录内部一致性）"
    )


def _document_current_dates(findings: list) -> set[tuple[int, int, int]]:
    """② B1-4：**文档级单源**地解析出"LLM 自述的当前日期"（一次解析、全页复用）。

    这是 B1-4 ② 要的那个"基准值"：LLM 用它当比较基准（`当前日期/当前年份 XXXXX`）。
    真实 51 页实测显示，**同一个基准被逐页各解一次** ⇒ 7 页把它错当成**生产日期**
    （p8/p10/p19/p39/p41/p45/p46 的 `page_info.production_date == '2026-09-18'`），
    于是同一份文档里 p28 说「生产日期 2026-09-18」、p38 说「生产日期 2025年01月20日」
    ⇒ **同一次运行内自相矛盾**。改成文档级单源后，这种引用可以被一致地否定。

    ⚠️ **只做否定、不做选择**：本函数**不尝试**判定"真正的生产日期"是哪一个
    —— 实测候选里 `2025-01-25`（7 页）比 `2025-01-20`（6 页）还多，而这份 51 页
    记录本身很可能是**多子批复合体**（批号后缀 `-04/-05/-06/-07`），本就没有
    唯一真值。**猜真值**正是 B1-4 要根除的失败模式；可复现且可证伪的产物只有
    "哪些值被证明是当前日期"这一集合。
    """
    pool: set[tuple[int, int, int]] = set()
    for f in findings:
        if not isinstance(f, dict):
            continue
        text = str(f.get("description") or "")
        ref = _CURRENT_REF_RE.search(text)
        if not ref:
            continue
        v = _ref_side_date(text, ref)
        if v is not None:
            pool.add(v)
    return pool


def _check_production_date_premise(
    f: dict,
    structured: dict | None,
    current_dates: set[tuple[int, int, int]],
) -> str | None:
    """② B1-4：finding 的**前提**引用了不成立的生产日期 ⇒ 判定不成立。

    两级证据：

    - **强证据（抑制）**：引用的生产日期恰是文档级自述的**当前日期** ⇒ 把当前
      日期当成了生产日期（p28 / p19 实测：「与生产日期 2026-09-18 矛盾」，
      而当前日期正是 `2026-09-18`）。前提直接被证伪。
    - **弱证据（降级）**：引用的生产日期与该页 `page_info.production_date`
      不一致 ⇒ 页内自抽取就自相矛盾（p28 实测：本页基准 `2025.01.25`，
      却被引用成 `2026-09-18`）。保留给人工（可能是抽取层抖动），只降级。

    返回**抑制理由**；降级理由经 :func:`_check_production_date_mismatch` 另行给出。
    """
    cited = _CITED_PRODUCTION_DATE_RE.search(str(f.get("description") or ""))
    if not cited:
        return None
    v = _parse_date_literal(*cited.groups())
    if v is None or not current_dates:
        return None
    if not _date_in_pool(v, current_dates):
        return None
    return (
        f"文案把「{_fmt_date(v)}」当作生产日期，但该值是本文档中 LLM 自述的"
        f"**当前日期**（跨页单源核对）—— 当前日期不可能是生产日期，前提不成立"
    )


def _check_production_date_mismatch(f: dict, structured: dict | None) -> str | None:
    """② B1-4 弱证据：引用的生产日期与该页抽取出的 `production_date` 不一致。"""
    if not structured:
        return None
    cited = _CITED_PRODUCTION_DATE_RE.search(str(f.get("description") or ""))
    if not cited:
        return None
    v = _parse_date_literal(*cited.groups())
    own = _date_literals_with_pos(str((structured.get("page_info") or {}).get("production_date") or ""))
    if v is None or not own:
        return None
    if _date_in_pool(v, {x[1] for x in own}):
        return None
    return (
        f"文案引用的生产日期「{_fmt_date(v)}」与该页抽取出的生产日期"
        f"「{_fmt_date(own[0][1])}」不一致（页内自相矛盾，疑似抽取抖动），请人工核对"
    )


def _fmt_date(v: tuple[int, int, int]) -> str:
    """把 ``(年,月,日)`` 渲染成 ``YYYY[-MM[-DD]]``（缺省成分不补零占位）。"""
    y, m, d = v
    if not m:
        return f"{y:04d}"
    if not d:
        return f"{y:04d}-{m:02d}"
    return f"{y:04d}-{m:02d}-{d:02d}"


def _check_declared_order(f: dict) -> tuple[str | None, str | None]:
    """L3：文案声明的**先后方向**与日期事实相反 ⇒ 表述有误（**只降级**）。

    返回 ``(抑制理由, 降级理由)``；本函数**不再返回抑制理由**（恒为 ``None``），
    保留二元签名是为了让调用点与 :func:`_check_current_date_reference` 对称。

    **Round 46**：旧版把"方向词与事实相反"直接当作"整条 finding 不成立"而抑制，
    实测丢掉了 p38 的「2027.01.17」这条**未来日期**真线索（该页再无条目提及 2027，
    规则层也无兜底）。错的只是 LLM 的**表述**，它指出的日期对本身仍需人工核对
    ⇒ 改为降级并**在理由里纠正方向**。

    **Round 48 划界**：原先本函数还负责"参照物是当前日期 ⇒ 抑制"（按参照物分两档）。
    该语义已**整体移出**到 :func:`_check_current_date_reference` —— 因为旧实现只在
    方向**反**时才抑制，方向对的漏网；而"参照物是墙钟"与"方向反"本是两个独立判据，
    合在一处必然出错。此处现在**只管**"两侧都是记录内日期"的情形。
    """
    text = str(f.get("description") or "")
    m = re.search(
        r"晚于|迟于|早于|先于|after|later\s+than|before|earlier\s+than",
        text, re.IGNORECASE,
    )
    if not m:
        return None, None
    word = re.sub(r"\s+", " ", m.group(0).strip().lower())
    expected = _ORDER_WORDS.get(word)
    if expected is None:
        return None, None
    left = _DATE_LITERAL_RE.findall(text[: m.start()])
    right = _DATE_LITERAL_RE.findall(text[m.end():])
    if not left or not right:
        return None, None
    a = _parse_date_literal(*left[-1])   # 关系词**左侧最近**的日期
    b = _parse_date_literal(*right[0])   # 关系词**右侧最近**的日期
    if a is None or b is None or a == b:
        return None, None
    actual = "after" if a > b else "before"
    if actual == expected:
        return None, None  # 方向自洽 —— 不干预
    if _CURRENT_REF_RE.search(text):
        # 参照物是墙钟 ⇒ 由 _check_current_date_reference 独占处理（此处不重复判定）
        return None, None
    lit_a = "".join(x for x in left[-1] if x)
    lit_b = "".join(x for x in right[0] if x)
    stated = m.group(0).strip()
    corrected = "晚于" if actual == "after" else "早于"
    return None, (
        f"文案声明「{lit_a} {stated} {lit_b}」，但按日期事实 {lit_a} 应为"
        f"「{corrected}」{lit_b} —— 表述方向有误；其所指的两个日期本身请人工核对"
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

        - ``kept``: 原样保留的 finding（含被 L3 证实者与未经本模块判定者）。
          ⚠️ 旧 docstring 提到的 ``_guard_corroborated`` 标记**从未实现** ——
          Round 46 删除该描述，以免文档承诺一个不存在的字段。
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

    # B1-4 ②：**文档级单源**解析一次，全页复用（禁止各页各解 —— 那正是缺陷本身）。
    current_dates = _document_current_dates(findings)

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
        if reason is None:
            # ① B1-4：异常依据是"记录内日期 vs 当前日期" ⇒ 不构成异常（方向无关）
            reason = _check_current_date_reference(f)
        if reason is None:
            # ② B1-4：前提引用的"生产日期"恰是本文档自述的当前日期 ⇒ 前提不成立
            reason = _check_production_date_premise(f, structured, current_dates)
        _l3_down = None
        if (
            reason is None
            and str(f.get("type") or "") == "time_reversal"
            and _REVERSAL_TEXT_RE.search(_finding_text(f))           # 仅当确实在断言倒序
        ):
            _rec = _recompute_time_reversal(structured)              # L3 同源重算（三态）
            if _rec is False:
                reason = (
                    "规则层用同源判据复核该页结构化数据（可解析工序均无倒序），"
                    "未发现时间倒序（LLM 结论无据，疑似跨行/跨字段串位）"
                )
            elif _rec is None:
                # Round 46：**判不了 ≠ 判据确凿**。旧版把"一个工序都解析不出"
                # 当成"确无倒序"而抑制，实测 p43×4 / p46 的文案**明写**
                # 「开始时间晚于结束时间」却被冤枉抑制。
                _l3_down = (
                    "该页结构化数据中没有可解析的工序时间，规则层无法复核该结论"
                    "（判不了 ⇒ 保留待人工核对，不作抑制）"
                )
        if reason is None:
            # L3 方向重算 —— Round 48 起只管"两侧都是记录内日期"的情形
            # （参照物是墙钟的已由 ① 独占处理）。
            _sup2, _down2 = _check_declared_order(f)
            reason = _sup2
            _l3_down = _l3_down or _down2
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
            _check_grounding(                      # L2 溯源（完全凭空）
                f, raw_html, exclude_dates=current_dates
            )
            or _spec_value_unlocatable(f, structured)   # L1 查无此值（弱证据）
            or _check_speculative(f)               # L1' 推测性表述
            or _check_production_date_mismatch(f, structured)  # ② 页内基准不一致
            or _l3_down                            # L3 判不了 / 方向反但日期对属记录内
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
    if "该值是本文档中 LLM 自述的" in reason:
        return "L3-date-baseline"
    if "该比较不构成异常" in reason:
        return "L3-current-date"
    if "表述方向有误" in reason:
        return "L3-declared-order"
    return "L?"


def apply_review(
    findings: list,
    *,
    structured_by_page: dict | None = None,
    raw_by_page: dict | None = None,
) -> tuple[list, list[dict]]:
    """**落库（写路径）唯一入口**：复核 + 物化降级，返回 ``(final, suppressed)``。

    ``final`` = 保留项 + 已把 ``to_severity`` 写回 ``severity`` 的降级项
    （**不改动入参 dict**，返回新副本）；降级理由以 ``｜理由`` 追加到 description
    供复核者阅读（GMP：结论变更须可解释）。``suppressed`` 与
    ``spec_guard.suppression_rows`` 同形，可直接写 ``finding_suppressions`` 台账。

    ⚠️ **不要在调用方重写这段"重建 + 降级物化"逻辑** —— stage2/stage3 曾各自
    手抄一份，且与这里**已经漂移**（本函数对 description 做了 ``.rstrip()``，
    两处调用方没有），属 B3-4 登记的可维护性缺陷；机检
    ``tests/unit/test_llm_guard_single_impl.py`` 会抓住再复制。
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
