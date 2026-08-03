"""Cross-page analyzer 单元测试。

覆盖：
- analyze_cross_page 主入口
- SpecBounds / _parse_spec 规格解析
- _expand_power_notation 幂记法（误报修复）
- _try_unit_normalize 单位换算（误报修复）
- _check_completeness 完整性检查
"""
import pytest
import asyncio
from unittest.mock import patch, AsyncMock

from core.cross_page_analyzer import (
    analyze_cross_page,
    SpecBounds,
    _parse_spec,
    _expand_power_notation,
    _parse_number,
    _extract_unit,
    _try_unit_normalize,
    _judge,
)


def _make_page(page_no, steps, findings=None, page_info=None, event_year_groups=None):
    """构造 analyze_cross_page 所需的页面结构（与 pipeline 一致）。"""
    return {
        "page": page_no,
        "data": {
            "steps": steps or [],
            "findings": findings or [],
            "page_info": page_info or {},
            "event_year_groups": event_year_groups or {},
        },
    }


def _make_step(step_no, start_time=None, end_time=None, operator="", reviewer="",
               measurements=None, parameters=None):
    """构造 step 字典。"""
    s = {"step_no": step_no}
    if start_time:
        s["start_time"] = start_time
    if end_time:
        s["end_time"] = end_time
    if operator:
        s["operator"] = operator
    if reviewer:
        s["reviewer"] = reviewer
    if measurements:
        s["measurements"] = measurements
    if parameters:
        s["parameters"] = parameters
    return s


def _make_cell(actual, spec, unit=""):
    """构造矩阵单元格 values dict。"""
    return {"actual": str(actual), "spec": spec, "unit": unit}


@pytest.fixture
def sample_pages():
    """返回模拟的页面数据。"""
    return [
        _make_page(1, [
            _make_step(1, "2024-01-01 10:00", "2024-01-01 11:00", "张三", "李四",
                       measurements=[{"time": "10:00", "values": {"温度": _make_cell("25.5", "20-30", "℃")}}]),
        ]),
        _make_page(2, [
            _make_step(2, "2024-01-01 11:00", "2024-01-01 12:00", "张三", "李四",
                       measurements=[{"time": "11:00", "values": {"温度": _make_cell("35.0", "20-30", "℃")}}]),
        ]),
    ]


class TestSpecBoundsAndParsing:
    """规格解析 — 核心误报修复验证。"""

    def test_parse_range_spec(self):
        bounds = _parse_spec("20-30")
        assert bounds is not None
        assert bounds.low == 20.0
        assert bounds.high == 30.0

    def test_parse_less_than_spec(self):
        bounds = _parse_spec("<0.3")
        assert bounds is not None
        assert bounds.high == 0.3

    def test_parse_less_equal_spec(self):
        bounds = _parse_spec("≤0.3")
        assert bounds is not None

    def test_parse_power_notation_spec(self):
        expanded = _expand_power_notation("≤10^3")
        assert "1000" in expanded
        bounds = _parse_spec(expanded)
        assert bounds is not None
        assert bounds.high == 1000.0

    def test_parse_chinese_spec(self):
        bounds = _parse_spec("不超过100次")
        assert bounds is not None
        assert bounds.high == 100.0

    def test_parse_none_spec(self):
        assert _parse_spec(None) is None

    def test_parse_empty_spec(self):
        assert _parse_spec("") is None

    def test_parse_invalid_spec(self):
        assert _parse_spec("abc") is None


class TestJudge:
    """_judge 参数判定。"""

    def test_in_range_returns_true(self):
        bounds = SpecBounds(op="between", low=20.0, high=30.0)
        assert _judge(bounds, 25.0) is True

    def test_out_of_range_returns_false(self):
        bounds = SpecBounds(op="between", low=20.0, high=30.0)
        assert _judge(bounds, 35.0) is False

    def test_at_boundary_returns_true(self):
        bounds = SpecBounds(op="between", low=20.0, high=30.0)
        assert _judge(bounds, 20.0) is True
        assert _judge(bounds, 30.0) is True


class TestPowerNotationExpansion:
    """幂记法展开（误报修复核心）。"""

    def test_expand_10_cubed(self):
        assert _expand_power_notation("10^3") == "1000"

    def test_expand_10_to_4(self):
        assert _expand_power_notation("10^4") == "10000"

    def test_expand_with_prefix(self):
        result = _expand_power_notation("≤10^3")
        assert "1000" in result

    def test_no_power_notation_unchanged(self):
        assert _expand_power_notation("20-30") == "20-30"


class TestUnitNormalization:
    """单位换算（误报修复核心 — ppm ↔ %）。

    _try_unit_normalize 返回 (converted_value, note) 元组：
    - 无需/无法换算时返回 (None, "")
    - 成功换算时返回 (converted_actual, "（已归一化 ...）")
    """

    def test_extract_unit_ppm(self):
        assert _extract_unit("99ppm") == "ppm"

    def test_extract_unit_percent(self):
        assert _extract_unit("50%") == "%"

    def test_extract_unit_none(self):
        assert _extract_unit("") == ""
        assert _extract_unit(None) == ""

    def test_ppm_to_percent_conversion(self):
        """99ppm 应换算为 0.0099%。"""
        converted, note = _try_unit_normalize(99.0, "ppm", "≤50%")
        assert converted is not None
        assert abs(converted - 0.0099) < 0.0001
        assert "归一化" in note

    def test_same_unit_no_conversion(self):
        """相同单位无需换算，返回 (None, "")。"""
        converted, note = _try_unit_normalize(25.0, "%", "20-30%")
        assert converted is None
        assert note == ""


class TestAnalyzeCrossPage:
    """analyze_cross_page 主入口。"""

    def test_returns_list(self, sample_pages):
        findings = asyncio.run(analyze_cross_page(sample_pages))
        assert isinstance(findings, list)

    def test_empty_pages_returns_empty(self):
        findings = asyncio.run(analyze_cross_page([]))
        assert findings == []

    def test_findings_have_required_fields(self, sample_pages):
        findings = asyncio.run(analyze_cross_page(sample_pages))
        for f in findings:
            assert "type" in f
            assert "severity" in f
            assert "description" in f
            assert "page" in f
            assert "source" in f

    def test_out_of_spec_produces_finding(self):
        """参数越界应产生 finding。"""
        pages = [_make_page(1, [
            _make_step(1, measurements=[
                {"time": "10:00", "values": {"温度": _make_cell("40.0", "20-30", "℃")}},
            ]),
        ])]
        findings = asyncio.run(analyze_cross_page(pages))
        assert any("温度" in f.get("description", "") or "越界" in f.get("type", "")
                   or "参数" in f.get("type", "") for f in findings)

    def test_in_spec_no_finding(self):
        """在规格内不应产生参数越界 finding。"""
        pages = [_make_page(1, [
            _make_step(1, measurements=[
                {"time": "10:00", "values": {"温度": _make_cell("25.0", "20-30", "℃")}},
            ]),
        ])]
        findings = asyncio.run(analyze_cross_page(pages))
        param_findings = [f for f in findings if "越界" in f.get("type", "") or "参数" in f.get("type", "")]
        assert len(param_findings) == 0

    def test_power_notation_no_false_positive(self):
        """幂记法 50 vs ≤10^3 不应越界（误报修复验证）。"""
        pages = [_make_page(1, [
            _make_step(1, measurements=[
                {"time": "10:00", "values": {"需氧菌": _make_cell("50", "≤10^3")}},
            ]),
        ])]
        findings = asyncio.run(analyze_cross_page(pages))
        assert all("需氧菌" not in f.get("description", "") for f in findings)

    def test_missing_operator_produces_finding(self):
        """缺少操作人应产生完整性 finding。"""
        pages = [_make_page(1, [
            _make_step(1, start_time="2024-01-01 10:00", end_time="2024-01-01 11:00"),
        ])]
        findings = asyncio.run(analyze_cross_page(pages))
        assert any("签名" in f.get("description", "") or "操作人" in f.get("description", "")
                   or "复核" in f.get("description", "") for f in findings)

    def test_complete_step_no_completeness_finding(self, sample_pages):
        """完整 step 不应产生完整性 finding。"""
        findings = asyncio.run(analyze_cross_page(sample_pages))
        completeness = [f for f in findings if "签名" in f.get("description", "")
                         or "操作人" in f.get("description", "")]
        assert len(completeness) == 0

    def test_time_reversal_cross_page_produces_finding(self):
        """跨页时间倒序应产生 critical finding。"""
        pages = [
            _make_page(9, [_make_step(2, start_time="2024-01-01 15:00", end_time="2024-01-01 16:00")]),
            _make_page(10, [_make_step(3, start_time="2024-01-01 14:30", end_time="2024-01-01 15:30")]),
        ]
        findings = asyncio.run(analyze_cross_page(pages))
        assert any("时间" in f.get("type", "") or "工序" in f.get("description", "") for f in findings)
