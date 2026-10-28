"""B1-7：R4 `suspicious_date` 的**分析基准年**护栏。

原缺陷：`_check_suspicious_dates` 用 `datetime.now().year` 作上界 ⇒
**同一份 PDF 在不同年份重分析会得到不同结论**（finding 不可复现）。

判据方向本身是对的（记录里出现未来日期确实可疑），修的是**基准固化**：
默认取记录中解析出的生产年份；缺失时回退当前年**并在 finding 里注明**。
"""
from __future__ import annotations

import datetime as _dt

import pytest

from core.rules import rule_time

PAGE = dict


def _page(pno: int, prod: str | None, start: str | None = None,
          end: str | None = None) -> dict:
    return {
        "page": pno,
        "page_info": {"production_date": prod} if prod else {},
        "steps": [{"start_time": start or "", "end_time": end or ""}],
    }


class _Clock:
    """假的 datetime —— 只提供 `.now()`，用于把"系统时间"钉死。"""

    def __init__(self, year: int) -> None:
        self._year = year

    def now(self):  # noqa: ANN201
        return _dt.datetime(self._year, 6, 1, 12, 0, 0)


@pytest.fixture
def freeze_clock(monkeypatch):
    def _set(year: int) -> None:
        monkeypatch.setattr(rule_time, "datetime", _Clock(year))
    return _set


# ── ① 解析记录里的生产年份 ───────────────────────────────────────────────────


class TestRecordProductionYear:
    def test_majority_wins(self):
        pages = [_page(1, "2025-09-25"), _page(2, "2025-09-26"), _page(3, "2026-01-01")]
        assert rule_time.record_production_year(pages) == 2025

    def test_tie_picks_smaller_year_deterministically(self):
        """平票取较小年份 —— 让结果与 dict/页序**无关**（可复现的前提）。"""
        a = [_page(1, "2025-01-01"), _page(2, "2026-01-01")]
        b = list(reversed(a))
        assert rule_time.record_production_year(a) == rule_time.record_production_year(b) == 2025

    def test_none_when_no_production_date(self):
        assert rule_time.record_production_year([_page(1, None)]) is None

    def test_unparseable_production_date_is_ignored(self):
        assert rule_time.record_production_year([_page(1, "无日期")]) is None


# ── ② 基准年优先级 ───────────────────────────────────────────────────────────


class TestBaselineYear:
    def test_record_year_wins_over_clock(self):
        assert rule_time._baseline_year([_page(1, "2025-01-01")]) == (2025, False)

    def test_explicit_override_wins_over_record(self):
        assert rule_time._baseline_year([_page(1, "2025-01-01")], 2019) == (2019, False)

    def test_fallback_marks_itself_inferred(self, freeze_clock):
        freeze_clock(2026)
        year, inferred = rule_time._baseline_year([_page(1, None)])
        assert (year, inferred) == (2026, True)


# ── ③ 决定性：基准不随系统时间漂移 ───────────────────────────────────────────


class TestFindingsDoNotDriftWithTheClock:
    """这两条是本条的**核心判据**（也是变异验证的靶子）。"""

    def test_result_is_the_record_year_baseline_not_the_clock(self, freeze_clock):
        """记录是 2025 年生产的，系统时间设到 2099 ⇒ **仍按 2025 判**。

        2027 这个日期：基准 2025 ⇒ 上界 2026 ⇒ **命中**；
        若判据退回 `datetime.now()`（2099 ⇒ 上界 2100）⇒ **不命中**。
        ⇒ 这条断言能把"基准随系统时间漂移"的回归直接打红。
        """
        freeze_clock(2099)
        pages = [_page(1, "2025-01-01", start="2027-03-03 10:00", end="2025-01-01 11:00")]
        findings = rule_time._check_suspicious_dates(pages)
        assert [f["type"] for f in findings] == ["suspicious_date"]
        assert "2027" in findings[0]["ocr_text"]

    def test_two_different_clock_times_give_identical_findings(self, freeze_clock):
        """spec 验收：固定基准年 + 两个不同系统时间 ⇒ finding **完全一致**。"""
        pages = [
            _page(1, "2025-01-01", start="2027-03-03 10:00", end="2025-01-01 11:00"),
            _page(2, "2025-01-02", start="1999-12-31 08:00", end=""),
        ]
        freeze_clock(2026)
        first = rule_time._check_suspicious_dates(pages)
        freeze_clock(2099)
        second = rule_time._check_suspicious_dates(pages)
        assert first == second
        assert len(first) == 2, "两个异常日期都应命中（否则断言退化成空集相等）"

    def test_no_false_positive_for_in_record_dates(self):
        """**正向对照**：记录自身的日期不得被判可疑（否则上面的"命中"无意义）。"""
        pages = [_page(1, "2025-01-01", start="2025-01-01 08:00", end="2025-01-01 09:00")]
        assert rule_time._check_suspicious_dates(pages) == []


# ── ④ 回退必须"说出来" ───────────────────────────────────────────────────────


class TestFallbackIsDisclosed:
    def test_description_names_the_inferred_baseline(self, freeze_clock):
        freeze_clock(2026)
        pages = [_page(1, None, start="2099-05-05 10:00", end="")]
        findings = rule_time._check_suspicious_dates(pages)
        assert findings and "基准年取当前年份" in findings[0]["description"]

    def test_description_does_not_claim_inference_when_record_has_year(self):
        """反向：记录里有生产年份时**不得**出现"回退"字样（否则是误导）。"""
        pages = [_page(1, "2025-01-01", start="2030-05-05 10:00", end="")]
        findings = rule_time._check_suspicious_dates(pages)
        assert findings and "基准年取当前年份" not in findings[0]["description"]


# ── ⑥ 固化基准**不得**静默削弱检测（本轮自己引入的风险点） ───────────────────


class TestAbsoluteFutureIsStillCaught:
    """基准取自记录后，"生产年+1"成了**自指上界** —— 记录整体落在未来就永远判不出来。

    这是修 B1-7 最容易踩的坑：为了可复现而丢掉绝对判据 = **静默削弱检测**。
    故必须**同时**保留一条绝对判据，并如实声明它依赖运行时刻。
    """

    def test_self_consistent_record_from_the_far_future_is_flagged(self, freeze_clock):
        freeze_clock(2026)
        pages = [_page(1, "2036-06-15",
                       start="2036-06-15 08:00", end="2036-06-15 09:00")]
        findings = rule_time._check_suspicious_dates(pages)
        assert findings, "记录整体落在未来却零命中 —— 检测被静默削弱"
        assert all("按运行时刻判断" in f["description"] for f in findings), (
            "绝对判据必须**如实声明**它依赖运行时刻，不能假装可复现"
        )

    def test_absolute_clause_does_not_fire_for_a_normal_record(self, freeze_clock):
        """**正向对照**：正常记录不得被绝对判据误伤（否则上条无判别力）。"""
        freeze_clock(2026)
        pages = [_page(1, "2025-01-01", start="2025-01-01 08:00", end="2025-01-01 09:00")]
        assert rule_time._check_suspicious_dates(pages) == []

    def test_reason_strings_are_distinguishable(self):
        """两条判据的依据文案必须**可区分** —— 复核者要看得出是哪条命中的。"""
        assert rule_time._suspicion_reason(1999, 2026, 2027) == "早于2000"
        assert rule_time._suspicion_reason(2027, 2026, 2027) == "晚于2026"
        abs_reason = rule_time._suspicion_reason(2036, 2037, 2027)
        assert abs_reason is not None and "按运行时刻判断" in abs_reason
        assert rule_time._suspicion_reason(2026, 2026, 2027) is None



class TestExplicitAnalysisYear:
    def test_override_changes_the_verdict(self):
        pages = [_page(1, "2025-01-01", start="2027-03-03 10:00", end="")]
        assert rule_time._check_suspicious_dates(pages, analysis_year=2025)  # 上界 2026
        assert rule_time._check_suspicious_dates(pages, analysis_year=2030) == []  # 上界 2031

    def test_override_beats_record_and_clock(self, freeze_clock):
        freeze_clock(2099)
        pages = [_page(1, "2025-01-01", start="2030-05-05 10:00", end="")]
        assert rule_time._check_suspicious_dates(pages, analysis_year=1999)  # 上界 2000
