"""`core/rules/year_vote.py` + R3 接线单测。

背景（真实 51 页轮次 `0c5cfb00-897` 实测）：形如 `[2015, 2025]` 的
`year_contradiction` 里，少数年份与多数年份**只差一位数字**，且页支持度远低于
多数 —— 是同一次单字符误读在多页重复。更糟的是 R1-b/R5/R9a 用 `year_delta > 2`
把同一次误读再报一遍（"2015 级联"）。

本文件锁定三件事：
1. **投票判据的五道闸**各自都能否掉不该归一的输入（保守性）；
2. **跨年记录保护** —— 相邻年（2024/2025）永不被归一；
3. **接线** —— 四个发射点确实因归一而抑制，且未归一的情形**行为不变**。
"""
from __future__ import annotations

import pytest

from core.rules.parsing import _edit_distance_le1
from core.rules.rule_time import (
    _check_signature_order,
    _check_signature_time_anomaly,
    _check_time_reversal_cross_page,
    _check_year_contradiction,
)
from core.rules.year_vote import (
    YearVote,
    batch_year,
    build_year_vote,
    date_strings,
    single_digit_variant,
    years_in_text,
)

# ---------------------------------------------------------------------------
# 基础纯函数
# ---------------------------------------------------------------------------


class TestSingleDigitVariant:
    @pytest.mark.parametrize("a,b,expected", [
        (2015, 2025, True),   # 第 3 位 1↔2（真实误读形态）
        (2020, 2025, True),
        (2010, 2020, True),
        (2018, 2025, False),  # 差 2 位 → 很可能是真实的模板年份（如文件 2018 年审核）
        (2013, 2025, False),
        (1999, 2025, False),
        (2025, 2025, False),
    ])
    def test_variant(self, a, b, expected):
        assert single_digit_variant(a, b) is expected


class TestBatchYear:
    @pytest.mark.parametrize("raw,expected", [
        ("1127011N250101", 2025),
        ("1127011N250101-04", 2025),
        ("1127011N^*250101", 2025),
        ("A010297-M250104", 2025),
        ("", None),
        (None, None),
        ("1127011", None),
        ("1127011N2501017800L", None),  # 年份段后紧跟数字 → 不误判
    ])
    def test_batch_year(self, raw, expected):
        assert batch_year(raw) == expected


class TestYearsInText:
    def test_extracts_plausible_years(self):
        assert years_in_text("生产日期 2025.01.20") == {2025}
        assert years_in_text("2015.01 与 2025.01") == {2015, 2025}

    def test_ignores_digits_inside_long_runs(self):
        # 批号里的 2501 后紧跟 01 → 不是年份，不得命中
        assert years_in_text("1127011N250101") == set()

    def test_ignores_out_of_range(self):
        assert years_in_text("编号 1234 与 9999") == set()


class TestCollectorEquivalence:
    """机检漂移：投票看到的年份集合必须与规则实际比较的年份集合同源。"""

    def test_collector_matches_rule_time(self):
        from core.rules.rule_time import _collect_all_date_strings

        page = {
            "page": 3,
            "page_info": {"production_date": "2025-01-20"},
            "steps": [
                {"start_time": "2025-01-21 08:00", "end_time": "2025-01-21 12:00",
                 "signatures": [{"sign_time": "2025-01-21 13:00"},
                                {"sign_time": None}]},
                {"start_time": None, "end_time": "2025-01-22"},
            ],
            "event_year_groups": {"production": [2025], "review": []},
        }
        base = _collect_all_date_strings(page)
        extra = date_strings(page)
        # date_strings 是 rule_time 的超集：额外含 event_year_groups
        for s in base:
            assert s in extra
        assert "2025" in extra


# ---------------------------------------------------------------------------
# 投票对象：五道闸各自都要能"否掉"
# ---------------------------------------------------------------------------


def _pages_with(years_by_page: dict[int, list[int]], batch_no: str | None = "1127011N250101"):
    """构造最小页面集：每页把年份放进 event_year_groups.production。"""
    return [
        {
            "page": p,
            "page_info": {"batch_no": batch_no or "", "production_date": f"{y}-01-20"},
            "steps": [],
            "event_year_groups": {"production": [y]},
        }
        for p, ys in years_by_page.items()
        for y in ys
    ]


class TestResolveGates:
    def _vote(self, majority_pages: int, minority_pages: int, *, majority=2025,
              minority=2015, batch=True):
        pages = _pages_with(
            {p: [majority] for p in range(1, majority_pages + 1)},
            batch_no="1127011N250101" if batch else None,
        )
        pages += _pages_with(
            {100 + p: [minority] for p in range(1, minority_pages + 1)},
            batch_no="1127011N250101" if batch else None,
        )
        return build_year_vote(pages)

    def test_normalized_when_evidenced_and_decisive(self):
        vote = self._vote(20, 3)
        assert vote.majority == 2025
        assert vote.batch_years == (2025,)
        resolved, reason = vote.resolve(2015)
        assert resolved == 2025 and reason and "批号佐证" in reason

    def test_adjacent_year_never_normalized(self):
        """跨年记录保护：2024 与 2025 相邻，即便单字符差异且支持度低也不归一。"""
        vote = self._vote(20, 1, minority=2024)
        resolved, reason = vote.resolve(2024)
        assert resolved == 2024 and reason is None

    def test_two_digit_difference_never_normalized(self):
        vote = self._vote(20, 1, minority=2013)
        resolved, reason = vote.resolve(2013)
        assert resolved == 2013 and reason is None

    def test_no_evidence_and_multipage_minority_kept(self):
        """无批号佐证时只归一"孤页读法"；出现在 2 页以上则保留。"""
        vote = self._vote(20, 2, batch=False)
        resolved, reason = vote.resolve(2015)
        assert resolved == 2015 and reason is None

    def test_weak_evidence_but_single_page_normalized(self):
        vote = self._vote(20, 1, batch=False)
        resolved, reason = vote.resolve(2015)
        assert resolved == 2025 and reason and "孤页读法" in reason

    def test_small_majority_base_not_normalized(self):
        """多数只有 2 页 → 基数不足，不归一。"""
        vote = self._vote(2, 1)
        resolved, reason = vote.resolve(2015)
        assert resolved == 2015 and reason is None

    def test_indecisive_vote_not_normalized(self):
        """少数读法页数 ≥ 阈值（多数 20 页 × 0.35 = 7）→ 投票不决定性，保留。"""
        vote = self._vote(20, 8)
        resolved, reason = vote.resolve(2015)
        assert resolved == 2015 and reason is None

    def test_batch_evidenced_year_itself_never_normalized(self):
        """批号佐证的年份有独立证据，不得被"投票"吞掉。"""
        vote = self._vote(20, 1, minority=2025, majority=2015)
        resolved, reason = vote.resolve(2025)
        assert resolved == 2025 and reason is None

    def test_tie_has_no_majority(self):
        pages = _pages_with({1: [2025], 2: [2015]}, batch_no=None)
        vote = build_year_vote(pages)
        assert vote.majority is None
        assert vote.resolve(2015) == (2015, None)

    def test_deterministic(self):
        pages = _pages_with({p: [2025] for p in range(1, 21)}, batch_no="1127011N250101")
        assert build_year_vote(pages) == build_year_vote(pages)

    def test_empty_returns_no_majority(self):
        vote = build_year_vote([])
        assert vote == YearVote(support={}, majority=None, batch_years=())
        assert vote.resolve(2025) == (2025, None)
        assert vote.resolve(None) == (None, None)


# ---------------------------------------------------------------------------
# 接线：四个发射点
# ---------------------------------------------------------------------------


def _contradiction_pages(majority_pages: int, minority: list[int]):
    """多数页 production=2025 + 1 页含 [2025, minority] 的歧义。"""
    pages = [
        {"page": p, "page_info": {"batch_no": "1127011N250101"},
         "steps": [], "event_year_groups": {"production": [2025]}}
        for p in range(1, majority_pages + 1)
    ]
    pages.append({
        "page": 99, "page_info": {"batch_no": "1127011N250101"}, "steps": [],
        "event_year_groups": {"production": [minority[0], minority[1]]},
    })
    return pages


class TestYearContradictionWiring:
    def test_ocr_variant_does_not_emit(self):
        """真实形态：[2015, 2025] 单字符误读 + 批号佐证 → 不再报矛盾。"""
        assert _check_year_contradiction(_contradiction_pages(20, [2025, 2015])) == []

    def test_genuine_two_year_still_emitted(self):
        """真实双年份（差 2 位）必须照旧报出 —— 归一不得吃掉真信号。"""
        findings = _check_year_contradiction(_contradiction_pages(20, [2025, 2013]))
        assert len(findings) == 1
        assert findings[0]["type"] == "year_contradiction"
        assert "[2013, 2025]" in findings[0]["description"]

    def test_single_year_unaffected(self):
        pages = _pages_with({p: [2025] for p in range(1, 6)})
        assert _check_year_contradiction(pages) == []


def _sig_pages(prod_year: str, sign_year: str, *, batch="1127011N250101"):
    """多数页 production=2025 + 一页签名时间在 sign_year（早于操作时间）。"""
    pages = [
        {"page": p, "page_info": {"batch_no": batch, "production_date": "2025-01-20"},
         "steps": [], "event_year_groups": {}}
        for p in range(1, 21)
    ]
    pages.append({
        "page": 99,
        "page_info": {"batch_no": batch, "production_date": prod_year},
        "event_year_groups": {},
        "steps": [{
            "step_no": 1,
            "start_time": "2025-01-21 10:00",
            "signatures": [{"role": "operator", "name": "张三",
                            "sign_time": sign_year}],
        }],
    })
    return pages


class TestSignatureRulesWiring:
    def test_signature_time_anomaly_suppressed_when_year_misread(self):
        """签名年 2015 实为 2025 的误读 → year_delta>2 的 finding 不再发射。"""
        assert _check_signature_time_anomaly(_sig_pages("2015-01-21", "2015-01-21 08:00")) == []

    def test_signature_time_anomaly_kept_for_real_gap(self):
        """差 2 位的真实年份差 → 照旧发射（含"年份相差"提示）。"""
        findings = _check_signature_time_anomaly(
            _sig_pages("2013-01-21", "2013-01-21 08:00")
        )
        assert len(findings) == 1
        assert "年份相差" in findings[0]["description"]

    def test_signature_order_suppressed_when_year_misread(self):
        pages = _sig_pages("2015-01-21", "2015-01-21 08:00")
        step = pages[-1]["steps"][0]
        step["signatures"] = [
            {"role": "operator", "name": "张三", "sign_time": "2025-01-21 10:00"},
            {"role": "reviewer", "name": "李四", "sign_time": "2015-01-21 08:00"},
        ]
        assert _check_signature_order(pages) == []

    def test_signature_order_kept_for_real_gap(self):
        pages = _sig_pages("2013-01-21", "2013-01-21 08:00")
        step = pages[-1]["steps"][0]
        step["signatures"] = [
            {"role": "operator", "name": "张三", "sign_time": "2025-01-21 10:00"},
            {"role": "reviewer", "name": "李四", "sign_time": "2013-01-21 08:00"},
        ]
        findings = _check_signature_order(pages)
        assert len(findings) == 1
        assert "年份相差" in findings[0]["description"]


class TestTimeReversalWiring:
    def _pages(self, first_year: str, second_year: str):
        """多数页 production_date=2025-01-20（投票的基数来源）+ 两页跨页时间序。

        注意：投票的年份证据来自**日期串**，故占位页必须带 `production_date`
        —— 真实文档里这类页正是年份证据的来源（无日期的页对投票无贡献）。
        """
        pages = [
            {"page": p, "page_info": {"batch_no": "1127011N250101",
                                      "production_date": "2025-01-20"},
             "steps": [], "event_year_groups": {}}
            for p in range(3, 23)
        ]
        pages.append({
            "page": 1, "page_info": {"batch_no": "1127011N250101",
                                     "production_date": "2025-01-20"},
            "event_year_groups": {},
            "steps": [{"step_no": 1, "start_time": f"{first_year} 08:00",
                       "end_time": f"{first_year} 12:00", "signatures": []}],
        })
        pages.append({
            "page": 2, "page_info": {"batch_no": "1127011N250101",
                                     "production_date": "2025-01-20"},
            "event_year_groups": {},
            "steps": [{"step_no": 2, "start_time": f"{second_year} 09:00",
                       "end_time": f"{second_year} 10:00", "signatures": []}],
        })
        return pages

    def test_cross_page_reversal_suppressed_when_year_misread(self):
        findings = _check_time_reversal_cross_page(
            self._pages("2025-01-21", "2015-01-21")
        )
        assert [f for f in findings if "年份相差" in f["description"]] == []

    def test_cross_page_reversal_kept_for_real_year_gap(self):
        """差 2 位的真实年份差 → 照旧发射（含"年份相差"提示）。"""
        findings = _check_time_reversal_cross_page(
            self._pages("2025-01-21", "2013-01-21")
        )
        assert len([f for f in findings if "年份相差" in f["description"]]) == 1


# ---------------------------------------------------------------------------
# 批号归一（R2 投票）—— 编辑距离是判据的一部分，单独锁定
# ---------------------------------------------------------------------------


class TestEditDistanceLe1:
    @pytest.mark.parametrize("a,b,expected", [
        ("1127011N250101", "1127011N250101", True),
        ("1127011N250101", "11270111250101", True),    # 单字符替换 N↔1
        ("1127011N250101", "127011N250101", True),     # 单字符删除
        ("1127011N250101", "1127011N2501011", True),   # 单字符插入
        ("1127011N250101", "1127011X250101", True),    # 任意单字符
        ("1127011N250101", "11270112X0101", False),    # 两处差异
        ("1127011N250101", "2245DP20260115", False),   # 长度 + 内容全变
        ("", "", True),
    ])
    def test_edit_distance_le1(self, a, b, expected):
        assert _edit_distance_le1(a, b) is expected
