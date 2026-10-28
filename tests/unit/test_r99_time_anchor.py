"""R99：CR-5（时序锚点）/ CR-6（跨零点）/ CR-26（准备类工序）的判据护栏。

三条缺陷都在真实语料上有逐条定位（`docs/CROSS_REVIEW_2026-10-22.md` §3.2 B4）：

- **CR-5**：10 页语料 `production_date` **10/10 页全空**，`_parse_time("12:00","")`
  一律返回 None ⇒ 时序检查**静默全线失效**，同时把正常的 `12:00`/`14:00` 报成
  「时间格式无法解析」（`#1602`–`#1605`）。格式没坏，缺的是"这一天是哪天"。
- **CR-6**：`#1482`（p24 `14时12分 → 08时18分`）、`#1511`（p11 `23:04 → 00:04`）、
  `#1510`（p33 `20:21 → 00:17`）全是**跨零点**的正常写法；`#1426` 是 LLM 对
  同一现象的复述。缺日期时"同一天倒序"与"跨零点"**无法区分**。
- **CR-26**：`#1483`（p10 工序3「25%氯化钠溶液配制」10:15 vs p9 工序2 11:04）
  —— 溶液是**提前配好**的，`step_no` 递增 ≠ 时刻必须递增。

本文件同时钉住**不得被顺手放宽**的三条边界：
  ① 跨**工序**边界的同日倒序不按跨零点处理（否则 B1-9 的检测强度被静默削弱）；
  ② 准备类工序只在"年份差 ≤2"时豁免（年份差大 = 提取错误，仍要报）；
  ③ 文案自带日期/日号时，跨零点豁免不生效（那一天是事实，可判）。
"""
from __future__ import annotations

import pytest

from core.cross_page_analyzer import _normalize_pages
from core.rules.llm_finding_guard import (
    _is_cross_midnight_clock_claim,
    review_llm_findings,
)
from core.rules.parsing import crosses_midnight, page_anchor_date
from core.rules.rule_doc import _check_measurement_time_sequence
from core.rules.rule_time import (
    _check_time_reversal_cross_page,
    _check_time_reversal_in_page,
)


def _step(step_no, start=None, end=None, operation=None, measurements=None):
    s: dict = {"step_no": step_no}
    if start is not None:
        s["start_time"] = start
    if end is not None:
        s["end_time"] = end
    if operation is not None:
        s["operation"] = operation
    if measurements is not None:
        s["measurements"] = [{"time": t} for t in measurements]
    return s


def _pages(items):
    """items: (page_no, steps[, page_info]) —— 走真实归一化，不手搓结构。"""
    structures = []
    for item in items:
        pno, steps = item[0], item[1]
        info = item[2] if len(item) > 2 else {}
        structures.append({
            "page": pno,
            "data": {"steps": steps, "findings": [], "page_info": info},
        })
    return _normalize_pages(structures)


# ── CR-5：页级日期锚点 ────────────────────────────────────────────────
class TestPageAnchorDate:
    def test_production_date_wins(self):
        """有生产日期就用它 —— 签名日期只是兜底，不能反客为主。"""
        page = _pages([(1, [_step(1, "10:00", "11:00", measurements=["10:00"])],
                        {"production_date": "2025-01-20"})])[0]
        page["steps"][0]["signatures"] = [{"role": "reviewer", "sign_time": "2025-06-03"}]
        assert page_anchor_date(page) == "2025-01-20"

    def test_blank_production_date_falls_back_to_signature_mode(self):
        """空白字符串等同缺失（实测语料里就是 `""`，不是 None）。"""
        page = _pages([(1, [_step(1, "10:00", "11:00")], {"production_date": "   "})])[0]
        page["steps"][0]["signatures"] = [
            {"role": "reviewer", "sign_time": "2025-06-03"},
            {"role": "operator", "sign_time": "2025-06-04"},
            {"role": "qa", "sign_time": "2025-06-03"},
        ]
        assert page_anchor_date(page) == "2025-06-03"

    def test_tie_goes_to_first_seen(self):
        """票数相同时取**先出现**的日期（按阅读顺序，可复现，不做随机）。"""
        page = _pages([(1, [_step(1, "10:00", "11:00")], {})])[0]
        page["steps"][0]["signatures"] = [
            {"role": "operator", "sign_time": "2025-06-05"},
            {"role": "reviewer", "sign_time": "2025-06-04"},
        ]
        assert page_anchor_date(page) == "2025-06-05"

    def test_no_anchor_at_all_returns_none(self):
        page = _pages([(1, [_step(1, "10:00", "11:00")], {})])[0]
        page["steps"][0]["signatures"] = [{"role": "operator", "sign_time": ""}]
        assert page_anchor_date(page) is None

    def test_page_without_steps_returns_none(self):
        page = _pages([(1, [], {})])[0]
        assert page_anchor_date(page) is None


# ── CR-6：跨零点判定本身 ─────────────────────────────────────────────
class TestCrossesMidnight:
    def test_both_dateless_and_curr_earlier_is_midnight(self):
        from core.rules.parsing import _parse_time

        assert crosses_midnight("14时12分", "08时18分",
                                _parse_time("14时12分", "2025-01-22"),
                                _parse_time("08时18分", "2025-01-22")) is True

    def test_either_side_dated_is_judgeable(self):
        from core.rules.parsing import _parse_time

        assert crosses_midnight("2025-01-22 14:12", "08:18",
                                _parse_time("2025-01-22 14:12", "2025-01-22"),
                                _parse_time("08:18", "2025-01-22")) is False

    def test_forward_order_is_not_midnight(self):
        from core.rules.parsing import _parse_time

        assert crosses_midnight("08:18", "14:12",
                                _parse_time("08:18", "2025-01-22"),
                                _parse_time("14:12", "2025-01-22")) is False

    def test_unparsed_side_is_not_midnight(self):
        assert crosses_midnight("14:12", "08:18", None, None) is False

    def test_wrap_under_20h_is_midnight(self):
        """p24 真实形态：14:12 → 08:18 隐含 18.1 小时（浓缩过夜）。"""
        from core.rules.parsing import _parse_time

        assert crosses_midnight("14时12分", "08时18分",
                                _parse_time("14时12分", "2025-01-22"),
                                _parse_time("08时18分", "2025-01-22")) is True

    def test_wrap_near_24h_is_not_midnight(self):
        """隐含 23.7 小时 ⇒ 跨零点解释力不够，仍按倒序处理。

        实测 `09:10 → 08:50`（同一天参数矩阵倒退）若被当成跨零点，等于把
        参数矩阵的行序错误**全部静默掉**。
        """
        from core.rules.parsing import _parse_time

        assert crosses_midnight("09:10", "08:50",
                                _parse_time("09:10", "2025-06-01"),
                                _parse_time("08:50", "2025-06-01")) is False


# ── CR-6：同一工序起止（#1482 形态）───────────────────────────────────
class TestInPageCrossMidnight:
    def test_p24_shape_not_reported(self):
        """p24 工序2 原样：14 时 12 分 → 08 时 18 分，两侧都无日期。"""
        pages = _pages([(24, [_step(2, "14 时 12 分", "08 时 18 分",
                                    operation="R2113 浓缩")],
                        {"production_date": "2015年01月22日"})])
        assert _check_time_reversal_in_page(pages) == []

    def test_dated_same_day_inversion_still_critical(self):
        """带日期的同日倒序是事实 ⇒ 仍 critical（不得被 CR-6 吞掉）。"""
        pages = _pages([(1, [_step(1, "2024-01-01 11:00", "2024-01-01 10:00")])])
        tr = _check_time_reversal_in_page(pages)
        assert len(tr) == 1 and tr[0]["severity"] == "critical"

    def test_dated_same_day_18h_inversion_still_critical(self):
        """两侧都带日期、同一天、倒退 18 小时 ⇒ 事实可判，**不得**当跨零点。

        这正是"日期闸门"存在的理由：18.1 小时的隐含时长在"跨零点"的解释
        范围之内，若不看字面日期就会被静默吞掉。
        """
        pages = _pages([(1, [_step(1, "2025-01-22 14:12", "2025-01-22 08:18")])])
        tr = _check_time_reversal_in_page(pages)
        assert len(tr) == 1
        assert tr[0]["severity"] == "critical"

    def test_one_side_dated_still_reported(self):
        """一侧有日期 ⇒ 可判，只是证据弱 ⇒ warning（不得静默消失）。"""
        pages = _pages([(1, [_step(1, "2024-01-01 11:00", "10:00")],
                         {"production_date": "2024-01-01"})])
        tr = _check_time_reversal_in_page(pages)
        assert len(tr) == 1 and tr[0]["severity"] == "warning"


# ── CR-26：准备类工序（#1483 形态）────────────────────────────────────
class TestPrepOperationExemption:
    def test_p10_prep_operation_not_reported(self):
        """p10 工序3「25%氯化钠溶液配制」10:15 早于 p9 工序2 的 11:04。"""
        pages = _pages([
            (9, [_step(2, "11:04", "19:09", operation="T2101a~d 柱控制泵出口压力<0.3MPa")],
             {"production_date": "2025-01-20"}),
            (10, [_step(3, "2025.01.20 10:15", "2025.01.20 10:27",
                        operation="25%氯化钠溶液配制：R2355 氯化钠配制罐加入 3m^3")],
             {"production_date": ""}),
        ])
        assert _check_time_reversal_cross_page(pages) == []

    def test_p33_non_prep_still_reported(self):
        """p33「干燥记录」不是准备类 ⇒ 必须保留（CR-26 只豁免准备类）。"""
        pages = _pages([
            (25, [_step(4, "08:35", "12:10", operation="")],
             {"production_date": "2025-01-25"}),
            (33, [_step("", "2025-01-24 00:21", "2025-01-25 02:32",
                        operation="干燥记录（每4小时±15分钟记录一次，日期更换时需标出）")],
             {"production_date": "2025-01-24"}),
        ])
        tr = _check_time_reversal_cross_page(pages)
        assert len(tr) == 1
        assert "干燥记录" in tr[0]["description"] or tr[0]["page"] == 33

    def test_prep_with_year_gap_still_reported(self):
        """准备类工序遇年份差 >2 ⇒ 那是提取错误，仍要报（豁免不覆盖它）。"""
        pages = _pages([
            (9, [_step(2, "11:04", "19:09", operation="T2101 柱控制泵")],
             {"production_date": "2025-01-20"}),
            (10, [_step(3, "2015-01-20 10:15", "2015-01-20 10:27",
                        operation="25%氯化钠溶液配制")],
             {"production_date": "2015-01-20"}),
        ])
        tr = _check_time_reversal_cross_page(pages)
        assert len(tr) == 1
        assert "年份" in tr[0]["description"]

    def test_cross_step_same_day_inversion_not_relaxed(self):
        """跨**工序**边界的同日倒序**不**按跨零点处理（守 B1-9 检测强度）。

        若按跨零点解释，"工序3 14:30 早于工序2 结束 16:00"要绕 22.5 小时 ——
        不成立。故本层保留原判据。
        """
        pages = _pages([
            (9, [_step(2, "15:00", "16:00")], {"production_date": "2024-01-01"}),
            (10, [_step(3, "14:30", "15:30")], {"production_date": "2024-01-01"}),
        ])
        tr = _check_time_reversal_cross_page(pages)
        assert len(tr) == 1
        assert tr[0]["severity"] == "warning"


class TestCrossPageAnchorMismatch:
    def test_p6_vs_p4_different_anchors_not_compared(self):
        """10 页 p6「清场 23:10」(签名日 05-20) 与 p4「工序10 13:00」(06-03)。

        页序与日期不同步，两页锚点不同且两侧都没写日期 ⇒ 不该拼成"倒序"。
        旧实现里 p6 的时刻解析不出来，这条根本不会产生；CR-5 补上锚点后
        必须同时补这条边界，否则"修好一个、长出一个"。
        """
        pages = _pages([
            (4, [_step(10, "13:00", "15:00", operation="过筛")],
             {"production_date": "2025-06-03"}),
            (6, [_step("", "23:10", "23:52", operation="称量岗位清场")],
             {"production_date": ""}),
        ])
        # p6 无生产日期，锚点来自签名日期
        pages[1]["steps"][0]["signatures"] = [
            {"role": "operator", "sign_time": "2025.05.20"},
        ]
        assert _check_time_reversal_cross_page(pages) == []

    def test_dated_side_keeps_reporting_p33_vs_p25(self):
        """CR-26 的保留项：p33（带日期）vs p25 仍要报。

        两页锚点不同（01-24 / 01-25），但 curr 自带日期 ⇒ 事实在字面上，照比。
        """
        pages = _pages([
            (25, [_step(4, "08:35", "12:10")], {"production_date": "2025-01-25"}),
            (33, [_step("", "2025-01-24 00:21", "2025-01-25 02:32",
                        operation="干燥记录")],
             {"production_date": "2025-01-24"}),
        ])
        tr = _check_time_reversal_cross_page(pages)
        assert len(tr) == 1
        assert tr[0]["page"] == 33


# ── CR-6：参数矩阵测量行（#1511 / #1510 / #1512 形态）────────────────
class TestMeasurementSequence:
    def test_p11_midnight_rows_not_reported(self):
        """p11 工序6：22:03 / 23:04 / 00:04 / 00:35 是一夜连续取样。"""
        pages = _pages([(11, [_step(6, measurements=["22:03", "23:04", "00:04", "00:35"])],
                         {"production_date": "2025-01-20"})])
        assert _check_measurement_time_sequence(pages) == []

    def test_p33_midnight_rows_not_reported(self):
        pages = _pages([(33, [_step("", measurements=["16:22", "20:21", "00:17", "04:19"])],
                         {"production_date": "2025-01-24"})])
        assert _check_measurement_time_sequence(pages) == []

    def test_same_day_reversal_still_reported(self):
        """同日倒退（字面自带日期，非跨零点）仍是缺陷 —— 豁免不能变成"矩阵永不复核"。"""
        pages = _pages([(11, [_step(6, measurements=[
            "2025-01-20 11:54", "2025-01-20 12:37", "2025-01-20 10:02",
        ])], {"production_date": "2025-01-20"})])
        tr = _check_measurement_time_sequence(pages)
        assert len(tr) == 1
        assert "10:02" in tr[0]["description"]

    def test_p38_different_anchor_pages_not_compared(self):
        """p22 工序3（01-22）与 p38 工序3（01-20）是不同岗位的同号工序。"""
        pages = _pages([
            (22, [_step(3, measurements=["11:54", "12:50"])], {"production_date": "2025-01-22"}),
            (38, [_step(3, measurements=["17:29", "19:15"])], {"production_date": "2025.01.20"}),
        ])
        assert _check_measurement_time_sequence(pages) == []

    def test_anchor_mismatch_is_what_suppresses_it(self):
        """把跨零点这条解释**排除掉**之后，仍不报 —— 证明是锚点闸门在起作用。

        构造：p22 08:00（锚点 01-22）→ p38 17:29（锚点 01-20）。钟点上是
        **前进**的（08:00 < 17:29），跨零点分支不会命中；只有"两页锚点不同"
        能解释"为什么不报"。
        """
        pages = _pages([
            (22, [_step(3, measurements=["08:00", "08:00"])], {"production_date": "2025-01-22"}),
            (38, [_step(3, measurements=["17:29", "19:15"])], {"production_date": "2025.01.20"}),
        ])
        assert _check_measurement_time_sequence(pages) == []

    def test_same_anchor_cross_page_reversal_still_reported(self):
        """同一日期跨页的矩阵仍逐行核对（豁免只针对"锚点不同"）。"""
        pages = _pages([
            (20, [_step(2, measurements=["2025-01-20 10:00", "2025-01-20 12:00"])],
             {"production_date": "2025-01-20"}),
            (21, [_step(2, measurements=["2025-01-20 09:00", "2025-01-20 11:00"])],
             {"production_date": "2025-01-20"}),
        ])
        tr = _check_measurement_time_sequence(pages)
        assert len(tr) == 1
        assert "09:00" in tr[0]["description"]


# ── CR-5：文案必须说实话 ─────────────────────────────────────────────
class TestUnparseableWording:
    def test_missing_anchor_says_cannot_check_not_format_error(self):
        """无锚点 + 纯时刻 ⇒ 必须说"无法核对"，**不得**说"格式无法解析"。"""
        pages = _pages([(1, [_step(1, "12:00", "14:00", operation="称量岗位清场")], {})])
        tr = _check_time_reversal_cross_page(pages)
        assert len(tr) == 1
        desc = tr[0]["description"]
        assert "无法核对时间顺序" in desc
        assert "未找到可用于定位日期" in desc
        assert "时间格式无法解析" not in desc

    def test_garbage_literal_still_says_format_error(self):
        """字面量真的是坏的（无时刻可读）⇒ 仍报格式错，不要张冠李戴。"""
        pages = _pages([(1, [_step(1, "待补", "见附页", operation="称量岗位清场")], {})])
        tr = _check_time_reversal_cross_page(pages)
        assert len(tr) == 1
        assert "时间格式无法解析" in tr[0]["description"]

    def test_anchor_from_signature_removes_the_noise(self):
        """签名日期能补出锚点 ⇒ 12:00→14:00 正常解析，这条噪声整体消失。"""
        pages = _pages([(1, [_step(1, "12:00", "14:00", operation="称量岗位清场")], {})])
        pages[0]["steps"][0]["signatures"] = [
            {"role": "reviewer", "sign_time": "2025.06.03"},
        ]
        assert _check_time_reversal_cross_page(pages) == []


# ── CR-6：LLM 侧的同一断言（#1426）───────────────────────────────────
class TestGuardCrossMidnightClaim:
    @pytest.mark.parametrize("text", [
        "浓缩开始时间（14:12）晚于浓缩结束时间（08:18），时间逻辑颠倒。",
        "浓缩结束时间（08:18）早于浓缩开始时间（14:12），时间逻辑颠倒。",
        "吹氮气 23时04分 与 00时04分 存在时间倒置",
    ])
    def test_clock_only_claims_are_recognised(self, text):
        assert _is_cross_midnight_clock_claim(text) is True

    @pytest.mark.parametrize("text", [
        # 带日期 ⇒ 那一天是事实，同一天倒序照报
        "开始时间 2025-01-20 14:12 晚于结束时间 2025-01-20 08:18",
        # 带日号 ⇒ 同上（p45 形态，CR-6 不覆盖）
        "水洗结束时间'12时09分'早于开始时间'18日09时07分'",
        # 三个时刻不是本形态
        "12:00、14:00、16:00 三点顺序矛盾",
        # 跨页（两道不同工序的边界）不是本形态 —— 实测 10 页 p6
        "第6页 工序 开始(23:10) 早于第4页 工序10 开始(13:00)，工序编号顺序与开始时刻矛盾",
        # 没有先后关系词
        "时间格式无法解析（start=12:00 end=14:00）",
        "",
    ])
    def test_reverse_controls(self, text):
        assert _is_cross_midnight_clock_claim(text) is False

    def test_single_page_reference_still_matches(self):
        """页内文案只带一个"第N页" ⇒ 仍是同一工序内部，照常识别。"""
        assert _is_cross_midnight_clock_claim(
            "第24页 工序2 开始时间(14 时 12 分) 晚于结束时间(08 时 18 分)"
        ) is True

    def test_guard_suppresses_llm_time_reversal(self):
        rows = [{
            "page": 24, "type": "time_reversal", "severity": "critical",
            "source": "llm_page",
            "description": "浓缩开始时间（14:12）晚于浓缩结束时间（08:18），时间逻辑颠倒。",
        }]
        kept, _down, sup = review_llm_findings(rows, structured_by_page={})
        assert kept == []
        assert len(sup) == 1
        assert "跨零点" in sup[0]["reason"]

    def test_guard_covers_user_rule_too(self):
        rows = [{
            "page": 24, "type": "user_rule", "severity": "critical",
            "source": "user_rule",
            "description": "开始 14:12 晚于 结束 08:18",
        }]
        kept, _down, sup = review_llm_findings(rows, structured_by_page={})
        assert kept == [] and len(sup) == 1

    def test_guard_does_not_touch_dated_claim(self):
        """跨日（日期不同）的倒序断言字面上就成立 ⇒ 不得被跨零点豁免吞掉。"""
        rows = [{
            "page": 1, "type": "time_reversal", "severity": "warning",
            "source": "llm_page",
            "description": "开始时间 2025-01-21 14:12 晚于结束时间 2025-01-20 08:18",
        }]
        kept, down, sup = review_llm_findings(rows, structured_by_page={})
        assert sup == []
        assert len(kept) + len(down) == 1

    def test_guard_does_not_touch_other_types(self):
        """类型白名单是防越界：参数超差文案里出现两个时刻不该被吞。"""
        rows = [{
            "page": 1, "type": "param_out_of_spec", "severity": "warning",
            "source": "llm_page",
            "description": "12:00 与 14:00 两次读数矛盾",
        }]
        kept, _down, _sup = review_llm_findings(rows, structured_by_page={})
        assert len(kept) == 1
