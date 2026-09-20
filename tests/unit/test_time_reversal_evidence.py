"""R1a / R1b 时间倒序判据的「证据分级」护栏（B1-9）。

背景（Round 44，视觉直读 p48 原页确认）：
`_check_time_reversal_cross_page` 把**区间重叠**当成**倒序**，并按 `time_reversal`
给了 critical —— 而原图上那是**不同设备的两道并行清洗**（D2101/D2102 真空干燥箱
03:12→03:36 与 烘盘/盘罩/勺子 03:34→04:12），重叠属正常；且两处时刻都是**手写**
填写，与项目自身约定「手写体字段应标 confidence=low，规则不直接判定」相悖。

本文件固化 B1-9 的四条判据：
  ① 真倒序（curr.start < prev.start）⇒ critical（不放宽检测强度）；
  ② 仅重叠（prev.start <= curr.start < prev.end）⇒ warning（不是"倒序"）；
  ③ 所依据的时刻为**手写**填写 ⇒ 封顶 warning；
  ④ 时刻字面量**缺日期成分**（比较所用日期系由该页生产日期推定）⇒ 封顶 warning。

样本取自 Round 42 真实 51 页隔离库，`provenance` 注释给出出处。
"""
from __future__ import annotations

import pytest

from core.cross_page_analyzer import _normalize_pages
from core.rules.rule_time import (
    _check_time_reversal_cross_page,
    _check_time_reversal_in_page,
    _evidence_notes,
    _literal_has_date,
    _time_is_handwritten,
)


def _step(step_no, start=None, end=None, handwritten=None):
    s: dict = {"step_no": step_no}
    if start:
        s["start_time"] = start
    if end:
        s["end_time"] = end
    if handwritten is not None:
        s["handwritten"] = handwritten
    return s


def _pages(pairs):
    """pairs: [(page_no, [step, ...]), ...] 或 [(page_no, steps, page_info), ...]

    返回规则层所需的归一化页面结构。``page_info.production_date`` 是
    `_parse_time` 对"只有时刻"的输入做补全的唯一来源 —— 不传则纯时刻
    字面量根本解析不出来（`_parse_time` 的既定契约）。
    """
    structures = []
    for item in pairs:
        pno, steps = item[0], item[1]
        info = item[2] if len(item) > 2 else {}
        structures.append({
            "page": pno,
            "data": {"steps": steps, "findings": [], "page_info": info},
        })
    return _normalize_pages(structures)


def _tr(findings):
    return [f for f in findings if f["type"] == "time_reversal"]


# ── ① 助手：日期成分判定 ────────────────────────────────────────────
class TestLiteralHasDate:
    def test_iso_datetime_has_date(self):
        assert _literal_has_date("2025-09-25 03:36") is True

    def test_chinese_datetime_has_date(self):
        assert _literal_has_date("2025年01月21日 08时09分") is True

    def test_month_day_with_time_has_date(self):
        assert _literal_has_date("07-17 14:30") is True

    @pytest.mark.parametrize("lit", ["08时 09分", "03时36分", "14:12", "11:04"])
    def test_time_only_has_no_date(self, lit):
        """只有时刻 ⇒ 比较所用日期只能靠该页生产日期补全，不是记录里的事实。"""
        assert _literal_has_date(lit) is False

    @pytest.mark.parametrize("bad", [None, "", 123, [], {}])
    def test_non_string_is_false(self, bad):
        assert _literal_has_date(bad) is False


# ── ② 助手：手写信号判定 ────────────────────────────────────────────
class TestTimeIsHandwritten:
    # provenance: Round 42 real 轮 p48 工序3/工序4 的 handwritten 字段原值
    HW3 = ["E008232-2406019", "5", "25日03时12分", "03时36分", "✓是1☐否", "任智敏"]
    HW4 = ["E008232-2406019", "5", "25日03时34分", "04时12分", "✓是1☐否", "任智敏"]

    def test_provenance_hw3_matches_its_own_times(self):
        assert _time_is_handwritten(self.HW3, "2025-09-25 03:12") is True
        assert _time_is_handwritten(self.HW3, "2025-09-25 03:36") is True

    def test_provenance_hw4_matches_its_own_times(self):
        assert _time_is_handwritten(self.HW4, "2025-09-25 03:34") is True
        assert _time_is_handwritten(self.HW4, "2025-09-25 04:12") is True

    def test_回归_日期与时刻粘连时仍能命中(self):
        """⚠️ 回归护栏（本实现真实踩过的 bug）。

        曾先 ``lit.replace(" ", "")`` 再匹配 ``(?<!\\d)(\\d{1,2})[:时](\\d{2})``：
        `2025-09-25 03:36` 去空白后变成 `2025-09-2503:36`，日期末位数字与时刻
        粘连，反向断言 ``(?<!\\d)`` 直接挡住匹配 ⇒ **整条手写封顶静默失效**。
        必须在原字面量上匹配。
        """
        assert _time_is_handwritten(self.HW3, "2025-09-25 03:36") is True

    def test_negative_no_handwritten_annotation(self):
        assert _time_is_handwritten([], "2025-09-25 03:36") is False
        assert _time_is_handwritten(None, "2025-09-25 03:36") is False

    def test_negative_different_time_not_matched(self):
        """同页别的手写值不构成该时刻的手写证据。"""
        assert _time_is_handwritten(self.HW3, "2025-09-25 07:45") is False

    def test_negative_non_time_tokens_only(self):
        hw = ["任智敏", "E008232-2406019"]
        assert _time_is_handwritten(hw, "2025-09-25 03:36") is False


# ── ③ 证据说明聚合 ──────────────────────────────────────────────────
class TestEvidenceNotes:
    def test_clean_evidence_no_notes(self):
        assert _evidence_notes([], "2024-01-01 16:00", [], "2024-01-01 14:30") == []

    def test_handwritten_noted(self):
        hw = ["16时00分"]           # 与 lit_prev 的 16:00 命中
        notes = _evidence_notes(hw, "2024-01-01 16:00", [], "2024-01-01 14:30")
        assert any("手写" in n for n in notes)
        assert len(notes) == 1      # 两个字面量都带日期 ⇒ 无"推定"说明

    def test_date_inferred_noted(self):
        notes = _evidence_notes([], "16:00", [], "14:30")
        assert any("推定" in n for n in notes)

    def test_both_noted(self):
        notes = _evidence_notes(["16时00分"], "16:00", [], "14:30")
        assert len(notes) == 2
        assert any("手写" in n for n in notes)
        assert any("推定" in n for n in notes)


# ── ④ 跨页判据分级（R1b）────────────────────────────────────────────
class TestCrossPageGrading:
    def test_true_inversion_is_critical(self):
        """真倒序：工序3 开始(14:30) 早于 工序2 开始(15:00) ⇒ 编号与时刻矛盾。"""
        pages = _pages([
            (9, [_step(2, "2024-01-01 15:00", "2024-01-01 16:00")]),
            (10, [_step(3, "2024-01-01 14:30", "2024-01-01 15:30")]),
        ])
        tr = _tr(_check_time_reversal_cross_page(pages))
        assert len(tr) == 1
        assert tr[0]["severity"] == "critical"
        assert "倒序" in tr[0]["description"]

    def test_mere_overlap_is_warning_not_critical(self):
        """仅重叠：工序4 开始(03:34) 晚于 工序3 开始(03:12)，只压到 工序3 的结束。

        并行工序天然重叠 ⇒ 不得当成"倒序"给 critical。
        """
        pages = _pages([
            (48, [
                _step(3, "2025-09-25 03:12", "2025-09-25 03:36"),
                _step(4, "2025-09-25 03:34", "2025-09-25 04:12"),
            ]),
        ])
        tr = _tr(_check_time_reversal_cross_page(pages))
        assert len(tr) == 1
        assert tr[0]["severity"] == "warning"
        assert "重叠" in tr[0]["description"]

    def test_true_inversion_with_handwritten_is_capped(self):
        pages = _pages([
            (9, [_step(2, "2024-01-01 15:00", "2024-01-01 16:00", handwritten=["16时00分"])]),
            (10, [_step(3, "2024-01-01 14:30", "2024-01-01 15:30", handwritten=["14时30分"])]),
        ])
        tr = _tr(_check_time_reversal_cross_page(pages))
        assert len(tr) == 1
        assert tr[0]["severity"] == "warning"
        assert "手写" in tr[0]["description"]

    def test_true_inversion_without_date_is_capped(self):
        """时刻缺日期 ⇒ 比较所用日期由该页生产日期推定 ⇒ 不得 critical。"""
        pages = _pages([
            (9, [_step(2, "15:00", "16:00")], {"production_date": "2024-01-01"}),
            (10, [_step(3, "14:30", "15:30")], {"production_date": "2024-01-01"}),
        ])
        tr = _tr(_check_time_reversal_cross_page(pages))
        assert len(tr) == 1
        assert tr[0]["severity"] == "warning"
        assert "推定" in tr[0]["description"]

    def test_normal_order_no_finding(self):
        pages = _pages([
            (1, [_step(1, "2024-01-01 10:00", "2024-01-01 11:00")]),
            (2, [_step(2, "2024-01-01 11:30", "2024-01-01 12:00")]),
        ])
        assert _tr(_check_time_reversal_cross_page(pages)) == []

    def test_type_stays_canonical_time_reversal(self):
        """类型词表是单一真值：不得为本条新增类型（会牵动 CANONICAL_TYPES /
        TYPE_QUERIES / 前端 zh_map 多处同步）。"""
        pages = _pages([
            (48, [
                _step(3, "2025-09-25 03:12", "2025-09-25 03:36"),
                _step(4, "2025-09-25 03:34", "2025-09-25 04:12"),
            ]),
        ])
        for f in _tr(_check_time_reversal_cross_page(pages)):
            assert f["type"] == "time_reversal"


# ── ⑤ 页内判据分级（R1a）────────────────────────────────────────────
class TestInPageGrading:
    def test_same_step_start_after_end_is_critical(self):
        """同一工序"结束早于开始"逻辑上不可能 ⇒ 基准 critical 不变。"""
        pages = _pages([(1, [_step(1, "2024-01-01 11:00", "2024-01-01 10:00")])])
        tr = _tr(_check_time_reversal_in_page(pages))
        assert len(tr) == 1
        assert tr[0]["severity"] == "critical"

    def test_same_step_handwritten_is_capped(self):
        pages = _pages([
            (1, [_step(1, "2024-01-01 11:00", "2024-01-01 10:00",
                       handwritten=["11时00分", "10时00分"])]),
        ])
        tr = _tr(_check_time_reversal_in_page(pages))
        assert len(tr) == 1
        assert tr[0]["severity"] == "warning"
        assert "手写" in tr[0]["description"]

    def test_same_step_date_inferred_is_capped(self):
        pages = _pages([(1, [_step(1, "11:00", "10:00")], {"production_date": "2024-01-01"})])
        tr = _tr(_check_time_reversal_in_page(pages))
        assert len(tr) == 1
        assert tr[0]["severity"] == "warning"
        assert "推定" in tr[0]["description"]

    def test_normal_order_no_finding(self):
        pages = _pages([(1, [_step(1, "2024-01-01 10:00", "2024-01-01 11:00")])])
        assert _tr(_check_time_reversal_in_page(pages)) == []


# ── ⑥ 真实 p48 形态（端到端判据）────────────────────────────────────
class TestRealP48Shape:
    """Round 42 real 轮 p48 原样数据 → 必须为 warning 且注明手写。"""

    def test_p48_real_shape_downgraded(self):
        hw3 = ["E008232-2406019", "5", "25日03时12分", "03时36分", "✓是1☐否", "任智敏"]
        hw4 = ["E008232-2406019", "5", "25日03时34分", "04时12分", "✓是1☐否", "任智敏"]
        pages = _pages([
            (48, [
                _step(3, "2025-09-25 03:12", "2025-09-25 03:36", handwritten=hw3),
                _step(4, "2025-09-25 03:34", "2025-09-25 04:12", handwritten=hw4),
            ]),
        ])
        tr = _tr(_check_time_reversal_cross_page(pages))
        assert len(tr) == 1
        f = tr[0]
        assert f["severity"] == "warning"
        assert "重叠" in f["description"]
        assert "手写" in f["description"]
        assert f["page"] == 48
