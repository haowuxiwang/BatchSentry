"""core/rules/llm_finding_guard.py 单测（B1-6 收权复核）。

样本形态**全部取自 Round 43 的 51 页真实 e2e 实测**（见 docs/ADVERSARIAL_AUDIT.md
§15 与 devlogs/_replay/），不是构造出来的假想用例 —— 每个 ``pXX`` 都对应真页。

判别力说明（变异验证）：若把 ``review_llm_findings`` 改成直接原样返回
（no-op），下列全部断言都会红；反之若把某条判据放宽（如允许评价词当实测值），
对应的"防误杀"用例会红。两条方向都被守住。
"""
from __future__ import annotations

from core.rules.llm_finding_guard import (
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

    def test_p2_declared_order_reversed_suppressed(self):
        """p2：「2025.01.30 晚于 2026.09.18」—— 2025 < 2026，方向与事实相反。"""
        _k, _d, sup = _review(
            _f(2, "signature_time_anomaly", "critical",
               "车间负责人审核日期2025.01.30晚于当前日期2026.09.18"),
        )
        assert len(sup) == 1
        assert sup[0]["evidence"]["guard_layer"] == "L3-declared-order"

    def test_p38_declared_order_reversed_suppressed(self):
        """p38：「2027.01.17 早于 2025.01.20」—— 2027 > 2025，方向相反。"""
        _k, _d, sup = _review(
            _f(38, "signature_time_anomaly", "critical",
               "操作者签名时间 2027.01.17 早于 生产日期 2025年01月20日"),
        )
        assert len(sup) == 1

    def test_correct_direction_not_suppressed(self):
        """方向自洽 ⇒ 不干预（如真有未来日期：2027 晚于 2025 是事实）。"""
        _k, _d, sup = _review(
            _f(1, "signature_time_anomaly", "warning",
               "签名时间 2027.01.17 晚于 生产日期 2025年01月20日"),
        )
        assert sup == []


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
