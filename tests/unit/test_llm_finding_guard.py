"""core/rules/llm_finding_guard.py 单测（B1-6 收权复核）。

样本形态**全部取自 Round 43 的 51 页真实 e2e 实测**（见 docs/ADVERSARIAL_AUDIT.md
§15 与 devlogs/_replay/），不是构造出来的假想用例 —— 每个 ``pXX`` 都对应真页。

判别力说明（变异验证）：若把 ``review_llm_findings`` 改成直接原样返回
（no-op），下列全部断言都会红；反之若把某条判据放宽（如允许评价词当实测值），
对应的"防误杀"用例会红。两条方向都被守住。
"""
from __future__ import annotations

from core.rules.llm_finding_guard import (
    _check_grounding,
    apply_review,
    review_llm_findings,
)

# ── 真实页的最小 structured 复现 ────────────────────────────────────────

# p6：接收开始 09:00 / 结束 09:52（原件顺序正常）；体积 13.6 m³
STRUCT_P6 = {
    "page_info": {"production_date": "2025-01-20"},
    "steps": [{
        "step_no": "3", "start_time": "09:00", "end_time": "09:52",
        "parameters": [{"name": "接收体积", "spec_range": "", "value": "13.6",
                        "unit": "m³"}],
    }],
}

# p17：P3 列四行全 0；F3 累计流量 2578（`2025.01.21` 是表外签名日期）
STRUCT_P17 = {
    "page_info": {"production_date": "2025-01-20"},
    "steps": [{
        "step_no": "3",
        "parameters": [],
        "measurements": [{
            "time": "13:18",
            "values": {
                "P3": {"spec": "0~1", "actual": "0", "unit": "MPa"},
                "F3 累计流量": {"spec": "", "actual": "2578", "unit": "L"},
            },
        }],
    }],
}

# 真倒序页（对照组）：开始 19:15 晚于结束 18:30
STRUCT_REAL_REVERSAL = {
    "page_info": {"production_date": "2025-01-20"},
    "steps": [{"step_no": "1", "start_time": "19:15", "end_time": "18:30"}],
}

# p43 / p46 实测形态：该页**有** steps，但时刻字段一个都解析不出
# （空串 / 占位符）⇒ 规则层无从复核，"判不了"不得当成"判据确凿"。
STRUCT_UNPARSEABLE_TIMES = {
    "page_info": {"production_date": "2025-09-25"},
    "steps": [
        {"step_no": "1", "start_time": "", "end_time": "—"},
        {"step_no": "2", "start_time": "见附表", "end_time": ""},
    ],
}


def _f(page, ftype, sev, desc, source="llm_page", **kw):
    return {"page": page, "type": ftype, "severity": sev,
            "description": desc, "source": source, **kw}


def _review(one, structures=None, raws=None):
    kept, down, sup = review_llm_findings(
        [one], structured_by_page=structures or {}, raw_by_page=raws or {},
    )
    return kept, down, sup


# ── L1 值形态：强证据 ⇒ 抑制 ────────────────────────────────────────────

class TestL1ValueShape:
    def test_p17_column_shift_letter_value(self):
        """p17：P3 的"实测值"是字母 A ⇒ 不是数值，超差判定不成立。"""
        _k, _d, sup = _review(
            _f(17, "param_out_of_spec", "critical",
               "步骤 3 中，13:18 时 P3 (MPa) 的实际值为 'A'，不符合规格范围。"),
            {17: STRUCT_P17},
        )
        assert len(sup) == 1
        assert sup[0]["evidence"]["guard_layer"] == "L1-value-shape"

    def test_p17_date_in_numeric_column(self):
        """p17：日期 `2025.01.21` 出现在数值列 ⇒ 列串位。"""
        _k, _d, sup = _review(
            _f(17, "param_out_of_spec", "critical",
               "步骤 3 中，13:18 时 F3 累计流量 (L) 的实际值为 '2025.01.21'，"
               "不符合规格范围。"),
            {17: STRUCT_P17},
        )
        assert len(sup) == 1
        assert "日期形态" in sup[0]["reason"]

    def test_p39_boolean_value_is_type_mismatch(self):
        """p39：勾选值"否"不是数值 ⇒ 不构成数值超差（类型错配）。"""
        _k, _d, sup = _review(
            _f(39, "param_out_of_spec", "critical",
               "步骤 6 中的器具排放是否整齐参数值为否，不符合规格范围"),
        )
        assert len(sup) == 1
        assert "布尔形态" in sup[0]["reason"]

    def test_p9_evaluative_phrase_is_not_a_value(self):
        """防误杀（p9 实测缺陷）：判据措辞「超出规格范围」不是"实测值"。

        提取器若把它当值，会以"非数值形态"抑制掉一条**真超差**。
        """
        _k, _d, sup = _review(
            _f(9, "param_out_of_spec", "warning", "T2101a~d 压力值超出规格范围"),
        )
        assert sup == []

    def test_p22_duplicate_time_claim_not_judged_by_reversal_rule(self):
        """防误杀（p22 实测缺陷）：「时间重复」不是「倒序」，不得用倒序判据否定它。"""
        _k, _d, sup = _review(
            _f(22, "time_reversal", "critical",
               "12:37 的流速记录与 12:37 的另一条记录存在时间重复"),
            {22: STRUCT_P6},   # 该页 structured 里当然没有倒序
        )
        assert sup == []


# ── L1 弱证据：查无此值 ⇒ 降级（不抑制，漏检代价更高）────────────────

class TestL1ValueUnlocatable:
    def test_p26_value_not_in_extraction_downgrades_not_suppresses(self):
        """p26：45.6°C 在结构化抽取中查无此值 ⇒ 降级（保留给人工，不丢）。"""
        kept, down, sup = _review(
            _f(26, "param_out_of_spec", "critical",
               "温度（0~5°C）在 12:10 时的实际值 45.6°C 超出规格范围 0~5°C"),
            {26: STRUCT_P17},   # 该页 actual 只有 0 / 2578
        )
        assert sup == []
        assert len(down) == 1 and down[0]["from_severity"] == "critical"


# ── L2 溯源 ─────────────────────────────────────────────────────────────

class TestL2Grounding:
    def test_p6_fabricated_number_downgraded(self):
        """p6：文案数字在 OCR 原文中无法定位 ⇒ 降级（疑似幻觉）。"""
        _k, down, sup = _review(
            _f(6, "suspicious_date", "warning", "操作者签名日期 18222 无法识别"),
            raws={6: "<table><tr><td>操作者签名</td><td>郑雅</td></tr></table>"},
        )
        assert sup == []
        assert len(down) == 1 and down[0]["to_severity"] == "info"


# ── L1' 推测性表述 ──────────────────────────────────────────────────────

class TestL1Speculative:
    def test_p6_unreadable_is_not_an_anomaly_conclusion(self):
        """p6：「无法识别」不能作为异常结论（v4 prompt 已禁止此类表述）。"""
        _k, down, sup = _review(
            _f(6, "suspicious_date", "critical", "操作者签名日期无法识别"),
        )
        assert sup == []
        assert len(down) == 1


# ── L3 判据重算 ─────────────────────────────────────────────────────────

class TestL3Recompute:
    def test_p6_cross_field_shift_suppressed(self):
        """p6：LLM 说"结束 13:6 早于开始 09:00"，structured 里是 09:00→09:52。"""
        _k, _d, sup = _review(
            _f(6, "time_reversal", "critical",
               "接收结束时间 13:6 早于接收开始时间 09:00"),
            {6: STRUCT_P6},
        )
        assert len(sup) == 1
        assert sup[0]["evidence"]["guard_layer"] == "L3-time-reversal"

    def test_real_reversal_kept(self):
        """对照组：structured 里确有倒序 ⇒ 不抑制（不得把真阳性一起收掉）。"""
        _k, _d, sup = _review(
            _f(1, "time_reversal", "warning", "工序1 结束时间早于开始时间"),
            {1: STRUCT_REAL_REVERSAL},
        )
        assert sup == []

    def test_p43_unparseable_page_downgrades_not_suppresses(self):
        """p43：文案**明写**「开始时间晚于结束时间」（真倒序的形态），但该页
        工序时刻**一个都解析不出** ⇒ 规则层**判不了** ⇒ 只降级、**不得抑制**。

        Round 46 对抗性审查修正（原为 P1）：旧 `_recompute_time_reversal` 返
        `bool`，把"无样本"归入 `False` ⇒ 实测冤枉抑制了 p43×4 / p46 的真倒序
        线索（`probe_audit2.py`【4】：8 条 L3 抑制里 **6 条**该页可解析样本 = 0）。
        """
        _k, down, sup = _review(
            _f(43, "time_reversal", "critical", "V2109浸泡开始时间晚于结束时间"),
            {43: STRUCT_UNPARSEABLE_TIMES},
        )
        assert sup == [], "判不了时不得抑制（漏检代价 > 误报代价）"
        assert down and down[0]["to_severity"] == "warning"
        assert "无法复核" in down[0]["reason"]

    def test_time_reversal_without_structured_downgrades(self):
        """structured 缺失（json 坏页 / 未抽取）⇒ 同样判不了 ⇒ 不抑制。"""
        _k, down, sup = _review(
            _f(46, "time_reversal", "critical", "V2116 浸泡开始时间晚于 V2116 浸泡结束时间"),
        )
        assert sup == []
        assert down, "判不了时应降级保留，而不是静默丢弃"

    def test_p2_declared_order_reversed_suppressed(self):
        """p2：「2025.01.30 晚于 2026.09.18」—— 参照物是当前日期 ⇒ 判定不成立。

        Round 48：抑制层级由 ``L3-declared-order`` 改为 **``L3-current-date``**
        —— 该语义已整体移出 ``_check_declared_order``（后者只管"两侧都是记录内
        日期"），因为它与"方向反"是两个独立判据，合在一处必然漏掉方向对的条目。
        """
        _k, _d, sup = _review(
            _f(2, "signature_time_anomaly", "critical",
               "车间负责人审核日期2025.01.30晚于当前日期2026.09.18"),
        )
        assert len(sup) == 1
        assert sup[0]["evidence"]["guard_layer"] == "L3-current-date"

    def test_p38_declared_order_reversed_downgrades_not_suppresses(self):
        """p38：方向词错（2027 > 2025 却说"早于"），但两侧**都是记录内日期**
        ⇒ 只降级、**不抑制**。

        Round 46 对抗性审查修正（原为 P1）：旧实现把"方向词反"当成"整条 finding
        不成立"而抑制，实测导致「2027」这个**未来日期**从该页结果里完全消失
        （该页仅剩 `[rule] self_review`，规则层**无兜底**）⇒ 丢失真线索。
        错的只是 LLM 的**表述**，日期对本身必须留给复核者。
        """
        _k, down, sup = _review(
            _f(38, "signature_time_anomaly", "critical",
               "操作者签名时间 2027.01.17 早于 生产日期 2025年01月20日"),
        )
        assert sup == [], "两侧均为记录内日期时不得抑制（会丢掉真线索）"
        assert len(down) == 1
        assert down[0]["to_severity"] == "warning"
        assert "应为「晚于」" in down[0]["reason"]

    def test_p27_declared_order_reversed_downgrades_not_suppresses(self):
        """p27：两侧日期相差 **10 年**（2025.01.29 vs 2015.01.25）⇒ 年份误读
        线索必须留下（该页只是**碰巧**另有一条 `year_contradiction` 兜底，
        不得依赖巧合）。"""
        _k, down, sup = _review(
            _f(27, "signature_time_anomaly", "warning",
               "复核者签名时间 2025.01.29 早于操作者签名时间 2015.01.25"),
        )
        assert sup == []
        assert len(down) == 1
        assert "应为「晚于」" in down[0]["reason"]

    def test_correct_direction_not_suppressed(self):
        """方向自洽 ⇒ 不干预（如真有未来日期：2027 晚于 2025 是事实）。"""
        _k, _d, sup = _review(
            _f(1, "signature_time_anomaly", "warning",
               "签名时间 2027.01.17 晚于 生产日期 2025年01月20日"),
        )
        assert sup == []


# ── L3 日期语义（B1-4，Round 48）────────────────────────────────────────
#
# B1-4 的两条判据都**只否定可证伪的前提、绝不猜真值**（fail-open）：
#   ① 参照物是「当前日期/当前年份」⇒ 该比较不构成异常（**方向无关**）
#   ② finding 引用的"生产日期"恰是**文档级单源**解析出的当前日期 ⇒ 前提不成立
# 每条都配一个**反向控制**（同一形态翻转关键字段后必须不抑制），
# 否则就是"写完就绿"的空护栏。

class TestL3DateSemanticsB14:
    # ── ① 参照物 = 当前日期 ⇒ 抑制（方向无关）────────────────────────

    def test_p49_direction_correct_still_suppressed(self):
        """p49（Round 48 新收）：参照物是当前日期，且**方向本就对**（2025 < 2026）。

        旧实现只在方向**反**时抑制 ⇒ 这类"过去日期早于现在"的记录常态全部漏网
        （实测 23 条里只收掉 1 条）。这是本条判据的**主要**新增覆盖。
        """
        _k, _d, sup = _review(
            _f(49, "signature_time_anomaly", "critical",
               "审核人庞明娟的签名日期为2025.02.24，早于当前年份2026.09.18"),
        )
        assert len(sup) == 1
        assert sup[0]["evidence"]["guard_layer"] == "L3-current-date"

    def test_p38_not_equal_to_current_year_suppressed(self):
        """p38：「生产日期 2025年01月20日 与 当前年份 2026年09月18日 不符」。

        与当前日期"不符"是**必然**（记录不可能总在今天产生）⇒ 不是异常。
        形态上**没有先后关系词**，只能由本判据（而非 `_check_declared_order`）收掉。
        """
        _k, _d, sup = _review(
            _f(38, "suspicious_date", "critical",
               "生产日期2025年01月20日与当前年份2026年09月18日不符"),
        )
        assert len(sup) == 1
        assert sup[0]["evidence"]["guard_layer"] == "L3-current-date"

    def test_year_only_reference_still_detected(self):
        """参照物**只到年**（「当前年份 2026」）也必须认得出 —— 实测形态。

        回归护栏：早期实现用 `_DATE_LITERAL_RE`（要求"年+月"）解析参照物，
        纯年份会被整个丢掉 ⇒ ① 对 p3/p12/p25/p37 等页静默失效。
        """
        _k, _d, sup = _review(
            _f(3, "suspicious_date", "warning", "生产日期 2015.01.20 早于当前年份 2026"),
        )
        assert len(sup) == 1 and sup[0]["evidence"]["guard_layer"] == "L3-current-date"

    def test_future_date_vs_current_date_kept(self):
        """**反向控制**：记录内日期**晚于**自述当前日期 ⇒ 真未来日期，必须保留。

        没有这条，① 就退化成"凡带当前日期的条目一律抑制"，会连真异常一起收掉。
        """
        _k, _d, sup = _review(
            _f(1, "suspicious_date", "critical", "签名时间 2027.01.17 晚于 当前日期 2026.09.18"),
        )
        assert sup == [], "未来日期是真异常，不得抑制"

    def test_literal_less_current_year_claims_fail_open(self):
        """**fail-open 边界**：文案凑不出"记录内日期字面量"时不得猜测。

        两条实测形态都走这里：
        - p1「起草人签名年份与当前年份不符」—— 压根没给年份；
        - p36「记录发放年份 > 当前年份+1」—— 语序与断言方向**正相反**。
        无从用字面量证伪 ⇒ 不干预（宁可留噪，不可误杀）。
        """
        for desc in ("起草人签名年份与当前年份不符", "记录发放年份 > 当前年份+1"):
            _k, _d, sup = _review(_f(1, "year_contradiction", "warning", desc))
            assert sup == [], desc

    # ── ② 引用"当前日期"当生产日期 ⇒ 前提不成立 ─────────────────────

    def test_p28_without_cross_page_evidence_fails_open(self):
        """p28 的**另一半**：只有它自己时（没有别的页声明当前日期）判不了 ⇒ 不抑制。

        p28 的文案里**没有**「当前日期」字样 —— 单靠本页无法证伪"2026-09-18 是
        生产日期"这个前提。此时按 fail-open 只降级（见 `_check_production_date_mismatch`：
        引用的值与该页抽取出的 `2025.01.25` 不一致）。
        """
        _k, down, sup = _review(
            _f(28, "year_contradiction", "critical",
               "步骤8中记录的日期2015-01-23与生产日期2026-09-18矛盾"),
            {28: {"page_info": {"production_date": "2025.01.25"}}},
        )
        assert sup == [], "无跨页证据时不得抑制（fail-open）"
        assert any("不一致" in d["reason"] for d in down)

    def test_p28_suppressed_when_current_date_declared_elsewhere(self):
        """**跨页单源**：把 p38 的「当前年份 2026年09月18日」声明一并喂进去，
        p28 的前提才能被证伪 ⇒ 抑制。

        与上一条成对：单独看 p28 判不了（它自己没写「当前日期」），
        有了文档级单源才判得出来 —— 这正是 B1-4 ② 要的"一次解析、全页复用"。
        """
        kept, down, sup = review_llm_findings(
            [
                _f(38, "suspicious_date", "warning",
                   "生产日期2025年01月20日与当前年份2026年09月18日不符"),
                _f(28, "year_contradiction", "critical",
                   "步骤8中记录的日期2015-01-23与生产日期2026-09-18矛盾"),
            ],
            structured_by_page={28: {"page_info": {"production_date": "2025.01.25"}}},
        )
        p28_sup = [s for s in sup if s["finding"].get("page") == 28]
        assert len(p28_sup) == 1
        assert p28_sup[0]["evidence"]["guard_layer"] == "L3-date-baseline"

    def test_p38_correct_production_date_not_suppressed(self):
        """**反向控制**：p38 引用的是**正确**的生产日期（2025年01月20日）⇒
        ② 不得误伤；「2027」这条真线索必须留下（只是不再 critical）。

        没有这条，② 就退化成"凡引用生产日期一律抑制"。
        """
        _k, down, sup = _review(
            _f(38, "signature_time_anomaly", "critical",
               "操作者签名时间 2027.01.17 早于 生产日期 2025年01月20日"),
            {38: {"page_info": {"production_date": "2025年01月20日"}}},
        )
        assert sup == [], "引用正确的生产日期时不得抑制"
        assert down and down[0]["to_severity"] == "warning"

    def test_cited_date_mismatch_with_own_page_downgrades(self):
        """② 弱证据：引用的生产日期与该页抽取出的不一致 ⇒ 只降级（保留给人工）。"""
        _k, down, sup = _review(
            _f(7, "year_contradiction", "warning",
               "取样日期 2025.01.29 与生产日期 2015.01.25 年份矛盾"),
            {7: {"page_info": {"production_date": "2025.01.25"}}},
        )
        assert sup == []
        assert any("不一致" in d["reason"] for d in down)


# ── L4 severity 封顶（收权的核心不变式）────────────────────────────────

class TestL4SeverityCap:
    def test_llm_critical_never_survives_as_critical(self):
        """不变式：LLM 的 critical 一律不给 critical（无规则层背书）。"""
        samples = [
            _f(1, "completeness", "critical", "缺少复核人签名"),
            _f(1, "year_contradiction", "critical", "年份矛盾"),
            _f(1, "suspicious_date", "critical", "日期可疑"),
            _f(1, "signature_time_anomaly", "critical", "签名时间异常"),
            _f(1, "param_out_of_spec", "critical", "参数超差"),
        ]
        for s in samples:
            kept, down, _sup = _review(s)
            assert not any(k.get("severity") == "critical" for k in kept), s
            assert down and down[0]["to_severity"] == "warning"

    def test_rule_source_not_reviewed(self):
        """规则层是判据来源：其 critical 必须原样保留。"""
        kept, down, sup = _review(
            _f(38, "self_review", "critical",
               "第38页 工序3 操作人与复核人为同一人「周新」", source="rule"),
        )
        assert len(kept) == 1 and kept[0]["severity"] == "critical"
        assert down == [] and sup == []

    def test_llm_warning_without_weak_evidence_kept(self):
        """无弱证据的 LLM warning 保留（只降 critical，不无差别打压）。"""
        kept, down, _sup = _review(_f(1, "completeness", "warning", "缺少 QA 签名"))
        assert len(kept) == 1 and down == []


# ── 契约：纯函数 / 输出形状 ─────────────────────────────────────────────

class TestContract:
    def test_input_not_mutated(self):
        """apply_review 不得改动入参（调用方可能仍持有原对象）。"""
        f = _f(1, "completeness", "critical", "缺少签名")
        snapshot = dict(f)
        apply_review([f])
        assert f == snapshot

    def test_suppressed_shape_matches_ledger(self):
        """抑制明细须与 spec_guard.suppression_rows 同形（可直接写台账）。"""
        _k, _d, sup = _review(
            _f(39, "param_out_of_spec", "critical", "参数值为否，不符合规格范围"),
        )
        assert set(sup[0]) == {"finding", "reason", "evidence"}
        assert sup[0]["reason"].strip()          # 非空理由
        assert sup[0]["evidence"]["page"] == 39  # 可追溯

    def test_downgraded_reason_is_visible_in_description(self):
        """降级理由必须对复核者可见（GMP：结论变更须可解释）。"""
        out, _sup = apply_review([_f(1, "completeness", "critical", "缺少签名")])
        assert out and "｜" in out[0]["description"]

    def test_non_llm_entries_pass_through(self):
        """user_rule/未知 source 原样通过（本模块只复核 LLM 生成项）。"""
        kept, _d, _s = _review(_f(1, "completeness", "critical", "x", source="user_rule"))
        assert len(kept) == 1


# ── B2-12：L2 溯源不得把「系统注入的当前日期/当前年份」说成幻觉 ──────────
class TestL2GroundingExcludesSystemDates:
    """B2-12：`_check_grounding` 的**理由**必须说真话。

    这类值来自**系统提示词注入的当天日期**（如 `2026-09-18`），本来就不该出现在
    OCR 原文里 ⇒ 对它扣「疑似提取幻觉」的帽子是**理由说谎**
    （Round 46 实测 19 条 L2 降级里 14 条属此类）。

    B1-4 ①（`_check_current_date_reference`）上线后这些条目已被**抑制**，
    实测残留 = 0 条。所以本组守的是**第二道**：一旦 ① 因凑不出两侧字面量而
    fail-open，L2 的理由也不能变成假话。
    """

    RAW = "复核记录 缺"          # OCR 原文里**必然没有** 2026
    DESC = "当前年份 2026年09月18日 与记录年份不符"

    def test_system_year_is_not_reported_as_hallucination(self):
        """系统注入的年份不产生「疑似提取幻觉」降级。"""
        f = _f(1, "suspicious_date", "warning", self.DESC, ocr_text=self.RAW)
        kept, down, sup = _review(f, raws={1: self.RAW})
        assert not sup, "参照物在句首（无前置日期）⇒ ① 判不了，不应抑制"
        assert not [d for d in down if "幻觉" in d["reason"]], (
            f"2026 是系统注入的当天日期，扣「幻觉」帽子是理由说谎：{down}"
        )
        assert len(kept) == 1, "无其它弱证据 ⇒ 应原样保留可见"

    def test_exclusion_is_load_bearing(self):
        """**正向对照**：去掉排除集，同一输入就真的会被扣「幻觉」帽子。

        没有这一条，上面的断言可能因为别的原因（如根本没走到 L2）而恒绿。
        """
        f = {"description": self.DESC, "ocr_text": self.RAW}
        without = _check_grounding(f, self.RAW)
        assert without is not None and "幻觉" in without, (
            f"对照失败：不传排除集时应当命中 L2，否则本组用例是空断言：{without}"
        )
        assert _check_grounding(f, self.RAW, exclude_dates={(2026, 9, 18)}) is None

    def test_genuinely_fabricated_number_still_flagged(self):
        """**反向控制**：真·凭空数字仍必须被标出（排除集不得把 L2 打哑）。"""
        f = {"description": "体积 9876 超出规格", "ocr_text": "体积 无数据记录"}
        reason = _check_grounding(f, f["ocr_text"], exclude_dates={(2026, 9, 18)})
        assert reason is not None and "幻觉" in reason, (
            f"9876 与池内日期无关，必须照旧判为无法定位：{reason}"
        )

    def test_exclusion_tolerates_mixed_literal_granularity(self):
        """池只到年、文案写到月日时也要排除；池外年份不得被误排除。"""
        f = {"description": "当前日期 2026.09 与当前年份 2026 不符", "ocr_text": self.RAW}
        assert _check_grounding(f, self.RAW, exclude_dates={(2026, 0, 0)}) is None, (
            "池里只到年（(2026,0,0)）时，文案写的 2026.09 / 2026 都应被排除"
        )
        g = {"description": "记录年份 2027 与当前日期 2026.09 不符", "ocr_text": self.RAW}
        r = _check_grounding(g, self.RAW, exclude_dates={(2026, 9, 18)})
        assert r is not None and "2027" in r, f"2027 不在池内，必须仍被标出：{r}"
