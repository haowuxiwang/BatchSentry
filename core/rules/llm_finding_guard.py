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
from core.finding_quality import (
    is_compliant_claim,
    is_review_check_date_claim,
    is_signature_order_claim,
)

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

#: **布尔/勾选类主张**的统一形态识别（B1-12）。
#:
#: 处置档位**不得由措辞决定**：旧实现只经 `_declared_spec_value`（认「…值为 X」/
#: 引号）取值，于是「参数值为否」(p39/p46) 走 L1 抑制、「勾选了否」(p45) 取不到值
#: ⇒ fail-open ⇒ 只降级 —— 同一件事两种档位，差别只是**用了哪个动词**。
#: 下面两侧分别覆盖「动词 + 了/为 + 是/否」与「值位置 + 是/否」两族写法。
#: ⚠️ `(?![一-龥])` 排除疑问词「是否」本身，避免任何含"是否…"的文案被误判。
_BOOLEAN_CLAIM_RE = re.compile(
    r"(?:勾选|选择|选中|标记|填写|判定|判断)\s*(?:了|为|是|作|的)?\s*"
    r"['\"“”‘’]?\s*([是否])(?![一-龥])"
    r"|(?:实际值|参数值|记录值|实测值|测定值|值)\s*[为是:：]?\s*"
    r"['\"“”‘’]?\s*([是否])(?![一-龥])"
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

#: R132 / TODO 0-74：**手写数字派生硬结论**的降级闸门。
#:
#: 实测（试用批记录 p9）：物料平衡条目「领用数 20 只、使用数 30 只 ⇒ 不平衡」，
#: 原页三格直读是 领用 30 / 使用 30 / 损耗 0（手写「3」被 OCR 读成「2」）。
#: 规则层对**超差**已有同类护栏（0-72），但 LLM 的**跨字段算术结论**没有：
#: 它拿两个手写读数做比较，就把「读数可能错」升级成「记录不平衡」。
#:
#: 只对"结论依赖数值比较"的条目生效；且**只在结构化数据确实把参与比较的
#: 数字标成 handwritten 时**降级（fail-open：无手写证据 ⇒ 不动）。
_HW_NUMERIC_TYPES = frozenset({"mass_balance"})
_COMPARISON_RE = re.compile(
    r"大于|小于|多于|少于|不平衡|不一致|不相等|不等于|超出|不足|超过"
)
_NUM_TOKEN_RE = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])")

#: 指控性措辞（R132 / TODO 0-75）：LLM 用「篡改/伪造/造假」给记录定性，
#: 而它的全部证据常常只是**一个自己也可能读错的 OCR 字面量**（实测 p8
#: 「复核人签名日期 2015.6.03……记录篡改嫌疑」，原页是 2025.06.30）。
#: GMP 场景里"疑似篡改"是能改变放行结论的指控，不得由单条弱证据发出。
_ACCUSATION_RE = re.compile(r"(?:记录)?(?:篡改|伪造|造假|捏造)(?:嫌疑)?")
_ACCUSATION_REASON = (
    "文案含「篡改/伪造/造假」指控，但证据仅为单条 OCR 读数（同一读数可能误读）"
    " ⇒ 改为需人工核对"
)
#: 实测原句的整体替换 —— 比逐词替换读起来通顺，且不保留"明显的日期错误"
#: 这种同样越过证据的定性。
_ACCUSATION_SENTENCE_RE = re.compile(
    r"存在明显的日期错误或(?:记录)?篡改嫌疑"
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


def _declared_boolean_literal(f: dict) -> str | None:
    """文案是否在断言一个**布尔/勾选结果**；是则返回字面量（``"是"``/``"否"``）。

    这是布尔类的**统一类型闸门**（B1-12）：判据是"文案断言了勾选结果"这一**性质**，
    与它用哪个动词无关。

    ⚠️ 为什么不能只靠 :func:`_declared_spec_value`：它认「…值为 X」与引号形态，
    于是「参数值为否」(p39/p46) 能取到值 ⇒ L1 抑制，而「**勾选了否**」(p45)
    取不到值 ⇒ fail-open ⇒ 只降级。**同一件事两种档位，差别只是措辞用了哪个动词。**
    Round 53 已渲染原页核实：p39/p45/p46 三页的「是」框才是有 √ 的那个，
    三处的"否"主张**全是 LLM 读反**（假阳性）⇒ 本就该得到同一种处置。

    ⚠️ 负向预查 ``(?![一-龥])`` 用于排除疑问词「**是否**」本身
    （「是否将柱内甲醇压干」）—— 否则任何含"是否…"的文案都会被误判成布尔主张。
    """
    text = _finding_text(f)
    m = _BOOLEAN_CLAIM_RE.search(text)
    if not m:
        return None
    return m.group(1) or m.group(2)


def _boolean_type_mismatch_reason(literal: str) -> str:
    """布尔类类型错配的**唯一**文案（`_layer_of` 依赖「勾选/布尔形态」这个子串）。"""
    return (
        f"声明的实测值「{literal}」为勾选/布尔形态，不构成数值超差"
        f"（疑似类型错配：应归 completeness/equipment_state 而非 param_out_of_spec）"
    )


def _check_spec_value_shape(f: dict, structured: dict | None) -> str | None:
    """L1（强证据 · 抑制）：`param_out_of_spec` 声明的实测值**形态非法**。

    形态非法 = 判定在逻辑上不成立：
    - 勾选/布尔值（"否"）不是数值 ⇒ 不构成"超出规格范围"（p39/p45/p46 类型错配）
    - 日期值（``2025.01.21``）出现在数值列 ⇒ 列串位（p17）
    - 无任何数字的字面量 ⇒ 不是数值

    「声明的值在结构化抽取中查无此值」属**弱证据**，见
    :func:`_spec_value_unlocatable`（降级而非抑制）。

    返回抑制理由；``None`` 表示不适用或判不了（fail-open）。
    """
    if str(f.get("type") or "") not in _SPEC_TYPES:
        return None
    # ④ 类型错配：布尔/选项值不构成"超出规格范围"。
    # **统一类型闸门（B1-12）必须先于"取值"判定** —— 否则档位由措辞决定（见
    # `_declared_boolean_literal` 的说明）。
    boolean_literal = _declared_boolean_literal(f)
    if boolean_literal is not None:
        return _boolean_type_mismatch_reason(boolean_literal)
    value = _declared_spec_value(f)
    if not value:
        return None  # 取不到值 ⇒ 判不了 ⇒ fail-open
    low = value.strip().lower()
    if low in _BOOLEANISH:
        return _boolean_type_mismatch_reason(value)
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
    """L3：文案声明的**先后方向**与日期事实不符。

    返回 ``(抑制理由, 降级理由)``，两档的判据**不同**：

    - 两侧**同一天**却断言倒置 ⇒ **抑制**（R95/A1）：同日不存在先后，
      判定在逻辑上不成立（用户 2026-10-21 手工标注的口径："时间一致没有问题"）。
      旧实现遇到 ``a == b`` 直接放过，正是 A1 那条自相矛盾 critical 的来源。
    - 两侧日期不同、但声明方向与事实相反 ⇒ **降级**（Round 46）：错的只是
      表述，它指出的日期对本身仍需人工核对。

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
    if a is None or b is None:
        return None, None
    lit_a = "".join(x for x in left[-1] if x)
    lit_b = "".join(x for x in right[0] if x)
    if a == b:
        # R95/A1：两侧**同一天**却断言先后倒置 ⇒ 文案自证其伪，判定不成立。
        # 旧实现遇到 a == b 直接放过（"方向无法判断"），于是
        # 「签名日期（2025.06.03）早于 QA 批准日期（2025.06.03）」这种
        # 自相矛盾的 critical 一路落到复核页。同日不构成倒置 —— 这是用户
        # 2026-10-21 手工标注里明确的口径（"时间一致没有问题"）。
        return (
            f"文案断言「{lit_a} {m.group(0).strip()} {lit_b}」，但两侧是**同一天**"
            f"（{_fmt_date(a)}）：同日不构成先后倒置，该判定不成立"
        ), None
    actual = "after" if a > b else "before"
    if actual == expected:
        return None, None  # 方向自洽 —— 不干预
    if _CURRENT_REF_RE.search(text):
        # 参照物是墙钟 ⇒ 由 _check_current_date_reference 独占处理（此处不重复判定）
        return None, None
    stated = m.group(0).strip()
    corrected = "晚于" if actual == "after" else "早于"
    return None, (
        f"文案声明「{lit_a} {stated} {lit_b}」，但按日期事实 {lit_a} 应为"
        f"「{corrected}」{lit_b} —— 表述方向有误；其所指的两个日期本身请人工核对"
    )



#: 可能承载"签名日期先后"断言的类型。**按内容判定，类型只是防越界的白名单**：
#:   - `signature_time_anomaly`：本裁定最常出现的类型；
#:   - `user_rule`：命中用户规则时的**原始 type**（`source` 由 type 反推，
#:     见 `llm_checks._llm_based_check`）。实测 1619/1620 就是
#:     `user_rule critical` 的签名先后断言 —— 旧实现只认前者，整类漏网；
#:   - `signature_mismatch`：LLM 会把同一条"先后"断言错标成姓名不符。
#:     实测 51 页 `#1562`（p18）与 `#1413` **文案逐字相同**，只有 type 不同
#:     （`signature_mismatch` vs `signature_time_anomaly`）—— 不纳入就会
#:     同一条结论漏一条、留一条。真正的"姓名不符"文案没有先后关系词，
#:     `_is_signature_order_claim` 不会命中（见反向控制用例）。
_ORDER_CLAIM_TYPES = frozenset({
    "signature_time_anomaly", "signature_mismatch", "user_rule",
})


# R102/CR-29：实现已上移到 `core.finding_quality`（叶子模块），此处只留别名，
# 调用点与既有测试不变；**不要再在本地复制一份判据**。
_is_signature_order_claim = is_signature_order_claim


# ⚠️ R111（2026-10-26）：CR-28 的**抑制分支已撤销**，上面这组常量与别名
# 在本模块里已无调用点。**保留**而不是删除，原因有二：
#   ① `tests/unit/test_r102_trash_type.py` 用它守着"判据只有一份实现"这条
#      不变式（`_is_signature_order_claim is is_signature_order_claim`）；
#   ② `devlogs/_verify/r96_mutate.py`、`r97_c1_user_rule_audit.py` 这些
#      历史回放脚本仍按名字引用它们，删了会让旧证据无法复跑。
# 它们不再参与任何判定 —— 别看到"没被调用"就把它们接回判定链。


#: 纯时刻（`14:12` / `14时12分`）。用于识别"两个裸时刻比大小"的断言。
_CLOCK_ONLY_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:时]\s*(\d{2})(?!\d)")

#: 文本里出现**日期或日号**就说明"哪一天"是写了的事实 ⇒ 可判，不属本裁定。
#: ⚠️ 只认 `2025.01.20` / `1月20日` / `18日` 这类；`14时` 不是日期（`时` 不在类里）。
_DATE_MARK_RE = re.compile(r"\d{4}\s*[-./年]|\d{1,2}\s*[月日]")

#: "把两个时刻的先后说成缺陷"的措辞。
_REVERSAL_WORD_RE = re.compile(r"倒置|倒序|颠倒|晚于|早于|矛盾")

#: 可能承载"两个裸时刻先后"断言的类型。`user_rule` 一并覆盖 —— 与 CR-28
#: 同源：用户规则的判定同样是 LLM 产出的，只认 `time_reversal` 会留同一个洞。
_CROSS_MIDNIGHT_TYPES = frozenset({"time_reversal", "user_rule"})


#: "第N页" —— 用来识别**跨页**断言（跨页比较的是两道工序，不属同一工序内部）。
_PAGE_REF_RE = re.compile(r"第\s*(\d+)\s*页")


def _is_cross_midnight_clock_claim(text: str) -> bool:
    """文案是否只是「两个**不带日期**的时刻比大小」（CR-6）。

    为什么这类断言不能成立：批记录里 `14:12 → 08:18`、`23:04 → 00:04` 是
    **跨日生产**的正常写法（含参数矩阵跨午夜继续取样）。缺日期时，"同一天
    倒序"与"跨零点"在结构化数据上**无法区分** —— 两个裸时刻无论谁大，都能
    被"跨了一次零点"解释。既然无法证伪，就不该以缺陷的形式进入待处理列表。

    ⚠️ 只在**文案自身没有日期/日号**时生效。文案写了 `2025-01-20 14:12`
    这类日期 ⇒ 这一天是事实，同一天内"晚于"就是真倒序，照报。
    ⚠️ 只在**同一工序内部**生效。实测 10 页 p6「工序 开始(23:10) 早于第4页
    工序10 开始(13:00)」—— 这是**两道不同工序**的边界，按跨零点解释要绕
    13.8 小时且跨页，不成立。判据用"文案里出现两个不同页码"来识别跨页断言；
    同一工序内部的文案不带页码（`rule_time` 的页内文案只有一个"第N页"）。
    ⚠️ 只在文案里**恰好两个**时刻时生效：三个以上时刻的断言（如"三个点
    都不单调"）不是本形态，交给别的层。
    ⚠️ 实测保留项：p45「结束时间'12时09分'早于开始时间'18日09时07分'」
    带 `18日` ⇒ 不命中（那是"LLM 假设同一天"的另一种失效，不在 CR-6 范围）。
    """
    if not text:
        return False
    if not _REVERSAL_WORD_RE.search(text):
        return False
    if _DATE_MARK_RE.search(text):
        return False
    if len(set(_PAGE_REF_RE.findall(text))) >= 2:
        return False
    clocks = _CLOCK_ONLY_RE.findall(text)
    return len(clocks) == 2


#: 文案里"两个名字被拿来对比"的形态：`王小雨 vs 王涛雨` / `张三与李四`。
#: 只认**紧贴分隔词**的两个 2–4 字中文词，避免把「检查人）与复核人」这类
#: 括号隔开的角色名当成人名。
_NAME_PAIR_RE = re.compile(
    r"([一-龥]{2,4})\s*(?:vs\.?|VS|与|和)\s*([一-龥]{2,4})"
)
#: 括号形态：`检查人（王小雨）与…（王涛雨）` —— 跨页 LLM 习惯把两个名字
#: 分别放进括号、中间隔着角色词，紧贴形态匹配不到（实测 p3 的 llm_cross 版本）。
#: 中间允许 40 字，且遇到句读即止，避免跨句把无关括号配成一对。
_NAME_PAIR_PAREN_RE = re.compile(
    r"[（(]([一-龥]{2,4})[）)][^。；\n]{0,40}?[（(]([一-龥]{2,4})[）)]"
)


def _name_variant_pair(text: str) -> tuple[str, str] | None:
    """文案对比的两个名字是否**仅差一个字**（手写 OCR 变体）。

    实测（试用批记录 p3）：同一栏两次签名被识别为「王小雨」与「王涛雨」，
    被判为"签名姓名不一致"（R95/A4）—— 同一人手写签名在 OCR 里差一个字
    是常态，把这种差异当成"不同签名人"会制造假缺陷。
    """
    pairs = _NAME_PAIR_RE.findall(text) + _NAME_PAIR_PAREN_RE.findall(text)
    for a, b_ in pairs:
        if a == b_ or len(a) != len(b_):
            continue
        if sum(1 for x, y in zip(a, b_) if x != y) == 1:
            return a, b_
    return None


def _check_name_variant_claim(f: dict) -> str | None:
    """L3（弱证据 · 降级）：把"同一人名的 OCR 变体"当成"不同签名人"。

    只降级不抑制 —— 它仍然是"这页签名需要看一眼"的有效线索（也提示 OCR
    在该页不稳定），但不该以"签名人不同"的确定性语气出现。
    """
    text = _finding_text(f)
    if not re.search(r"不一致|不同|不符|差异", text):
        return None
    pair = _name_variant_pair(text)
    if pair is None:
        return None
    return (
        f"两个签名「{pair[0]}」与「{pair[1]}」仅差一个字，属手写体 OCR 变体，"
        f"不构成「不同签名人」的结论 ⇒ 降级为待人工核对"
    )


#: 「日期相同」的自述词。`(?<!不)一致` 排除"不一致"（那是反义）。
_SAME_DAY_RE = re.compile(r"相同|均为|同为|同一天|同日|(?<!不)一致")


def _check_same_day_contradiction(f: dict) -> str | None:
    """L3（强证据 · 抑制）：文案**自述日期相同**却又断言先后倒置。

    与 :func:`_check_declared_order` 的分工：后者比较关系词**两侧**的日期；
    本函数处理"关系词两侧只写了一侧日期、而相同性由另一句话给出"的形态 ——
    实测（试用批记录 p2）："检查人（王小雨）与复核人（蒋俊）签名日期均为
    2025.06.03，但复核人签名日期不应早于检查人签名日期"，关系词右边没有
    日期字面量，`_check_declared_order` 取不到 b 就放过了（R95/A3）。

    **只在能反证时抑制**（fail-open）：文案里出现多个**不同**日期、或出现
    时刻字面量（同日不同时刻的先后是另一回事，本函数判不了）⇒ 不干预。
    """
    text = _finding_text(f)
    if not re.search(r"晚于|迟于|早于|先于", text):
        return None
    if not _SAME_DAY_RE.search(text):
        return None
    if _CLOCK_RE.search(text):
        return None  # 有具体时刻 ⇒ 可能是真实的同日先后问题
    dates = {_parse_date_literal(*g) for g in _DATE_LITERAL_RE.findall(text)}
    dates.discard(None)
    if len(dates) != 1:
        return None  # 没有日期 / 多个不同日期 ⇒ 判不了
    d = next(iter(dates))
    return (
        f"文案自述日期「{_fmt_date(d)}」相同（或全文只出现这一个日期）"
        f"却断言先后倒置 —— 同一天不构成倒置，该判定不成立"
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


def _handwritten_numeric_tokens(structured: dict | None) -> set[str]:
    """该页结构化抽取里**标为手写**的数值字面量集合（R132 / 0-74）。

    ``value_source`` 由抽取器/OCR 低置信占位符共同决定（``core.rules.base``），
    是"这个数字是手写填的"的机器事实；``handwritten`` 是 LLM 标注，只作补充。
    """
    if not isinstance(structured, dict):
        return set()
    out: set[str] = set()

    def _collect(value) -> None:
        for tok in _NUM_TOKEN_RE.findall(str(value or "")):
            out.add(tok)

    for step in structured.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for p in step.get("parameters") or []:
            if isinstance(p, dict) and p.get("value_source") == "handwritten":
                _collect(p.get("value"))
        for m in step.get("measurements") or []:
            if not isinstance(m, dict):
                continue
            for cell in (m.get("values") or {}).values():
                if isinstance(cell, dict) and cell.get("value_source") == "handwritten":
                    _collect(cell.get("actual"))
        for note in step.get("handwritten") or []:
            _collect(note)
    return out


def _check_handwritten_numeric_conclusion(f: dict, structured: dict | None) -> str | None:
    """L3（弱证据 · 降级）：数值比较结论建立在**手写读数**上（R132 / 0-74）。

    实测 p9：LLM 拿「领用 20 / 使用 30」判"物料平衡错误"，而这两个数都是
    手写填写的；原页直读领用是 30。手写数字被 OCR 误读 ⇒ 比较的前提可能
    不成立。此处**只降级、不抑制**：记录是否真的不平衡仍需人工核对原页。
    """
    ftype = str(f.get("type") or "")
    if ftype not in _HW_NUMERIC_TYPES:
        return None
    if not _COMPARISON_RE.search(_finding_text(f)):
        return None
    cited = set(_NUM_TOKEN_RE.findall(_finding_text(f)))
    if not cited:
        return None
    hw = _handwritten_numeric_tokens(structured)
    hit = cited & hw
    if not hit:
        return None
    return (
        f"结论依赖手写数值（{'/'.join(sorted(hit)[:4])}），手写数字 OCR 易误读"
        f"且未获独立复核 ⇒ 降级为需核对，请对照原页确认读数"
    )


def sanitize_accusatory_wording(f: dict) -> dict | None:
    """把 LLM 的指控性措辞改为中性表述（R132 / 0-75）。

    只动 ``source ∈ LLM_SOURCES``：规则层文案是确定性判据，且从不使用
    「篡改/伪造」这类定性。返回**新副本**，``guard_reason`` 记录改写原因
    （不拼进 description —— 与 CR-1 同一条纪律：内部理由不进用户可见正文）。
    """
    if not isinstance(f, dict):
        return None
    if str(f.get("source") or "") not in LLM_SOURCES:
        return None
    desc = str(f.get("description") or "")
    if not _ACCUSATION_RE.search(desc):
        return None
    new_desc = _ACCUSATION_SENTENCE_RE.sub(
        "日期读数可能异常（不排除 OCR 误读），需人工核对", desc,
    )
    new_desc = _ACCUSATION_RE.sub("需人工核对", new_desc)
    nf = dict(f)
    nf["description"] = new_desc.strip()
    # 已有降级理由时**追加**（不覆盖）：措辞改写与"为什么降级"是两件事，
    # 但都要在台账里可回看（GMP 可追溯）。
    _prev = str(nf.get("guard_reason") or "").strip()
    nf["guard_reason"] = f"{_prev}；{_ACCUSATION_REASON}" if _prev else _ACCUSATION_REASON
    return nf


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

    # R111：确定性规则 R9b 已经逐人逐日核对过「检查人 ⇄ 复核人」这一对的页。
    # 那些页上 LLM 常再写一句「复核人签名日期早于检查人签名日期。」—— 无姓名、
    # 无日期，信息量严格小于规则那条，并列展示就是用户 A9 抱怨的重复噪声。
    # 只在**同一页**抑制，且只抑制同一对角色（`is_review_check_date_claim`），
    # 不按类型一刀切 —— 同页其它签名问题（姓名不符、缺日期）照常保留。
    _r9b_pages = {
        f.get("page") for f in findings
        if isinstance(f, dict) and f.get("rule_id") == "R9b"
    }

    for f in findings:
        if not isinstance(f, dict):
            kept.append(f)
            continue
        # R95：**逻辑自证其伪**的文案与来源无关 —— `user_rule` 命中同样是
        # LLM 判定（见 core/rules/llm_checks._user_rules_section），却因为
        # 被当作"确定性判据"而整体绕过了本模块。实测该绕过让 7 条
        # 「日期相同却断言早于」的 critical 直达复核页（A1/A3）。
        # 故本项检查放在来源过滤**之前**，只此一项对所有来源生效。
        _contradiction = _check_same_day_contradiction(f)
        if _contradiction:
            suppressed.append({
                "finding": f,
                "reason": _contradiction,
                "evidence": {
                    "rule": "core.rules.llm_finding_guard",
                    "page": f.get("page"),
                    "type": f.get("type"),
                    "guard_layer": "L3",
                },
            })
            continue
        # ── R111（2026-10-26）：CR-28 的整体抑制**已撤销** ──────────────
        #
        # 历史：R96 曾按用户 2026-10-22 的裁定，把「两处签名日期的先后」
        # **一律抑制**（10 页 1594/1619/1598/1615、51 页 1413/1562）。
        # 该裁定的依据是"同一张表单不同栏目本来就不同步填写"。
        #
        # R111 撤销它，理由是用户 2026-10-26 自己改判：**复核人与检查人的
        # 日期必须一致** —— 这两个角色在同一轮放行里前后脚签，日期不同
        # （早于、晚于都算）就是异常。原裁定把 p7 那条「复核人 2025.06.04
        # 早于检查人 2025.06.21」当成正确行为，恰恰判反了。
        #
        # 撤销后有两件事**必须**同时成立，缺一条就是新的静默：
        # ① 复核⇄检查 这一对不但不抑制，还要**抬到 warning**（见本函数末尾
        #    的 post-pass）—— 确认过的异常不该停在 info 里被忽略；
        # ② 其余签名先后（操作人⇄复核人、复核人⇄批准人…）**恢复原判**，
        #    不再抑制。它们不是"已确认正常"，只是"未裁定"，抑制等于替
        #    用户下结论（R96 之前 `_l3_down` 的降级/年份线索本来就该留着）。
        #
        # ⚠️ 别再退回"按 type 一刀切"：实测 28 条 `signature_time_anomaly` 里
        # 混着「姓名不一致」「签名缺失」「发放日早于起草日」「年份写错」，
        # 按类型处理会一起吞掉。这里的判据始终是**文案内容**。
        # R99/CR-6：**两个不带日期的时刻比大小**不构成可证伪的断言。
        # 实测 p24 `#1426`/`#995`「浓缩开始时间（14:12）晚于结束时间（08:18）」
        # —— 原页是 01-22 14:12 开始、01-23 08:18 结束，跨了一次零点，属正常
        # 跨日生产；规则层（R1-a）已按 CR-6 不再发，LLM 这条必须同样处理，
        # 否则"规则修好了、LLM 那条还在"，复核员看到的仍然是误报。
        # 位置与 CR-28 相同（来源过滤之前），`user_rule` 一并覆盖。
        if str(f.get("type") or "") in _CROSS_MIDNIGHT_TYPES \
                and _is_cross_midnight_clock_claim(str(f.get("description") or "")):
            suppressed.append({
                "finding": f,
                "reason": (
                    "两个不带日期的时刻无法区分「同一天倒序」与「跨零点」"
                    "（跨日生产是正常写法）⇒ 不作为缺陷"
                ),
                "evidence": {
                    "rule": "core.rules.llm_finding_guard",
                    "page": f.get("page"),
                    "type": f.get("type"),
                    "guard_layer": "L3",
                },
            })
            continue
        # R104/P1-6：**自证合规的条目不是缺陷**。
        # 实测 `8df2f568` p9 一条 `self_review`：「……操作人与复核人不同，
        # 符合要求。但需注意……」—— 前半句自己在说没问题，却作为缺陷入库。
        # 危害不是"多一条"，而是复核员会认为**系统把合规项也当缺陷**，
        # 整条流水线的可信度一起下降。
        #
        # 与 A1/CR-28/CR-6 同位置（来源过滤**之前**）：文案自证合规与来源无关，
        # `user_rule` 同样适用（用户规则的产出说"符合要求"= 规则没命中）。
        # 判据在 `core.finding_quality.is_compliant_claim`（纯文案，单点实现）。
        if is_compliant_claim(str(f.get("description") or "")):
            suppressed.append({
                "finding": f,
                "reason": (
                    "该条文案自身结论为合规（如「符合要求」），不构成缺陷 —— "
                    "入库只会让复核员以为系统把合规项也当问题"
                ),
                "evidence": {
                    "rule": "core.finding_quality.is_compliant_claim",
                    "page": f.get("page"),
                    "type": f.get("type"),
                    "guard_layer": "L0",
                },
            })
            continue
        if str(f.get("source") or "") not in LLM_SOURCES:
            kept.append(f)  # 规则层产出：判据来源，不复核
            continue
        page = f.get("page")
        structured = structured_by_page.get(page)
        raw_html = raw_by_page.get(page, "")

        # R111：同一页已由 R9b 给出更具体的结论（含两个姓名与两个日期）⇒
        # 本条是同一事实的重复表述。放在来源闸门**之后**：`user_rule` 是用户
        # 自己写的判据，不替它做去重。
        if page in _r9b_pages and is_review_check_date_claim(
            str(f.get("description") or "")
        ):
            suppressed.append({
                "finding": f,
                "reason": (
                    "同一页的「检查人 ⇄ 复核人」日期不一致已由确定性规则 R9b "
                    "逐人逐日核出（含姓名与两个日期），本条为同一事实的重复表述"
                ),
                "evidence": {
                    "rule": "core.rules.llm_finding_guard",
                    "page": page,
                    "type": f.get("type"),
                    "guard_layer": "R111-dedup",
                    "covered_by": "R9b",
                },
            })
            continue

        # ── 抑制类（强证据 —— 判定在逻辑上不成立）────────────────────
        reason = _check_spec_value_shape(f, structured)              # L1 形态非法
        if reason is None:
            # ① B1-4：异常依据是"记录内日期 vs 当前日期" ⇒ 不构成异常（方向无关）
            reason = _check_current_date_reference(f)
        if reason is None:
            # ② B1-4：前提引用的"生产日期"恰是本文档自述的当前日期 ⇒ 前提不成立
            reason = _check_production_date_premise(f, structured, current_dates)
        if reason is None:
            # ③ R108/P1-5：**"签名必须有日期"是表头说了算的，不是常识说了算**。
            # 实测真实表头只有两种写法：「…|签名」104 张、「执行人签名和日期」8 张。
            # 前者签名栏里就是「操作者郑旭 复核者吴伟胜」，没有日期格可填 ——
            # 系统却报「所有签名均无日期，无法进行时间逻辑检查」（29 条）。
            # 判据在 `core.signature_date_need`（与 blank_column 同一条理由：
            # 表头文案在结构化时就丢了，只有守卫链同时握着 raw_by_page）。
            from core.signature_date_need import unfounded_reason as _sig_date_unfounded

            reason = _sig_date_unfounded(f.get("description"), raw_html)
        if reason is None:
            # ④ R115（用户 2026-10-27）：**有效期是时间段，不是时间点**。
            # 原文自己给出了完整区间（「2025.03.05 至 2026.03.04」），检查日期
            # 落在区间**内** ⇒ "已过期/超期/失效"这类断言被它自己引用的数字证伪。
            # 这不是启发式猜测：区间与落点都在同一句里，是算出来的结论。
            # 只在"整段文案都在说有效期"时抑制 —— 复合文案里的真缺陷不能连坐。
            reason = _validity_interval_contradiction(f)
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
            # 顺序即"理由的具体程度"：名字变体比"推测性表述"更具体，
            # 先说清是哪两个字差在哪，别让通用理由盖住它
            or _check_name_variant_claim(f)        # L3 名字 OCR 变体当"不同人"
            or _check_handwritten_numeric_conclusion(f, structured)  # 0-74 手写读数比较
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

    kept = _raise_review_check_date(kept, downgraded)

    if suppressed or downgraded:
        logger.info(
            "LLM finding 收权复核：抑制 %d 条、改判 %d 条（L1 形态 / L2 溯源 / "
            "L3 判据重算 / L4 severity 封顶 / R111 复核⇄检查抬级）",
            len(suppressed), len(downgraded),
        )
    return kept, downgraded, suppressed


#: 复核⇄检查日期不一致时抬到的目标等级。定在 warning 而不是 critical：
#: 这两个日期不一致**确实是缺陷**（用户 2026-10-26 裁定），但它不像批号不一致
#: 那样能让整批记录失去放行依据 —— 多半是同一天里补签时写错了日期。
_REVIEW_CHECK_TARGET_SEV = "warning"


def _raise_review_check_date(kept: list, downgraded: list) -> list:
    """R111 后置：**复核人 ⇄ 检查人 的签署日期不一致 ⇒ 至少 warning**。

    为什么需要这一步（CR-28 撤销只做了一半）：
    旧实现把这类断言**整条抑制**，撤销抑制后它们会以 LLM 自己给的等级出现 ——
    实测多数是 ``info``。但用户 2026-10-26 的裁定是"这就是问题"：这两个角色
    在同一轮放行里前后脚签，日期**应当相同**（p7 那条「复核人 2025.06.04 早于
    检查人 2025.06.21」正是他点名要报的）。停在 info 里会被复核页折叠，
    等于换一种方式继续静默。

    为什么走 ``downgraded`` 通道而不是就地改 ``kept``：
    ``apply_review`` 是唯一写路径，它 ``out = list(kept)`` 之后再把
    ``downgraded`` 逐条物化（写 ``severity`` 与 ``guard_reason``）。若两边都放，
    同一条 finding 会入库两次。走通道还能把"为什么抬级"落进 ``guard_reason``。

    Args:
        kept: 已通过前面各层的 finding（含规则层产出）。
        downgraded: 就地追加改判记录（本函数会往里面写）。

    Returns:
        过滤后的 ``kept`` —— 命中项已移出，改由 ``downgraded`` 承载。

    ⚠️ **不动 ``source == "rule"``**：确定性层已经自己定了等级，守卫层不替它改判。
    """
    if not kept:
        return kept
    out = []
    for f in kept:
        if (
            isinstance(f, dict)
            and str(f.get("source") or "") != "rule"
            and _SEV_RANK.get(str(f.get("severity") or "info"), 1)
            < _SEV_RANK[_REVIEW_CHECK_TARGET_SEV]
            and is_review_check_date_claim(str(f.get("description") or ""))
        ):
            downgraded.append({
                "finding": f,
                "from_severity": str(f.get("severity") or "info"),
                "to_severity": _REVIEW_CHECK_TARGET_SEV,
                "reason": (
                    "复核人与检查人的签署日期不一致：这两个角色在同一轮放行里"
                    "前后脚签，日期应当相同（用户 2026-10-26 裁定，早于/晚于均为异常）"
                ),
                "rule": "core.rules.llm_finding_guard",
            })
            continue
        out.append(f)
    return out


def _validity_interval_contradiction(f: dict) -> str | None:
    """L3（强证据 · 抑制）：有效期断言被它自己引用的区间**证伪**。

    用户 2026-10-27：「我们 BatchSentry 一定声称有效期是**时间段**
    （比如 2025.11.02-2026.11.01），而不是时间点；检查人所填写的日期会在仪器的
    有效期的时间段里填写 —— 我在 2026.02.06 检查，有效期是
    2025.03.05-2026.03.04，仪器处于有效期。」

    所以「有效期 2025.03.05-2026.03.04，检查日期 2026.02.06，已过期」是
    **可证伪**的：区间与落点都在同一句里，算一下就知道落点在区间内。

    只认 ``inside`` 一种结论。``not_expired``（只给了到期日）**不抑制** ——
    起点未知，检查日期可能早于起点，此时"在有效期内"并未被证明。

    纯度门槛见 :func:`core.finding_quality.is_pure_validity_claim`：
    复合文案（前半句说有效期、后半句说签名缺失）一律不动。
    """
    from core.finding_quality import is_pure_validity_claim, validity_verdict

    desc = str(f.get("description") or "")
    if not is_pure_validity_claim(desc):
        return None
    v = validity_verdict(desc)
    if v is None or v.status != "inside":
        return None
    return (
        f"原文自己给出的有效期区间是 {v.start.isoformat()} 至 {v.end.isoformat()}，"
        f"检查日期 {v.checked.isoformat()} 落在区间**内** ⇒ "
        f"「已过期/超期」不成立（有效期是时间段，检查日期落在其中即处于有效期）"
    )


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
    if "签名栏表头" in reason:
        return "L1-header-date-rule"
    if "有效期区间是" in reason:
        return "L3-validity-interval"
    return "L?"


def apply_review(
    findings: list,
    *,
    structured_by_page: dict | None = None,
    raw_by_page: dict | None = None,
) -> tuple[list, list[dict]]:
    """**落库（写路径）唯一入口**：复核 + 物化降级，返回 ``(final, suppressed)``。

    ``final`` = 保留项 + 已把 ``to_severity`` 写回 ``severity`` 的降级项
    （**不改动入参 dict**，返回新副本）；降级理由写入 ``guard_reason`` 字段
    —— **不拼进 description**（CR-1：那是内部语，不进用户可见文案）。
    ``suppressed`` 与 ``spec_guard.suppression_rows`` 同形，可直接写
    ``finding_suppressions`` 台账。

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
        # CR-1（R97）：降级理由**另存一列**，绝不拼进 description。
        # 理由里全是内部语（"L2 溯源失败"、"LLM 独断 critical"、"v4 prompt 已禁止"），
        # 复核员看的是记录缺陷，不是后端实现。更坏的一种实测形态（p22 `1563`）：
        # 业务正文本身为空，追加后整条只剩理由 —— 用户看到的就是一句后端日志。
        # 结论变更仍可解释（GMP）：理由落 `guard_reason`，审计/复核页可查。
        nf["guard_reason"] = d["reason"]
        out.append(nf)
    # R106/P1-3：**整栏全空 = 抽取不可判**，不是"记录缺失"。
    # 实测 `e50e9a7e` p3：确认栏每一行都是空 `<td></td>`（13 个），LLM 发
    # 「所有设备的'确认'栏均为空白，缺少确认签名」。用户看原页：那一列打了钩 ——
    # 是 OCR 没取到手写勾选。一整列所有行都空是**抽取失败**的形态（人手填一列
    # 极少一行都不填），但纯文本分不出"没填"与"没识别" ⇒ 唯一诚实的结论是不可判。
    # 与 CR-4（`signature_unreadable`）同一条原则：证据不可靠时不下缺陷结论。
    from core.blank_column import retype_blank_column_claims

    out, _blank_changes = retype_blank_column_claims(out, raw_by_page)
    if _blank_changes:
        logger.info(
            "  blank-column retype: %d 条「整栏空白」断言改判为不可判（%s）",
            len(_blank_changes),
            ", ".join(f"p{c['page']}「{c['column']}」×{c['rows']}" for c in _blank_changes[:5]),
        )
    # R107/P1-2：有效期类断言**只给单点日期**（「校验有效期(2025.05.30)」），
    # 复核员容易读成"有效期就是这一天"。原文里没有区间起点就**不编**起点，
    # 只补两件能算出来的事：到期日与检查日期相差多少天、区间起点未识别。
    # 类型层面由 `finding_quality._DESC_TYPE_SIGNALS` 把 `param_out_of_spec`
    # 纠正成 `suspicious_date`（UI 标题「参数超标」→「日期可疑」）。
    from core.finding_quality import validity_overdue_clause

    _vc = 0
    for _i, _f in enumerate(out):
        if not isinstance(_f, dict):
            continue
        _desc = str(_f.get("description") or "")
        if re.search(r"已超出\s*\d+\s*天", _desc):  # 已经补过 / LLM 自己写了天数
            continue
        _clause = validity_overdue_clause(_desc)
        if not _clause:
            continue
        _nf = dict(_f)
        _nf["description"] = _desc.rstrip() + _clause
        out[_i] = _nf
        _vc += 1
    if _vc:
        logger.info("  validity overdue annotated: %d 条（补「已超出 N 天」）", _vc)
    # R132 / TODO 0-75：指控性措辞收口。放在**最后**（降级/改判都已完成）：
    # 无论条目被保留还是降级，用户看到的文案都不得自称"篡改"。
    _acc = 0
    for _i, _f in enumerate(out):
        _nf = sanitize_accusatory_wording(_f)
        if _nf is None:
            continue
        out[_i] = _nf
        _acc += 1
    if _acc:
        logger.info("  accusatory wording neutralized: %d 条（篡改/伪造 → 需人工核对）", _acc)
    return out, suppressed
