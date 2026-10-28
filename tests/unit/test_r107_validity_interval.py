"""R107 / P1-2：有效期的日期比较不是"参数超标"，且只给单点会误导。

事故（实测 `8df2f568` p2）：`param_out_of_spec` info
「电子台秤(A-FW01-307-4005)的校验有效期(2025.05.30)早于检查日期(2025.06.03)，
设备可能超期未校验」—— 复核页把它渲染成标题**「参数超标」**。

两个错：
1. 这是**日期比较**，`param_out_of_spec` 是数值超差类型，比的根本不是规格区间；
2. 有效期是**区间**，只印 `2025.05.30` 会让复核员以为"有效期就是这一天"。

改法：① 文案信号把 `param_out_of_spec` 纠正成 `suspicious_date`（UI「日期可疑」）；
② 只**补**能算出来的（相差几天、区间起点是否识别），**不改写、不编造起点**。

R115（用户 2026-10-27）追加了第二条裁定 —— 有效期是**时间段**，检查日期要
落在时间段**内**：
    「我在 2026.02.06 的时候，检查了仪器的有效期是在 2025.03.05-2026.03.04，
      仪器处于有效期，2026.02.06 这个时间点落在这个有效期的时间段内」
⇒ 判定按"落点是否在容器内"，而不是把两个日期并排比大小。
"""
from __future__ import annotations

import pytest

from core.finding_quality import (
    is_pure_validity_claim,
    normalize_finding,
    validity_overdue_clause,
    validity_verdict,
)
from core.rules.llm_finding_guard import apply_review

REAL = (
    "电子台秤(A-FW01-307-4005)的校验有效期(2025.05.30)"
    "早于检查日期(2025.06.03)，设备可能超期未校验。"
)

#: 实测 `8df2f568` p3 的**原文**（锚点是「称量操作日期」，不是「检查日期」）。
REAL_P3 = (
    "电子台秤/电子天平校验有效期（2025.05.30）"
    "早于称量操作日期（2025.06.03），可能已过期。"
)

#: 用户 2026-10-27 的原话用例。
USER_INTERVAL = "仪器有效期 2025.03.05-2026.03.04，检查日期 2026.02.06，仪器已过期。"


def _f(desc: str = REAL, ftype: str = "param_out_of_spec") -> dict:
    return {"page": 2, "type": ftype, "severity": "info",
            "source": "llm_page", "description": desc}


class TestTypeCorrection:
    def test_validity_comparison_is_not_param_out_of_spec(self):
        """实测那一条：类型必须变成 `suspicious_date`，原始类型留痕。"""
        f = _f()
        canonical, raw = normalize_finding(f)
        assert canonical == "suspicious_date"
        assert raw == "param_out_of_spec"

    def test_late_validity_is_also_a_date_issue(self):
        canonical, _ = normalize_finding(
            _f("校验有效期(2025.06.30)晚于检查日期(2025.06.03)。")
        )
        assert canonical == "suspicious_date"

    def test_real_spec_violation_is_untouched(self):
        """反向控制：真超差（判定词 + 数值区间）不得被改判成日期问题。"""
        f = _f("配浆温度 30°C 超出规格 15-25°C。")
        canonical, raw = normalize_finding(f)
        assert canonical == "param_out_of_spec"
        assert raw is None

    def test_other_types_are_not_reclassified(self):
        """只有"过宽类型"才纠正 —— 已经是 `suspicious_date` 的不动。"""
        f = _f(ftype="suspicious_date")
        canonical, raw = normalize_finding(f)
        assert canonical == "suspicious_date"
        assert raw is None


class TestOverdueClause:
    def test_single_point_gets_days_and_honest_gap(self):
        clause = validity_overdue_clause(REAL)
        assert "已超出 4 天" in clause
        assert "2025-05-30" in clause and "2025-06-03" in clause
        assert "区间起点未识别" in clause, "起点原文里没有，不能编"

    def test_explicit_interval_is_echoed(self):
        """原文已给区间（用「至」或横线连接）⇒ 照实写区间，不说"起点未识别"。"""
        for sep in ("至", "-"):
            clause = validity_overdue_clause(
                f"校验有效期 2024.05.31 {sep} 2025.05.30，检查日期 2025.06.03。"
            )
            assert "2024-05-31 至 2025-05-30" in clause
            assert "区间起点未识别" not in clause

    def test_unrelated_earlier_date_is_not_taken_as_start(self):
        """「2024.01.01 生产」不是有效期起点 —— 中间没有区间连接词，不得当区间。"""
        clause = validity_overdue_clause(
            "2024.01.01 生产，校验有效期(2025.05.30)早于检查日期(2025.06.03)。"
        )
        assert "区间起点未识别" in clause

    def test_not_overdue_returns_none(self):
        assert validity_overdue_clause("校验有效期(2025.06.30)晚于检查日期(2025.06.03)。") is None

    def test_missing_anchor_or_dates_returns_none(self):
        assert validity_overdue_clause("校验有效期(2025.05.30)已过期。") is None
        assert validity_overdue_clause("没有日期的一句话。") is None
        assert validity_overdue_clause("") is None
        assert validity_overdue_clause(None) is None

    def test_impossible_date_returns_none(self):
        """`2025.13.40` 构造不出日期 ⇒ 不补（宁可不说话，也不写错天数）。"""
        assert validity_overdue_clause("有效期(2025.13.40)早于检查日期(2025.06.03)。") is None


class TestGuardWiring:
    def test_apply_review_appends_the_clause(self):
        out, _ = apply_review([_f()], structured_by_page={2: {}}, raw_by_page={})
        assert "已超出 4 天" in out[0]["description"]
        assert out[0]["description"].startswith("电子台秤")

    def test_input_is_not_mutated(self):
        src = _f()
        apply_review([src], structured_by_page={2: {}}, raw_by_page={})
        assert src["description"] == REAL

    def test_is_idempotent(self):
        """跑两遍不得重复追加（retry 会重跑 Stage 3）。"""
        once, _ = apply_review([_f()], structured_by_page={2: {}}, raw_by_page={})
        twice, _ = apply_review(once, structured_by_page={2: {}}, raw_by_page={})
        assert twice[0]["description"] == once[0]["description"]
        assert twice[0]["description"].count("已超出") == 1

    def test_not_overdue_is_not_annotated(self):
        out, _ = apply_review(
            [_f("校验有效期(2025.06.30)晚于检查日期(2025.06.03)。")],
            structured_by_page={2: {}}, raw_by_page={},
        )
        assert "已超出" not in out[0]["description"]


class TestAnchorVocabulary:
    """R115：锚点必须是"日期角色"的词族 —— 只认「检查日期」等于功能没上线。

    实测真实库 5 条有效期断言，旧实现**一条都补不上**（命中 0/5）：真实文案
    写的是「称量操作日期」。
    """

    #: 钉死的锚点集合。用字面量而不是 import 实现里的常量 —— 否则删掉一个锚点
    #: 时参数化会**跟着缩水**，用例反而更少，"测试通过"是假象。
    _PINNED = (
        "检查日期", "操作日期", "使用日期", "称量日期",
        "检验日期", "生产日期", "复核日期",
    )

    def test_anchor_set_is_pinned(self):
        from core.finding_quality import _ANCHOR_WORDS

        assert set(_ANCHOR_WORDS) == set(self._PINNED), (
            "锚点集合变了 —— 真实数据的命中率会跟着变，必须回来重算"
        )

    def test_real_page3_string_is_annotated(self):
        clause = validity_overdue_clause(REAL_P3)
        assert clause, "真实 p3 文案必须能补上说明，否则该功能在真实数据上等于不存在"
        assert "已超出 4 天" in clause
        assert "2025-05-30" in clause and "2025-06-03" in clause

    @pytest.mark.parametrize("anchor", _PINNED)
    def test_every_anchor_word_is_recognised(self, anchor):
        text = f"校验有效期(2025.05.30)早于{anchor}(2025.06.03)。"
        assert validity_overdue_clause(text), f"锚点「{anchor}」没被识别"

    def test_compound_anchor_is_covered_by_its_tail(self):
        """「称量操作日期」由「操作日期」覆盖（子串命中），不需要单列。

        变异 N9 实测：把它单列进 `_ANCHOR_WORDS` 再删掉，**零用例变红** ——
        说明它是冗余枚举。这里把它变成一条**显式**不变式。
        """
        assert validity_overdue_clause(REAL_P3), "复合锚点必须靠尾词命中"

    def test_signature_date_is_not_an_anchor(self):
        """「复核人签名日期…及设备校验有效期」不得把签名日期当落点。

        实测 `4cffb358` p8：把签名日期当落点会得出"签名早于有效期 ⇒ 时间倒置"
        这种没有意义的比较。
        """
        text = ("复核人签名日期（2015.6.03）早于检查人签名日期（2025.06.30）"
                "及设备校验有效期（2025.06.30），存在时间倒置。")
        assert validity_verdict(text) is None

    def test_field_name_is_echoed_not_generalised(self):
        """原文写「校验有效期」就回写「校验有效期」，不降格成泛泛的「有效期」。"""
        clause = validity_overdue_clause(REAL_P3)
        assert "校验有效期至" in clause


class TestIntervalVerdict:
    """区间语义：有效期是容器，检查日期是落点。"""

    def test_user_example_is_inside(self):
        v = validity_verdict(USER_INTERVAL)
        assert v is not None
        assert v.status == "inside"
        assert v.start.isoformat() == "2025-03-05"
        assert v.end.isoformat() == "2026-03-04"
        assert v.checked.isoformat() == "2026-02-06"

    def test_chinese_joiner_is_equivalent_to_hyphen(self):
        a = validity_verdict("仪器有效期 2025.03.05-2026.03.04，检查日期 2026.02.06。")
        b = validity_verdict("仪器有效期 2025.03.05 至 2026.03.04，检查日期 2026.02.06。")
        assert a.status == b.status == "inside"

    def test_after_end_is_expired(self):
        v = validity_verdict("校验有效期 2024.05.31 至 2025.05.30，检查日期 2025.06.03。")
        assert v.status == "expired"
        assert v.days == 4

    def test_before_start_is_its_own_status(self):
        """早于起点 ≠ 过期。方向反了，文案必须分开 —— 否则复核员会读成超期。"""
        v = validity_verdict("校验有效期 2025.03.05 至 2026.03.04，检查日期 2024.12.01。")
        assert v.status == "before_start"
        assert v.days == 94

    def test_single_point_can_only_prove_not_expired(self):
        """只给到期日时，**不能**声称"在有效期内"（起点未知，可能在起点之前）。"""
        v = validity_verdict("校验有效期(2025.06.30)晚于检查日期(2025.06.03)。")
        assert v.status == "not_expired"
        assert v.start is None

    def test_single_point_past_expiry_is_still_expired(self):
        v = validity_verdict(REAL)
        assert v.status == "expired"
        assert v.start is None

    def test_reversed_interval_is_not_trusted(self):
        """区间写反（起点晚于终点）⇒ 退回单点语义，不拿倒置的起点算天数。"""
        v = validity_verdict("校验有效期 2026.03.04 至 2025.03.05，检查日期 2026.02.06。")
        assert v.start is None

    def test_unrelated_earlier_date_never_becomes_start(self):
        v = validity_verdict("2024.01.01 生产，校验有效期(2025.05.30)早于检查日期(2025.06.03)。")
        assert v.start is None

    def test_no_verdict_when_dates_cannot_be_paired(self):
        assert validity_verdict("产品复测期/失效期/有效期晚于生产日期超过3年。") is None
        assert validity_verdict("校验有效期(2025.05.30)已过期。") is None
        assert validity_verdict(None) is None
        assert validity_verdict("") is None


class TestNoPointClaimedAsInterval:
    """用户 2026-10-27：「我们一定声称有效期是时间段，而不是时间点」。"""

    def test_annotation_never_claims_within_validity(self):
        """补的说明只允许说"超出/早于"，不得出现"在有效期内"这种未被证明的结论。"""
        for text in (REAL, REAL_P3, USER_INTERVAL,
                     "校验有效期 2024.05.31 至 2025.05.30，检查日期 2025.06.03。",
                     "校验有效期 2025.03.05 至 2026.03.04，检查日期 2024.12.01。"):
            clause = validity_overdue_clause(text)
            assert "在有效期内" not in (clause or "")
            assert "有效" not in (clause or "").replace("有效期", "")

    def test_single_point_annotation_admits_unknown_start(self):
        clause = validity_overdue_clause(REAL)
        assert "区间起点未识别" in clause
        assert "至" in clause, "单点也必须读成'有效期至 X'（区间终点），不是'有效期是 X'"


class TestIntervalSuppression:
    """可证伪的"已过期"断言：区间与落点都在同一句里，算得出来。"""

    def test_inside_interval_suppresses_the_expiry_claim(self):
        out, sup = apply_review([_f(USER_INTERVAL, "suspicious_date")],
                                structured_by_page={2: {}}, raw_by_page={})
        assert out == []
        assert len(sup) == 1
        assert sup[0]["evidence"]["guard_layer"] == "L3-validity-interval"

    def test_expired_claim_is_kept(self):
        out, sup = apply_review(
            [_f("校验有效期 2024.05.31 至 2025.05.30，检查日期 2025.06.03，已过期。",
                "suspicious_date")],
            structured_by_page={2: {}}, raw_by_page={})
        assert len(out) == 1 and sup == []

    def test_not_expired_single_point_is_never_suppressed(self):
        """只给到期日 ⇒ 起点未知 ⇒ "在有效期内"未被证明 ⇒ 不得抑制。"""
        out, sup = apply_review(
            [_f("校验有效期(2025.06.30)晚于检查日期(2025.06.03)，可能已过期。",
                "suspicious_date")],
            structured_by_page={2: {}}, raw_by_page={})
        assert len(out) == 1 and sup == []

    def test_compound_description_is_not_suppressed(self):
        """前半句说有效期、后半句说签名缺失 ⇒ 真缺陷不能连坐。"""
        text = ("仪器有效期 2025.03.05-2026.03.04，检查日期 2026.02.06，已过期。"
                "另：复核人签名缺失。")
        assert is_pure_validity_claim(text) is False
        out, sup = apply_review([_f(text, "suspicious_date")],
                                structured_by_page={2: {}}, raw_by_page={})
        assert len(out) == 1 and sup == []

    def test_rule_source_is_never_suppressed(self):
        """规则层是确定性判据 —— 与 R104/R105 同一条边界，系统不得吞掉它。"""
        f = _f(USER_INTERVAL, "suspicious_date")
        f["source"] = "rule"
        out, sup = apply_review([f], structured_by_page={2: {}}, raw_by_page={})
        assert len(out) == 1 and sup == []

