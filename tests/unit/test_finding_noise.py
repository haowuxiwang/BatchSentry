"""core/finding_noise.py 单测（M2 降噪）。

覆盖：结构性 kind 识别、抽取不确定降级、文档级聚合阈值、非目标类型零改动、
报告字段与确定性；并锁定 rule_doc 的标记契约（防止降噪静默失效）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.finding_noise import (
    STRUCTURAL_MARKERS,
    reduce_completeness_noise,
    structural_kind,
)

_REPO = Path(__file__).resolve().parents[2]


def _c(page: int, ocr_text: str, severity: str = "warning") -> dict:
    return {"page": page, "type": "completeness", "severity": severity,
            "description": "x", "ocr_text": ocr_text, "source": "rule"}


class TestStructuralKind:
    @pytest.mark.parametrize("marker,kind", [
        ("step_no=1 time=空", "time"),
        ("step_no=1 operator=空", "operator"),
        ("step_no=1 reviewer=空", "reviewer"),
        ("step_no=1 qa=空", "qa"),
    ])
    def test_markers(self, marker, kind):
        assert structural_kind(_c(1, marker)) == kind

    def test_non_completeness_is_none(self):
        f = {"type": "time_reversal", "ocr_text": "step_no=1 time=空"}
        assert structural_kind(f) is None

    def test_completeness_without_marker_is_none(self):
        assert structural_kind(_c(1, "其它说明")) is None

    def test_missing_ocr_text_is_none(self):
        assert structural_kind({"type": "completeness"}) is None


class TestPassThrough:
    def test_non_structural_untouched(self):
        other = {"page": 1, "type": "time_reversal", "severity": "critical",
                 "description": "y", "ocr_text": "", "source": "rule"}
        kept, rep = reduce_completeness_noise([other], total_pages=5)
        assert kept == [other]
        assert rep["aggregated_total"] == 0 and rep["downgraded_extraction_uncertain"] == 0

    def test_small_group_not_aggregated(self):
        items = [_c(p, "step_no=1 time=空") for p in range(1, 5)]
        kept, rep = reduce_completeness_noise(items, total_pages=10)
        assert len(kept) == 4 and rep["aggregated"] == {}


class TestExtractionDowngrade:
    def test_time_warning_on_flagged_page_downgraded(self):
        items = [_c(1, "step_no=1 time=空", "warning")]
        kept, rep = reduce_completeness_noise(
            items, flagged_pages={1}, total_pages=10)
        assert kept[0]["severity"] == "info"
        assert kept[0]["extraction_uncertain"] is True
        assert rep["downgraded_extraction_uncertain"] == 1

    def test_time_on_unflagged_page_not_downgraded(self):
        items = [_c(1, "step_no=1 time=空", "warning")]
        kept, _ = reduce_completeness_noise(items, flagged_pages={2}, total_pages=10)
        assert kept[0]["severity"] == "warning"
        assert "extraction_uncertain" not in kept[0]

    def test_non_extraction_kind_not_downgraded(self):
        items = [_c(1, "step_no=1 operator=空", "warning")]
        kept, rep = reduce_completeness_noise(
            items, flagged_pages={1}, total_pages=10)
        assert kept[0]["severity"] == "warning"
        assert rep["downgraded_extraction_uncertain"] == 0

    def test_originals_not_mutated(self):
        original = _c(1, "step_no=1 time=空", "warning")
        reduce_completeness_noise([original], flagged_pages={1}, total_pages=10)
        assert original["severity"] == "warning"  # 不可变


class TestAggregation:
    def test_pervasive_kind_aggregated(self):
        # 10 条覆盖 5 页 / 共 5 页 → 条数≥8 且广度≥30%
        items = [_c(p, "step_no=1 time=空") for p in [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]]
        kept, rep = reduce_completeness_noise(items, total_pages=5)
        comp = [f for f in kept if f["type"] == "completeness"]
        assert len(comp) == 1 and comp[0]["aggregated"] is True
        assert comp[0]["page_list"] == [1, 2, 3, 4, 5]
        assert "10 处" in comp[0]["description"]
        assert rep["aggregated"]["time"]["count"] == 10

    def test_below_page_ratio_kept_individual(self):
        # 10 条但只占 1/10 页（<30%）→ 不聚合
        items = [_c(1, "step_no=1 time=空") for _ in range(10)]
        kept, rep = reduce_completeness_noise(items, total_pages=10)
        assert len(kept) == 10 and rep["aggregated"] == {}

    def test_multiple_kinds_aggregate_independently(self):
        items = ([_c(p, "step_no=1 time=空") for p in range(1, 6)] * 2 +
                 [_c(p, "step_no=1 qa=空", "info") for p in range(1, 6)] * 2)
        kept, rep = reduce_completeness_noise(items, total_pages=5)
        assert set(rep["aggregated"]) == {"time", "qa"}
        assert len(kept) == 2  # 两条摘要

    def test_non_structural_preserved_alongside_aggregation(self):
        items = ([_c(p, "step_no=1 time=空") for p in range(1, 6)] * 2 +
                 [{"page": 1, "type": "time_reversal", "severity": "critical",
                   "description": "z", "ocr_text": "", "source": "rule"}])
        kept, _ = reduce_completeness_noise(items, total_pages=5)
        assert any(f["type"] == "time_reversal" for f in kept)

    def test_deterministic(self):
        items = [_c(p, "step_no=1 time=空") for p in range(1, 6)] * 2
        a, _ = reduce_completeness_noise(items, total_pages=5)
        b, _ = reduce_completeness_noise(items, total_pages=5)
        assert a == b


class TestSpecUnverifiableGrouping:
    """M2：spec_unverifiable 也参与聚合（按 type 归组）。"""

    @staticmethod
    def _su(page: int) -> dict:
        return {"page": page, "type": "spec_unverifiable", "severity": "warning",
                "description": "d", "ocr_text": "PN: spec= actual=3.0",
                "source": "rule"}

    def test_grouped_and_aggregated(self):
        items = [self._su(p) for p in range(1, 6)] * 2  # 10 条 / 5 页
        kept, rep = reduce_completeness_noise(items, total_pages=5)
        assert "spec_unverifiable" in rep["aggregated"]
        assert rep["aggregated"]["spec_unverifiable"]["count"] == 10
        assert len(kept) == 1 and kept[0]["type"] == "spec_unverifiable"
        assert kept[0]["aggregated"] is True

    def test_not_grouped_when_sparse(self):
        items = [self._su(1) for _ in range(3)]  # 条数 < min_group
        kept, rep = reduce_completeness_noise(items, total_pages=50)
        assert rep["aggregated"] == {} and len(kept) == 3

    def test_param_out_of_spec_never_aggregated(self):
        """高价值越界项绝不被聚合掉。"""
        items = [{"page": p, "type": "param_out_of_spec",
                  "severity": "critical", "description": "d",
                  "ocr_text": "", "source": "rule"} for p in range(1, 21)]
        kept, rep = reduce_completeness_noise(items, total_pages=5)
        assert len(kept) == 20 and rep["aggregated"] == {}


class TestMarkerContract:
    def test_rule_doc_still_emits_markers(self):
        """降噪依赖 rule_doc 的 ocr_text 标记；标记漂移则本测试失败。"""
        src = (_REPO / "core" / "rules" / "rule_doc.py").read_text(encoding="utf-8")
        for marker in STRUCTURAL_MARKERS:
            assert marker in src, f"rule_doc 已不再产出标记 {marker!r}"
