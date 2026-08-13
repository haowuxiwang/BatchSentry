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
from unittest.mock import patch, AsyncMock, MagicMock

from core.cross_page_analyzer import (
    analyze_cross_page,
    SpecBounds,
    _parse_spec,
    _expand_power_notation,
    _parse_number,
    _extract_unit,
    _try_unit_normalize,
    _judge,
    _parse_time,
    _extract_year,
    _normalize_pages,
    _collect_per_page_findings,
    _check_time_reversal_in_page,
    _check_time_reversal_cross_page,
    _step_sort_key,
    _check_param_out_of_spec,
    _check_suspicious_dates,
    _check_completeness,
    _check_batch_consistency,
    _check_low_confidence_params,
    _build_summary,
    _user_rules_section,
    _llm_based_check,
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
               measurements=None, parameters=None, signatures=None):
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
    if signatures:
        s["signatures"] = signatures
    return s


def _make_cell(actual, spec, unit=""):
    """构造矩阵单元格 values dict。"""
    return {"actual": str(actual), "spec": spec, "unit": unit}


def _norm(pages):
    """将 raw page 结构归一化为 rule 函数所需的格式。"""
    return _normalize_pages(pages)


@pytest.fixture
def sample_pages():
    """返回模拟的页面数据。"""
    return [
        _make_page(1, [
            _make_step(1, "2024-01-01 10:00", "2024-01-01 11:00", "张三", "李四",
                       measurements=[{"time": "10:00", "values": {"温度": _make_cell("25.5", "20-30", "℃")}}],
                       signatures=[{"role": "qa", "name": "王五"}]),
        ]),
        _make_page(2, [
            _make_step(2, "2024-01-01 11:00", "2024-01-01 12:00", "张三", "李四",
                       measurements=[{"time": "11:00", "values": {"温度": _make_cell("35.0", "20-30", "℃")}}],
                       signatures=[{"role": "qa", "name": "王五"}]),
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
    - 无需换算（同单位/无单位）时返回 (actual_num, "")
    - 成功换算时返回 (converted_actual, "unit normalized: ...")
    - 单位不一致但无换算规则时返回 (None, "unit_mismatch")（fail-closed）
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
        assert "normalized" in note

    def test_same_unit_no_conversion(self):
        """相同单位无需换算，返回 (actual_num, "")。"""
        converted, note = _try_unit_normalize(25.0, "%", "20-30%")
        assert converted == 25.0
        assert note == ""


class TestAnalyzeCrossPage:
    """analyze_cross_page 主入口。

    所有测试 mock get_llm_client，避免调用真实 DeepSeek API（120s 超时）。
    mock 返回空 findings，让规则层逻辑独立验证。
    """

    @pytest.fixture(autouse=True)
    def mock_llm_client(self):
        """自动 mock get_llm_client，返回空结果，避免真实 API 调用。"""
        mock_client = MagicMock()
        mock_client.chat_json = AsyncMock(return_value=[])
        with patch('core.cross_page_analyzer.get_llm_client',
                   return_value=mock_client):
            yield mock_client

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


# ===========================================================================
# _parse_time 测试 — 各种格式解析与异常分支
# ===========================================================================


class TestParseTime:
    """_parse_time 各种格式解析与异常分支。"""

    def test_empty_string_returns_none(self):
        """空字符串与纯空白应返回 None（line 84）。"""
        assert _parse_time("") is None
        assert _parse_time("   ") is None

    def test_none_returns_none(self):
        """None 输入应返回 None。"""
        assert _parse_time(None) is None

    def test_non_string_returns_none(self):
        """非字符串输入应返回 None。"""
        assert _parse_time(123) is None
        assert _parse_time([]) is None

    def test_time_only_no_fallback_returns_none(self):
        """仅有 HH:MM 且无 fallback_date 时应返回 None（line 94）。"""
        assert _parse_time("14:30") is None

    def test_time_only_unparseable_fallback_returns_none(self):
        """仅有 HH:MM 且 fallback_date 无法解析时应返回 None（line 92）。"""
        assert _parse_time("14:30", fallback_date="invalid") is None

    def test_time_only_with_valid_fallback(self):
        """仅有 HH:MM 且有有效 fallback_date 时应返回完整 datetime（line 91）。"""
        dt = _parse_time("14:30", fallback_date="2024.01.01")
        assert dt is not None
        assert dt.year == 2024
        assert dt.hour == 14
        assert dt.minute == 30

    def test_ocr_noise_pattern_valid(self):
        """OCR 串扰格式 '2022/4/202205.07' 应解析为 2022.05.07（lines 99-103）。"""
        dt = _parse_time("2022/4/202205.07")
        assert dt is not None
        assert dt.year == 2022
        assert dt.month == 5
        assert dt.day == 7

    def test_ocr_noise_pattern_invalid_date_falls_through(self):
        """OCR 串扰格式日期非法时应 fallthrough 不崩溃（lines 104-105）。"""
        # 月份=13、日=40 会触发 ValueError，然后 fallthrough 到 _DATE_RE
        result = _parse_time("2022/4/20221340")
        # 不应崩溃；_DATE_RE 会匹配 "2022/4/20"
        assert result is not None
        assert result.year == 2022
        assert result.month == 4
        assert result.day == 20

    def test_chinese_date_valid(self):
        """中文日期格式 '2024年5月7日' 应正确解析（lines 110-114）。"""
        dt = _parse_time("2024年5月7日")
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 5
        assert dt.day == 7

    def test_chinese_date_with_time(self):
        """中文日期含时间应解析出时分。"""
        dt = _parse_time("2024年05月07日 14时30分")
        assert dt is not None
        assert dt.hour == 14
        assert dt.minute == 30

    def test_chinese_date_invalid_returns_none(self):
        """中文日期非法时应返回 None（lines 115-116）。"""
        assert _parse_time("2024年13月32日") is None

    def test_normal_date_invalid_returns_none(self):
        """普通日期格式非法时应返回 None（lines 126-127）。"""
        assert _parse_time("2024.13.32") is None

    def test_bare_year(self):
        """纯年份字符串应解析为 1月1日（lines 132-133）。"""
        dt = _parse_time("2022")
        assert dt is not None
        assert dt.year == 2022
        assert dt.month == 1
        assert dt.day == 1

    def test_no_match_returns_none(self):
        """无法匹配任何格式时应返回 None。"""
        assert _parse_time("no date here") is None


# ===========================================================================
# _extract_year 测试
# ===========================================================================


class TestExtractYear:
    """_extract_year 年份提取。"""

    def test_int_input_uses_year_re_fallback(self):
        """非字符串输入时 _parse_time 返回 None，但 _YEAR_RE 仍可匹配（lines 145-148）。"""
        assert _extract_year(2024) == 2024

    def test_none_returns_none(self):
        assert _extract_year(None) is None

    def test_no_year_returns_none(self):
        assert _extract_year("no year") is None

    def test_valid_date_returns_year(self):
        assert _extract_year("2024.05.01") == 2024


# ===========================================================================
# _parse_number 边界条件
# ===========================================================================


class TestParseNumberEdge:
    """_parse_number 边界条件。"""

    def test_none_returns_none(self):
        """None 输入应返回 None（line 158）。"""
        assert _parse_number(None) is None

    def test_empty_string_returns_none(self):
        """空字符串应返回 None（line 158）。"""
        assert _parse_number("") is None

    def test_int_input(self):
        """int 输入应转为 float（line 160）。"""
        assert _parse_number(42) == 42.0

    def test_float_input(self):
        """float 输入应原样返回（line 160）。"""
        assert _parse_number(3.14) == 3.14

    def test_no_number_found_returns_none(self):
        """无数字的字符串应返回 None（line 170）。"""
        assert _parse_number("abc") is None

    def test_percentage(self):
        assert _parse_number("53.6%") == 53.6

    def test_with_units(self):
        assert _parse_number("17次") == 17.0


# ===========================================================================
# _expand_power_notation 非10基数
# ===========================================================================


class TestExpandPowerNotationEdge:
    """_expand_power_notation 非10基数不展开。"""

    def test_non_ten_base_unchanged(self):
        """非10基数的幂记法应保持原样（line 193）。"""
        result = _expand_power_notation("2^5")
        assert result == "2^5"


# ===========================================================================
# _parse_spec 边界条件
# ===========================================================================


class TestParseSpecEdge:
    """_parse_spec 边界条件。"""

    def test_whitespace_only_returns_none(self):
        """纯空白 strip 后为空应返回 None（line 217）。"""
        assert _parse_spec("   ") is None

    def test_greater_than_spec(self):
        """'>5' 应解析为 gt 操作（lines 249-251）。"""
        bounds = _parse_spec(">5")
        assert bounds is not None
        assert bounds.op == "gt"
        assert bounds.low == 5.0

    def test_greater_equal_spec(self):
        """'>=30' 应解析为 ge 操作（lines 249-251）。"""
        bounds = _parse_spec(">=30")
        assert bounds is not None
        assert bounds.op == "ge"
        assert bounds.low == 30.0

    def test_chinese_no_less_than(self):
        """'不少于30' 应解析为 ge 操作（line 259）。"""
        bounds = _parse_spec("不少于30")
        assert bounds is not None
        assert bounds.op == "ge"
        assert bounds.low == 30.0


# ===========================================================================
# _judge 所有操作符分支
# ===========================================================================


class TestJudgeEdge:
    """_judge 所有操作符分支。"""

    def test_lt_operator(self):
        """lt 操作符：actual < high（line 268）。"""
        bounds = SpecBounds(op="lt", high=0.3)
        assert _judge(bounds, 0.2) is True
        assert _judge(bounds, 0.3) is False

    def test_gt_operator(self):
        """gt 操作符：actual > low（lines 271-272）。"""
        bounds = SpecBounds(op="gt", low=5.0)
        assert _judge(bounds, 10.0) is True
        assert _judge(bounds, 5.0) is False

    def test_ge_operator(self):
        """ge 操作符：actual >= low（lines 273-274）。"""
        bounds = SpecBounds(op="ge", low=30.0)
        assert _judge(bounds, 30.0) is True
        assert _judge(bounds, 29.0) is False

    def test_le_operator(self):
        """le 操作符：actual <= high。"""
        bounds = SpecBounds(op="le", high=100.0)
        assert _judge(bounds, 100.0) is True
        assert _judge(bounds, 101.0) is False

    def test_unknown_op_returns_false_fail_closed(self):
        """未知操作符应 fail-closed 返回 False（line 275-277，GMP 安全原则）。"""
        bounds = SpecBounds(op="unknown")
        assert _judge(bounds, 0.0) is False


# ===========================================================================
# _try_unit_normalize 无换算
# ===========================================================================


class TestTryUnitNormalizeEdge:
    """_try_unit_normalize 无换算场景。"""

    def test_no_conversion_possible_returns_mismatch(self):
        """无法换算的单位应返回 (None, 'unit_mismatch') 触发 fail-closed。"""
        converted, note = _try_unit_normalize(50.0, "bar", "50psi")
        assert converted is None
        assert note == "unit_mismatch"

    def test_empty_actual_unit_returns_as_is(self):
        """actual 无单位时应返回 (actual_num, '') 按原值比较。"""
        converted, note = _try_unit_normalize(50.0, "", "20-30")
        assert converted == 50.0
        assert note == ""


# ===========================================================================
# _normalize_pages 异常输入
# ===========================================================================


class TestNormalizePages:
    """_normalize_pages 异常输入。"""

    def test_parse_error_page_skipped(self):
        """带 _parse_error 的页面应被跳过（line 397）。"""
        pages = [
            {"page": 1, "data": {"_parse_error": True}},
            {"page": 2, "data": {"steps": [], "page_info": {}}},
        ]
        result = _normalize_pages(pages)
        assert len(result) == 1
        assert result[0]["page"] == 2

    def test_no_data_skipped(self):
        """data 为 None 或空 dict 的页面应被跳过（line 397）。"""
        pages = [
            {"page": 1, "data": None},
            {"page": 2, "data": {}},
        ]
        result = _normalize_pages(pages)
        assert len(result) == 0

    def test_empty_list_returns_empty(self):
        assert _normalize_pages([]) == []

    def test_type_polluted_rows_filtered(self):
        """P1-2 兜底: 老 page_cache 脏数据（steps/findings 含非 dict、
        event_year_groups 含非数字）→ 归一化时过滤，规则层不接触非法元素。"""
        pages = [{
            "page": 1,
            "data": {
                "page_info": {"title": "x"},
                "steps": ["garbage", {"step_no": 1, "parameters": ["温度"]}],
                "findings": ["bad"],
                "event_year_groups": {"draft": ["2022年", 2021], "production": "oops"},
            },
        }]
        result = _normalize_pages(pages)
        assert len(result) == 1
        assert result[0]["steps"] == [{"step_no": 1, "parameters": []}]
        assert result[0]["findings"] == []
        # 非数字年份剔除；非 list 的 event 分组置空（键保留）
        assert result[0]["event_year_groups"] == {"draft": [2021], "production": []}

    def test_analyze_cross_page_survives_type_pollution(self):
        """P1-2 端到端: analyze_cross_page 面对类型污染输入不抛异常、
        不整单失败 — 规则层产生零 findings 而非崩溃。"""
        from unittest.mock import MagicMock

        polluted = [
            {"page": 1, "data": {
                "page_info": {"title": "污染页"},
                "steps": [
                    "garbage-step",
                    {"step_no": 1, "start_time": "2024-01-01 09:00",
                     "end_time": "2024-01-01 08:00",  # 时间反转本应触发 R1-a
                     "parameters": ["温度"], "signatures": ["签名"]},
                ],
                "event_year_groups": {"draft": ["2022年"], "review": 2021},
                "findings": ["bad"],
            }},
        ]
        mock_client = MagicMock()
        mock_client.chat_json = AsyncMock(return_value=[])
        with patch('core.cross_page_analyzer.get_llm_client', return_value=mock_client):
            findings = asyncio.run(analyze_cross_page(polluted, job_id="test"))
        assert isinstance(findings, list)
        # 消毒后 steps 只剩合法 dict → R1-a 正常判定/或跳过；不崩溃是核心断言
        assert all(isinstance(f, dict) for f in findings)

    def test_analyze_cross_page_survives_scalar_pollution(self):
        """P1-2 端到端(标量): page_info 非 dict / overall_confidence 非 str /
        operation 非 str / signature role 非 str — 规则层直接触碰这些字段
        （时间反转、批量一致性、完整性、低置信度检查），消毒后不抛异常。"""
        from unittest.mock import MagicMock

        polluted = [
            {"page": 1, "data": {
                "page_info": "摘要页",
                "overall_confidence": 5,
                "steps": [
                    {
                        "step_no": 1,
                        "operation": 123,
                        "start_time": "2024-01-01 09:00",
                        "end_time": "2024-01-01 10:00",
                        "signatures": [{"role": 7, "name": None}],
                        "parameters": [],
                        "measurements": [],
                    },
                ],
            }},
            {"page": 2, "data": {
                "page_info": "续页",
                "steps": [
                    {"step_no": 1, "operation": "复核", "parameters": [],
                     "measurements": [], "signatures": []},
                ],
            }},
        ]
        mock_client = MagicMock()
        mock_client.chat_json = AsyncMock(return_value=[])
        with patch('core.cross_page_analyzer.get_llm_client', return_value=mock_client):
            findings = asyncio.run(analyze_cross_page(polluted, job_id="test"))
        assert isinstance(findings, list)
        assert all(isinstance(f, dict) for f in findings)
        # 消毒路径: page_info 被置空 → 批量一致性检查跳过而非崩溃
        assert not any(f.get("source") == "rule" and f.get("description", "").startswith("批量") for f in findings)


# ===========================================================================
# _collect_per_page_findings 过滤
# ===========================================================================


class TestCollectPerPageFindings:
    """_collect_per_page_findings 过滤。"""

    def test_non_dict_finding_skipped(self):
        """非 dict 类型的 finding 应被跳过（line 426）。"""
        pages = _norm([
            _make_page(1, [], findings=["not a dict", 42, None]),
        ])
        result = _collect_per_page_findings(pages)
        assert result == []

    def test_missing_required_fields_skipped(self):
        """缺少必需字段的 finding 应被跳过（line 429）。"""
        pages = _norm([
            _make_page(1, [], findings=[
                {"type": "x"},
                {"severity": "warning"},
                {"type": "x", "severity": "warning", "description": "ok"},
            ]),
        ])
        result = _collect_per_page_findings(pages)
        assert len(result) == 1
        assert result[0]["description"] == "ok"
        assert result[0]["source"] == "llm_page"
        assert result[0]["page"] == 1

    def test_page_number_corrected(self):
        """LLM 设置的错误页号应被纠正为实际页号。"""
        pages = _norm([
            _make_page(5, [], findings=[
                {"type": "x", "severity": "warning", "description": "ok", "page": 999},
            ]),
        ])
        result = _collect_per_page_findings(pages)
        assert result[0]["page"] == 5

    def test_default_fields_added(self):
        """缺少 ocr_text/operator 时应填充默认值。"""
        pages = _norm([
            _make_page(1, [], findings=[
                {"type": "x", "severity": "warning", "description": "ok"},
            ]),
        ])
        result = _collect_per_page_findings(pages)
        assert result[0]["ocr_text"] == ""
        assert result[0]["operator"] == ""


# ===========================================================================
# R1-a: 页内时间倒序
# ===========================================================================


class TestTimeReversalInPage:
    """R1-a: 页内工序时间倒序。"""

    def test_start_after_end_produces_finding(self):
        """开始时间晚于结束时间应产生 critical finding（line 453）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024-01-01 11:00", end_time="2024-01-01 10:00"),
            ]),
        ])
        findings = _check_time_reversal_in_page(pages)
        assert len(findings) == 1
        f = findings[0]
        assert f["type"] == "time_reversal"
        assert f["severity"] == "critical"
        assert f["page"] == 1
        assert f["source"] == "rule"

    def test_normal_order_no_finding(self):
        """正常顺序不应产生 finding。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024-01-01 10:00", end_time="2024-01-01 11:00"),
            ]),
        ])
        findings = _check_time_reversal_in_page(pages)
        assert findings == []

    def test_missing_times_no_finding(self):
        """缺少时间字段的 step 不应产生 finding。"""
        pages = _norm([
            _make_page(1, [_make_step(1)]),
        ])
        findings = _check_time_reversal_in_page(pages)
        assert findings == []


# ===========================================================================
# R1-b: 跨页时间倒序
# ===========================================================================


class TestTimeReversalCrossPage:
    """R1-b: 跨页工序时间倒序。"""

    def test_skip_appendix_relisting(self):
        """不同页且 step_no 减小时应跳过（附表重列，line 516）。"""
        pages = _norm([
            _make_page(1, [_make_step(2, start_time="2024-01-01 10:00", end_time="2024-01-01 11:00")]),
            _make_page(2, [_make_step(1, start_time="2024-01-01 10:30", end_time="2024-01-01 11:30")]),
        ])
        findings = _check_time_reversal_cross_page(pages)
        assert findings == []

    def test_year_delta_gt_2_produces_warning(self):
        """年份相差 >2 年应产生 warning 级 finding（line 528）。"""
        pages = _norm([
            _make_page(1, [_make_step(1, start_time="2024-01-01 10:00", end_time="2024-01-01 11:00")]),
            _make_page(2, [_make_step(2, start_time="2020-01-01 10:30", end_time="2020-01-01 11:30")]),
        ])
        findings = _check_time_reversal_cross_page(pages)
        assert len(findings) == 1
        f = findings[0]
        assert f["type"] == "time_reversal"
        assert f["severity"] == "warning"
        assert "年份相差" in f["description"]

    def test_normal_cross_page_no_finding(self):
        """正常跨页顺序不应产生 finding。"""
        pages = _norm([
            _make_page(1, [_make_step(1, start_time="2024-01-01 10:00", end_time="2024-01-01 11:00")]),
            _make_page(2, [_make_step(2, start_time="2024-01-01 11:30", end_time="2024-01-01 12:00")]),
        ])
        findings = _check_time_reversal_cross_page(pages)
        assert findings == []

    def test_duplicate_step_same_page_skipped(self):
        """同页同 step_no 的重复工序不应产生时间倒序 finding（line 521）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(6, start_time="2024-01-01 10:00", end_time="2024-01-01 11:00"),
                _make_step(6, start_time="2024-01-01 10:30", end_time="2024-01-01 10:45"),
            ]),
        ])
        findings = _check_time_reversal_cross_page(pages)
        # 同页同 step_no 的两条记录应跳过比较
        assert findings == []

    def test_sort_exception_handled(self):
        """排序异常时应保持原顺序不崩溃（lines 495-496）。"""
        raised = [False]

        def side_effect(step_no):
            if not raised[0]:
                raised[0] = True
                raise RuntimeError("sort error")
            return _step_sort_key(step_no)

        with patch('core.cross_page_analyzer._step_sort_key', side_effect=side_effect):
            pages = _norm([
                _make_page(1, [
                    _make_step(1, start_time="2024-01-01 10:00", end_time="2024-01-01 11:00"),
                    _make_step(2, start_time="2024-01-01 11:00", end_time="2024-01-01 12:00"),
                ]),
            ])
            findings = _check_time_reversal_cross_page(pages)
            assert isinstance(findings, list)

    def test_unparseable_time_flagged_for_review(self):
        """OCR 导致的时间格式无法解析时应产生 completeness finding 提示人工复核。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024-01-01 10:00", end_time="2024-01-01 11:00"),
                _make_step(2, start_time="无效时间", end_time="2024-01-01 12:00"),
            ]),
        ])
        findings = _check_time_reversal_cross_page(pages)
        # 工序2 的 start_time 无法解析 → 应产生 1 条 completeness info finding
        review = [f for f in findings if f["type"] == "completeness"]
        assert len(review) == 1
        f = review[0]
        assert f["page"] == 1
        assert f["severity"] == "info"
        assert f["source"] == "rule"
        assert "工序2" in f["description"]
        assert "无法解析" in f["description"]
        assert "人工核对" in f["description"]
        assert "无效时间" in f["ocr_text"]
        # 不应同时产生 time_reversal 误报
        assert not any(f["type"] == "time_reversal" for f in findings)

    def test_unparseable_end_time_flagged(self):
        """end_time 无法解析时也应被标记。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024-01-01 10:00", end_time="垃圾日期"),
            ]),
        ])
        findings = _check_time_reversal_cross_page(pages)
        review = [f for f in findings if f["type"] == "completeness"]
        assert len(review) == 1
        assert "垃圾日期" in review[0]["ocr_text"]

    def test_no_time_fields_no_unparseable_finding(self):
        """没有 start_time/end_time 的步骤不应产生解析失败 finding。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1),  # 无时间字段
            ]),
        ])
        findings = _check_time_reversal_cross_page(pages)
        assert findings == []


# ===========================================================================
# _step_sort_key 排序键
# ===========================================================================


class TestStepSortKey:
    """_step_sort_key 排序键转换。"""

    def test_none_step_no(self):
        """None step_no 应返回 9999.0（line 564）。"""
        assert _step_sort_key(None) == 9999.0

    def test_numeric_step_no(self):
        """数字字符串应转为 float。"""
        assert _step_sort_key("1") == 1.0
        assert _step_sort_key(2) == 2.0

    def test_decimal_step_no(self):
        """小数 step_no 应正确解析。"""
        assert _step_sort_key("3.1") == 3.1

    def test_non_numeric_step_no(self):
        """非数字字符串应返回 9999.0（line 568）。"""
        assert _step_sort_key("abc") == 9999.0

    def test_appendix_step_no(self):
        """'附表1' 应提取数字部分。"""
        assert _step_sort_key("附表1") == 1.0


# ===========================================================================
# R3: 参数越界检查（含 _judge_param / _judge_cell）
# ===========================================================================


class TestCheckParamOutOfSpec:
    """R3: 参数越界检查 — 覆盖 _judge_param / _judge_cell 各分支。"""

    def test_param_actual_none_no_finding(self):
        """参数 value 为 None 时应跳过（line 629）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, parameters=[
                    {"name": "温度", "spec_range": "20-30", "value": None, "unit": "℃"},
                ]),
            ]),
        ])
        llm_queue = []
        findings = _check_param_out_of_spec(pages, llm_queue)
        assert findings == []
        assert llm_queue == []

    def test_param_empty_actual_no_finding(self):
        """参数 value 为空字符串时应跳过（line 629）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, parameters=[
                    {"name": "温度", "spec_range": "20-30", "value": "", "unit": "℃"},
                ]),
            ]),
        ])
        llm_queue = []
        findings = _check_param_out_of_spec(pages, llm_queue)
        assert findings == []
        assert llm_queue == []

    def test_param_non_numeric_actual_goes_to_llm_queue(self):
        """参数有数字 spec 但 value 非数字时应进入 llm_queue（lines 640-646）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, parameters=[
                    {"name": "温度", "spec_range": "20-30", "value": "异常", "unit": "℃"},
                ]),
            ]),
        ])
        llm_queue = []
        findings = _check_param_out_of_spec(pages, llm_queue)
        assert findings == []
        assert len(llm_queue) == 1
        assert llm_queue[0]["kind"] == "param"
        assert llm_queue[0]["name"] == "温度"

    def test_param_out_of_spec_produces_finding(self):
        """参数越界应产生 finding（lines 647-654）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, parameters=[
                    {"name": "温度", "spec_range": "20-30", "value": "40.0", "unit": "℃"},
                ]),
            ]),
        ])
        llm_queue = []
        findings = _check_param_out_of_spec(pages, llm_queue)
        assert len(findings) == 1
        assert findings[0]["type"] == "param_out_of_spec"
        assert findings[0]["page"] == 1

    def test_param_in_spec_no_finding(self):
        """参数在规格内不应产生 finding。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, parameters=[
                    {"name": "温度", "spec_range": "20-30", "value": "25.0", "unit": "℃"},
                ]),
            ]),
        ])
        llm_queue = []
        findings = _check_param_out_of_spec(pages, llm_queue)
        assert findings == []
        assert llm_queue == []

    def test_cell_actual_none_no_finding(self):
        """cell actual 为 None 时应跳过（line 672）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, measurements=[
                    {"time": "10:00", "values": {"温度": {"actual": None, "spec": "20-30", "unit": "℃"}}},
                ]),
            ]),
        ])
        llm_queue = []
        findings = _check_param_out_of_spec(pages, llm_queue)
        assert findings == []
        assert llm_queue == []

    def test_cell_empty_actual_no_finding(self):
        """cell actual 为空字符串时应跳过（line 672）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, measurements=[
                    {"time": "10:00", "values": {"温度": {"actual": "", "spec": "20-30", "unit": "℃"}}},
                ]),
            ]),
        ])
        llm_queue = []
        findings = _check_param_out_of_spec(pages, llm_queue)
        assert findings == []
        assert llm_queue == []

    def test_cell_unparseable_spec_goes_to_llm_queue(self):
        """cell spec 无法解析时应进入 llm_queue（lines 676-681）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, measurements=[
                    {"time": "10:00", "values": {"外观": {"actual": "浑浊", "spec": "应澄清", "unit": ""}}},
                ]),
            ]),
        ])
        llm_queue = []
        findings = _check_param_out_of_spec(pages, llm_queue)
        assert findings == []
        assert len(llm_queue) == 1
        assert llm_queue[0]["kind"] == "cell"
        assert llm_queue[0]["name"] == "外观"

    def test_cell_non_numeric_actual_goes_to_llm_queue(self):
        """cell 有数字 spec 但 actual 非数字时应进入 llm_queue（lines 684-689）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, measurements=[
                    {"time": "10:00", "values": {"温度": {"actual": "异常", "spec": "20-30", "unit": "℃"}}},
                ]),
            ]),
        ])
        llm_queue = []
        findings = _check_param_out_of_spec(pages, llm_queue)
        assert findings == []
        assert len(llm_queue) == 1
        assert llm_queue[0]["kind"] == "cell"

    def test_cell_out_of_spec_produces_finding(self):
        """cell 越界应产生 finding（lines 690-708）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, measurements=[
                    {"time": "10:00", "values": {"温度": {"actual": "40.0", "spec": "20-30", "unit": "℃"}}},
                ]),
            ]),
        ])
        llm_queue = []
        findings = _check_param_out_of_spec(pages, llm_queue)
        assert len(findings) == 1
        assert findings[0]["type"] == "param_out_of_spec"


# ===========================================================================
# R6: 完整性检查 — 重复 step 去重
# ===========================================================================


class TestCompletenessDedup:
    """R6: completeness 重复 step 去重。"""

    def test_duplicate_step_deduped(self):
        """同页同 step_no 重复出现时，completeness finding 应去重。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024-01-01 10:00", end_time="2024-01-01 11:00"),
                _make_step(1, start_time="2024-01-01 10:30", end_time="2024-01-01 10:45"),
            ]),
        ])
        findings = _check_completeness(pages)
        # 两条同 step_no=1 的记录应去重，operator 和 reviewer 各只报一次
        operator_findings = [f for f in findings if "操作人" in f.get("description", "")]
        reviewer_findings = [f for f in findings if "复核" in f.get("description", "")]
        assert len(operator_findings) == 1
        assert len(reviewer_findings) == 1


# ===========================================================================
# R4: suspicious_date 边界条件
# ===========================================================================


class TestSuspiciousDatesEdge:
    """R4: suspicious_date 边界条件。"""

    def test_unparseable_date_skipped(self):
        """无法提取年份的日期应被跳过（line 729）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="无效日期", end_time="也是无效的"),
            ]),
        ])
        findings = _check_suspicious_dates(pages)
        assert findings == []

    def test_empty_page_info_no_finding(self):
        """空 page_info 不应产生 finding。"""
        pages = _norm([_make_page(1, [])])
        findings = _check_suspicious_dates(pages)
        assert findings == []

    def test_signature_empty_sign_time_skipped(self):
        """签名为空 sign_time 时应被跳过不崩溃。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024-01-01 10:00", end_time="2024-01-01 11:00",
                           signatures=[
                               {"role": "operator", "name": "张三", "sign_time": ""},
                           ]),
            ]),
        ])
        findings = _check_suspicious_dates(pages)
        # 空 sign_time 不应产生 suspicious_date finding
        assert all("张三" not in f.get("description", "") for f in findings)


# ===========================================================================
# _build_summary 摘要构建
# ===========================================================================


class TestBuildSummary:
    """_build_summary 摘要构建 — 覆盖所有字段分支。"""

    def test_full_page_summary(self):
        """完整页面应包含所有摘要字段（lines 990, 992, 994, 997, 1021）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024-01-01 10:00", end_time="2024-01-01 11:00",
                           operator="张三", reviewer="李四",
                           parameters=[{"name": "温度", "value": "25", "unit": "℃"}],
                           measurements=[{"time": "10:00", "values": {"温度": {"actual": "25"}}}],
                           signatures=[{"role": "operator", "name": "张三", "sign_time": "2024-01-01 11:30"}]),
            ], findings=[
                {"type": "test", "description": "test finding"},
            ], page_info={
                "title": "Test Page",
                "production_date": "2024-01-01",
                "batch_no": "BATCH001",
            }, event_year_groups={"draft": [2024]}),
        ])
        summary = _build_summary(pages)
        assert "标题: Test Page" in summary
        assert "生产日期: 2024-01-01" in summary
        assert "批号: BATCH001" in summary
        assert "事件年份分组" in summary
        assert "签名:" in summary
        assert "张三" in summary

    def test_empty_pages_returns_empty(self):
        """空页面列表应返回空字符串。"""
        assert _build_summary([]) == ""

    def test_minimal_page(self):
        """最小页面应至少包含页码标题。"""
        pages = _norm([_make_page(1, [])])
        summary = _build_summary(pages)
        assert "第1页" in summary

    def test_parameter_without_value_skipped(self):
        """参数无 value 时应在摘要中跳过（不崩溃）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024-01-01 10:00", end_time="2024-01-01 11:00",
                           parameters=[
                               {"name": "有值参数", "value": "25", "unit": "℃"},
                               {"name": "无值参数", "value": "", "unit": ""},
                           ]),
            ]),
        ])
        summary = _build_summary(pages)
        assert "有值参数" in summary
        # 无值参数不应出现在摘要中


# ===========================================================================
# 扩展单位归一化 — 制药常用单位（mg/g/kg/mL/L 等）+ fail-closed
# ===========================================================================


class TestUnitNormalizationExpanded:
    """扩展单位换算表验证 — 制药常用质量/体积/浓度/压力单位。

    GMP 安全核心：spec 和 actual 使用不同但可换算的单位时必须正确换算，
    否则会导致漏报（不合格品被放行）。单位不一致且无换算规则时必须
    fail-closed（返回 unit_mismatch 触发人工复核），绝不静默按原值比较。
    """

    def test_mg_to_g_conversion(self):
        """spec="≤50mg", actual="0.1g"（=100mg，应不合格）。

        0.1g 必须换算为 100mg 才能正确判定越界。
        """
        converted, note = _try_unit_normalize(0.1, "g", "≤50mg")
        assert converted == 100  # 0.1g = 100mg
        assert "normalized" in note

    def test_unit_mismatch_fail_closed(self):
        """spec="≤50mg", actual="0.1mol"（无换算规则）应 fail-closed。"""
        converted, note = _try_unit_normalize(0.1, "mol", "≤50mg")
        assert converted is None
        assert note == "unit_mismatch"

    def test_extract_compound_unit_mg_per_l(self):
        """复合单位 mg/L 应被完整提取（不被 / 截断）。"""
        assert _extract_unit("1.5mg/L") == "mg/l"
        assert _extract_unit("100mg/mL") == "mg/ml"

    def test_g_to_kg_conversion(self):
        """1 g = 0.001 kg。"""
        converted, note = _try_unit_normalize(100.0, "g", "≤0.5kg")
        assert abs(converted - 0.1) < 0.0001  # 100g = 0.1kg
        assert "normalized" in note

    def test_ml_to_l_conversion(self):
        """1 mL = 0.001 L。"""
        converted, note = _try_unit_normalize(500.0, "ml", "0.5-1.0l")
        assert abs(converted - 0.5) < 0.0001  # 500mL = 0.5L
        assert "normalized" in note

    def test_mg_per_ml_to_mg_per_l(self):
        """1 mg/mL = 1000 mg/L。"""
        converted, note = _try_unit_normalize(2.0, "mg/ml", "≤1000mg/l")
        assert converted == 2000  # 2 mg/mL = 2000 mg/L（越界）
        assert "normalized" in note

    def test_bar_to_kpa_conversion(self):
        """1 bar = 100 kPa。"""
        converted, note = _try_unit_normalize(5.0, "bar", "≥200kpa")
        assert converted == 500  # 5 bar = 500 kPa
        assert "normalized" in note

    def test_no_actual_unit_compares_as_is(self):
        """actual 无单位时应按原值比较（不触发 mismatch）。"""
        converted, note = _try_unit_normalize(25.0, "", "20-30mg")
        assert converted == 25.0
        assert note == ""


# ===========================================================================
# 科学计数法解析 — 1.5×10^3 / 1.5e3 / 10**3
# ===========================================================================


class TestScientificNotation:
    """科学计数法展开验证。

    GMP 安全核心：spec 如 "≤1.5×10^3 cfu/g" 必须展开为 1500 才能正确判定，
    否则 0.1×10^3 这类实测值会被误解析为 0.1（漏报）。
    """

    def test_coefficient_times_power(self):
        """1.5×10^3 / 1.5*10^3 应展开为 1500。"""
        assert _expand_power_notation("1.5×10^3") == "1500"
        assert _expand_power_notation("1.5*10^3") == "1500"

    def test_e_notation(self):
        """1.5e3 / 1.5E3 应展开为 1500。"""
        assert _expand_power_notation("1.5e3") == "1500"
        assert _expand_power_notation("1.5E3") == "1500"

    def test_double_star(self):
        """10**3 应展开为 1000。"""
        assert _expand_power_notation("10**3") == "1000"

    def test_e_notation_with_prefix(self):
        """带前缀的科学计数法（≤1.5e3）应正确展开。"""
        result = _expand_power_notation("≤1.5e3")
        assert "1500" in result

    def test_coefficient_times_power_in_spec(self):
        """spec="≤1.5×10^3" 应解析为 high=1500。"""
        expanded = _expand_power_notation("≤1.5×10^3")
        bounds = _parse_spec(expanded)
        assert bounds is not None
        assert bounds.op == "le"
        assert bounds.high == 1500.0

    def test_e_notation_decimal_result(self):
        """非整数结果应保留小数（1.5e2 = 150.0 → '150'）。"""
        # 1.5 * 100 = 150.0 → 整数结果返回 '150'
        assert _expand_power_notation("1.5e2") == "150"

    def test_non_power_of_ten_base_not_expanded(self):
        """基数非 10 的幂（如 1.5*30）不应被展开。"""
        result = _expand_power_notation("1.5*30")
        assert result == "1.5*30"

    def test_superscript_still_works(self):
        """Unicode 上标 10³ 应展开为 1000（回归保护）。"""
        assert _expand_power_notation("10³") == "1000"
        assert _expand_power_notation("10⁵") == "100000"


# ===========================================================================
# 端到端 fail-closed 集成 — 单位不一致时 _judge_param / _judge_cell 行为
# ===========================================================================


class TestUnitMismatchFailClosedIntegration:
    """端到端验证：单位不一致且无换算规则时，_judge_param / _judge_cell
    应产出 completeness finding（人工复核），而非静默比较或漏报。

    GMP 安全核心：绝不能在不一致单位间静默比较（如 0.1mol vs ≤50mg），
    否则 0.1 ≤ 50 会误判合格，导致不合格品放行。
    """

    def test_param_unit_mismatch_produces_completeness_finding(self):
        """spec="≤50mg", actual="0.1mol" 应产出 completeness finding。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, parameters=[
                    {"name": "含量", "spec_range": "≤50mg", "value": "0.1mol", "unit": "mol"},
                ]),
            ]),
        ])
        llm_queue = []
        findings = _check_param_out_of_spec(pages, llm_queue)
        # 应产出 completeness finding（人工复核），不进 llm_queue，不产出 param_out_of_spec
        assert len(findings) == 1
        assert findings[0]["type"] == "completeness"
        assert findings[0]["severity"] == "warning"
        assert "单位不一致" in findings[0]["description"]
        assert "需人工确认" in findings[0]["description"]
        assert llm_queue == []

    def test_cell_unit_mismatch_produces_completeness_finding(self):
        """cell 单位不一致且无换算规则时应产出 completeness finding。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, measurements=[
                    {"time": "10:00", "values": {"压力": {"actual": "5bar", "spec": "≤50psi", "unit": "bar"}}},
                ]),
            ]),
        ])
        llm_queue = []
        findings = _check_param_out_of_spec(pages, llm_queue)
        assert len(findings) == 1
        assert findings[0]["type"] == "completeness"
        assert "单位不一致" in findings[0]["description"]
        assert llm_queue == []

    def test_cell_unit_conversion_produces_out_of_spec(self):
        """cell 有换算规则且越界时应产出 param_out_of_spec（不漏报）。

        spec="≤0.3mpa", actual="5bar"（=0.5mpa > 0.3mpa 越界）。
        """
        pages = _norm([
            _make_page(1, [
                _make_step(1, measurements=[
                    {"time": "10:00", "values": {"压力": {"actual": "5bar", "spec": "≤0.3mpa", "unit": "bar"}}},
                ]),
            ]),
        ])
        llm_queue = []
        findings = _check_param_out_of_spec(pages, llm_queue)
        assert len(findings) == 1
        assert findings[0]["type"] == "param_out_of_spec"
        assert "normalized" in findings[0]["description"]

    def test_param_unit_conversion_prevents_false_negative(self):
        """spec="≤50mg", actual="0.1g"（=100mg）应判越界，不漏报。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, parameters=[
                    {"name": "杂质", "spec_range": "≤50mg", "value": "0.1g", "unit": "g"},
                ]),
            ]),
        ])
        llm_queue = []
        findings = _check_param_out_of_spec(pages, llm_queue)
        # 0.1g = 100mg > 50mg → 越界，应产出 param_out_of_spec
        assert len(findings) == 1
        assert findings[0]["type"] == "param_out_of_spec"
        assert "归一化" in findings[0]["description"] or "normalized" in findings[0]["description"]


# ===========================================================================
# R7: 批号跨页一致性检查
# ===========================================================================


class TestBatchConsistency:
    """R7: 批号跨页一致性 — 不同批号指示装订错误或混批（严重 GMP 缺陷）。

    测试数据匹配生产 schema：batch_no 嵌套在 page_info 中（与
    _normalize_pages 输出格式一致），不再使用顶层 batch_no。
    """

    def test_single_batch_no_finding(self):
        pages = [
            {"page": 1, "page_info": {"batch_no": "B202201"}},
            {"page": 2, "page_info": {"batch_no": "B202201"}},
        ]
        findings = _check_batch_consistency(pages)
        assert findings == []

    def test_multiple_batch_nos_critical(self):
        pages = [
            {"page": 1, "page_info": {"batch_no": "B202201"}},
            {"page": 2, "page_info": {"batch_no": "B202202"}},
        ]
        findings = _check_batch_consistency(pages)
        assert len(findings) == 1
        assert findings[0]["severity"] == "critical"
        assert findings[0]["type"] == "batch_inconsistency"
        assert "装订错误" in findings[0]["description"]

    def test_pages_without_batch_no_skipped(self):
        pages = [
            {"page": 1, "page_info": {}},
            {"page": 2, "page_info": {"batch_no": "B202201"}},
        ]
        findings = _check_batch_consistency(pages)
        assert findings == []


# ===========================================================================
# QA 签名检查（R6 扩展）
# ===========================================================================


class TestQaSignatureCheck:
    """QA 签名检查 — 关键操作应有 QA 签名（info 级提示，避免误报过多）。"""

    def test_missing_qa_signature(self):
        pages = [{
            "page": 1,
            "steps": [{
                "step_no": 1,
                "operation": "称量",
                "start_time": "2022-05-07 14:00",
                "end_time": "2022-05-07 15:00",
                "signatures": [{"role": "operator", "name": "张三"}],
            }],
        }]
        findings = _check_completeness(pages)
        qa_findings = [f for f in findings if "QA" in f["description"]]
        assert len(qa_findings) == 1


# ===========================================================================
# 步骤无日期检查（R6 扩展）
# ===========================================================================


class TestStepWithoutDate:
    """步骤无日期检查 — 有操作描述但无执行时间的步骤应被标记（GMP 可追溯性要求）。"""

    def test_step_with_operation_but_no_time(self):
        pages = [{
            "page": 1,
            "steps": [{
                "step_no": 1,
                "operation": "称量原料",
                "signatures": [{"role": "operator", "name": "张三"}],
            }],
        }]
        findings = _check_completeness(pages)
        no_time_findings = [f for f in findings if "缺少执行时间" in f["description"]]
        assert len(no_time_findings) == 1


# ===========================================================================
# R8: 低置信度参数值人工复核提示
# ===========================================================================


class TestLowConfidenceParams:
    """R8: 低置信度参数值标记人工复核。

    OCR 可能误读手写数值（如 "0.974" → "0.914"），低置信度值即使通过规格
    检查也可能掩盖真实的越界结果，需提示人工核对原值。

    注意：原 measurements 中的 confidence/name/actual 字段在 v3 prompt
    schema 中不存在（measurements 为 {time, values:{列名:{spec,actual,unit,in_spec}}}），
    故移除针对单个 measurement 的低置信度检查，仅保留 overall_confidence
    页级检查（该字段由 per-page LLM 实际产出）。
    """

    def test_high_confidence_not_flagged(self):
        pages = [{
            "page": 1,
            "steps": [{
                "step_no": 1,
                "measurements": [
                    {"name": "温度", "actual": "25.5", "confidence": "high"},
                ],
            }],
        }]
        findings = _check_low_confidence_params(pages)
        assert findings == []

    def test_page_level_low_confidence(self):
        pages = [{"page": 1, "overall_confidence": "low", "steps": []}]
        findings = _check_low_confidence_params(pages)
        assert len(findings) == 1
        assert "整体识别置信度较低" in findings[0]["description"]

    def test_no_confidence_field_not_flagged(self):
        pages = [{
            "page": 1,
            "steps": [{
                "step_no": 1,
                "measurements": [{"name": "温度", "actual": "25.5"}],
            }],
        }]
        findings = _check_low_confidence_params(pages)
        assert findings == []


# ===========================================================================
# Phase 10: 用户自定义合规规则注入
# ===========================================================================


class TestUserRulesInjection:
    """用户规则注入 LLM 提示词（_user_rules_section + _llm_based_check）。"""

    def test_no_active_rules_returns_none_section(self, tmp_path):
        import json
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"user_rules": [
            {"text": "停用规则", "active": False},
        ]}), encoding="utf-8")
        with patch("config._config_path", return_value=cfg):
            section, rules_hash = _user_rules_section()
        assert section == ""
        assert rules_hash == "none"

    def test_no_rules_returns_none_section(self, tmp_path):
        import json
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"LLM_PROVIDER": "deepseek"}), encoding="utf-8")
        with patch("config._config_path", return_value=cfg):
            section, rules_hash = _user_rules_section()
        assert section == ""
        assert rules_hash == "none"

    def test_active_rules_produce_section_and_stable_hash(self, tmp_path):
        import json
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"user_rules": [
            {"id": "fixed-1", "text": "中间体储存温度必须 15-25°C", "active": True},
            {"id": "fixed-2", "text": "关键工序必须双人复核", "active": True},
        ]}), encoding="utf-8")
        with patch("config._config_path", return_value=cfg):
            section, rules_hash = _user_rules_section()
        assert "15-25°C" in section
        assert "双人复核" in section
        assert "[规则ID: " in section  # 每条规则注入唯一 ID 供 LLM 回填 rule_id
        assert rules_hash != "none"
        assert len(rules_hash) == 8
        with patch("config._config_path", return_value=cfg):
            _, rules_hash2 = _user_rules_section()
        assert rules_hash2 == rules_hash  # 相同内容 → 相同 hash

    def test_llm_based_check_injects_user_rules(self, tmp_path):
        """启用规则时 prompt 应包含规则段，prompt_version 应带 rules hash。"""
        import json
        from core.cross_page_analyzer import _llm_based_check
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"user_rules": [
            {"text": "产品 X 储存温度必须 15-25°C", "active": True},
        ]}), encoding="utf-8")

        captured = {}

        class FakeClient:
            async def chat_json(self, system_prompt, user_content, **kwargs):
                captured["system"] = system_prompt
                captured["user"] = user_content
                captured["audit"] = kwargs.get("audit_ctx", {})
                return []

        with patch("config._config_path", return_value=cfg), \
             patch("core.cross_page_analyzer.get_llm_client",
                   return_value=FakeClient()):
            asyncio.run(_llm_based_check("摘要内容", job_id="job-1"))

        assert "15-25°C" in captured["user"]
        assert captured["user"].startswith("用户自定义合规规则")
        assert captured["user"].endswith("摘要内容")
        assert captured["audit"]["prompt_version"].startswith("semantic_v2+rules")

    def test_llm_based_check_without_rules_uses_semantic_v2(self, tmp_path):
        """无规则时 prompt_version 应为 semantic_v2。"""
        import json
        from core.cross_page_analyzer import _llm_based_check
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({}), encoding="utf-8")

        captured = {}

        class FakeClient:
            async def chat_json(self, system_prompt, user_content, **kwargs):
                captured["user"] = user_content
                captured["audit"] = kwargs.get("audit_ctx", {})
                return []

        with patch("config._config_path", return_value=cfg), \
             patch("core.cross_page_analyzer.get_llm_client",
                   return_value=FakeClient()):
            asyncio.run(_llm_based_check("摘要内容", job_id="job-1"))

        assert captured["user"] == "摘要内容"
        assert captured["audit"]["prompt_version"] == "semantic_v2"

    def test_user_rule_findings_marked_with_user_rule_source(self, tmp_path):
        """type=user_rule 的 finding 应标记 source=user_rule，其余保持 llm_cross。"""
        import json
        from core.cross_page_analyzer import _llm_based_check
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({}), encoding="utf-8")

        class FakeClient:
            async def chat_json(self, system_prompt, user_content, **kwargs):
                return [
                    {"page": 1, "type": "user_rule", "severity": "warning",
                     "description": "储存温度 30°C 超出用户规则 15-25°C"},
                    {"page": 2, "type": "signature_mismatch", "severity": "critical",
                     "description": "签名不一致"},
                ]

        with patch("config._config_path", return_value=cfg), \
             patch("core.cross_page_analyzer.get_llm_client",
                   return_value=FakeClient()):
            findings = asyncio.run(_llm_based_check("摘要", job_id="job-1"))

        sources = {f["type"]: f["source"] for f in findings}
        assert sources == {"user_rule": "user_rule", "signature_mismatch": "llm_cross"}

    def test_user_rule_findings_carry_valid_rule_id(self, tmp_path):
        """rule_id 只在启用规则集合内保留；幻觉/缺失 id 一律置 None（防伪）。"""
        import json
        from core.cross_page_analyzer import _llm_based_check
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"user_rules": [
            {"id": "abc123", "text": "储存温度必须 15-25°C", "active": True},
            {"id": "def456", "text": "双人复核", "active": False},
        ]}), encoding="utf-8")

        class FakeClient:
            async def chat_json(self, system_prompt, user_content, **kwargs):
                return [
                    # 合法 id → 保留
                    {"page": 1, "type": "user_rule", "severity": "warning",
                     "description": "温度超标", "rule_id": "abc123"},
                    # 幻觉 id（不在启用集合，且指向停用规则）→ None
                    {"page": 2, "type": "user_rule", "severity": "warning",
                     "description": "复核缺失", "rule_id": "def456"},
                    # 完全编造 → None
                    {"page": 3, "type": "user_rule", "severity": "warning",
                     "description": "混批", "rule_id": "fake999"},
                    # 缺失 → None
                    {"page": 4, "type": "user_rule", "severity": "info",
                     "description": "无 id"},
                    # 非 user_rule 类型不受影响
                    {"page": 5, "type": "signature_mismatch", "severity": "critical",
                     "description": "签名不一致", "rule_id": "abc123"},
                ]

        with patch("config._config_path", return_value=cfg), \
             patch("core.cross_page_analyzer.get_llm_client",
                   return_value=FakeClient()):
            findings = asyncio.run(_llm_based_check("摘要", job_id="job-1"))

        by_page = {f["page"]: f["rule_id"] for f in findings}
        assert by_page[1] == "abc123"
        assert by_page[2] is None
        assert by_page[3] is None
        assert by_page[4] is None
        assert by_page[5] is None  # 非 user_rule 一律不带 rule_id
