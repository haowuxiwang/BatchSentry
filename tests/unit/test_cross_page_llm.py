"""Cross-page analyzer LLM 路径单元测试。

覆盖：
- _llm_fallback_check: 规则无法判定参数的 LLM 兜底判定
- _llm_based_check: 规则跑完后的 LLM 语义检查
- _check_year_contradiction: 同事件类型内年份不一致
- _check_suspicious_dates: 异常年份（<2000 或 >当前年+1）
- _check_signature_time_anomaly: 签名时间早于操作时间
- analyze_cross_page 端到端 LLM 路径触发
"""
import asyncio
from datetime import datetime
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from core.cross_page_analyzer import (
    analyze_cross_page,
    _llm_fallback_check,
    _llm_based_check,
    _check_year_contradiction,
    _check_suspicious_dates,
    _check_signature_time_anomaly,
    _normalize_pages,
)
from core.rules.rule_time import _check_signature_order


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


def _norm(pages):
    """将 raw page 结构归一化为 rule 函数所需的格式（顶层含 page_info/steps/...）。"""
    return _normalize_pages(pages)


def _make_step(step_no, start_time=None, end_time=None, operator="", reviewer="",
               measurements=None, parameters=None, signatures=None, checks=None):
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
    if checks:
        s["checks"] = checks
    return s


@pytest.fixture
def mock_llm():
    """Mock LLM 客户端 — 默认返回空 findings。

    patch core.rules.llm_checks.get_llm_client，使被测函数内部
    拿到的 client 完全可控。
    """
    mock_client = MagicMock()
    mock_client.chat_json = AsyncMock(return_value={"findings": []})
    with patch('core.rules.llm_checks.get_llm_client', return_value=mock_client):
        yield mock_client


# ===========================================================================
# _llm_fallback_check 测试
# ===========================================================================


class TestLLMFallbackCheck:
    """LLM 兜底判定 — 规则无法判定的参数（spec 非数字范围，如"应澄清"）。"""

    @pytest.mark.asyncio
    async def test_empty_queue_returns_empty(self, mock_llm):
        """空队列不应调用 LLM。"""
        result = await _llm_fallback_check([])
        assert result == []
        mock_llm.chat_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_spec_false_produces_finding(self, mock_llm):
        """LLM 判定不合规时应产生 param_out_of_spec finding。"""
        llm_queue = [
            {"page": 1, "step_no": 1, "name": "外观", "spec": "应澄清",
             "actual": "浑浊", "unit": "", "kind": "param"},
        ]
        mock_llm.chat_json.return_value = [
            {"index": 1, "in_spec": False, "reason": "溶液浑浊不符合澄清要求"},
        ]
        findings = await _llm_fallback_check(llm_queue)
        assert len(findings) == 1
        f = findings[0]
        assert f["type"] == "param_out_of_spec"
        assert f["severity"] == "warning"
        assert f["source"] == "llm_fallback"
        assert f["page"] == 1
        assert "外观" in f["description"]
        assert "应澄清" in f["description"]
        assert "LLM 判定" in f["description"]
        assert f["ocr_text"] == "外观: spec=应澄清 actual=浑浊"

    @pytest.mark.asyncio
    async def test_in_spec_null_produces_completeness_finding(self, mock_llm):
        """LLM 无法判定（in_spec=null）时应产生 completeness finding。"""
        llm_queue = [
            {"page": 2, "step_no": 3, "name": "含量", "spec": "符合要求",
             "actual": "99.5%", "unit": "", "kind": "param"},
        ]
        mock_llm.chat_json.return_value = [
            {"index": 1, "in_spec": None, "reason": "无法判定"},
        ]
        findings = await _llm_fallback_check(llm_queue)
        assert len(findings) == 1
        f = findings[0]
        assert f["type"] == "completeness"
        assert f["severity"] == "warning"
        assert f["source"] == "llm_fallback"
        assert f["page"] == 2
        assert "含量" in f["description"]
        assert "符合要求" in f["description"]
        assert "人工确认" in f["description"]

    @pytest.mark.asyncio
    async def test_in_spec_true_no_finding(self, mock_llm):
        """LLM 判定合规时不应产生 finding。"""
        llm_queue = [
            {"page": 1, "step_no": 1, "name": "外观", "spec": "应澄清",
             "actual": "澄清", "unit": "", "kind": "param"},
        ]
        mock_llm.chat_json.return_value = [
            {"index": 1, "in_spec": True, "reason": "溶液澄清"},
        ]
        findings = await _llm_fallback_check(llm_queue)
        assert findings == []

    @pytest.mark.asyncio
    async def test_exception_returns_review_findings(self, mock_llm):
        """LLM 调用异常时应 fail-closed：返回人工复核 findings 而非空列表。"""
        llm_queue = [
            {"page": 1, "step_no": 1, "name": "外观", "spec": "应澄清",
             "actual": "澄清", "unit": "", "kind": "param"},
        ]
        mock_llm.chat_json.side_effect = RuntimeError("LLM unavailable")
        findings = await _llm_fallback_check(llm_queue)
        assert len(findings) == 1
        f = findings[0]
        assert f["type"] == "completeness"
        assert f["severity"] == "warning"
        assert f["source"] == "rule"
        assert f["page"] == 1
        assert "外观" in f["description"]
        assert "应澄清" in f["description"]
        assert "人工确认" in f["description"]
        assert "LLM 调用失败" in f["description"]
        assert "RuntimeError" in f["description"]

    @pytest.mark.asyncio
    async def test_parse_error_returns_review_findings(self, mock_llm):
        """LLM 返回 _parse_error 时应 fail-closed：返回人工复核 findings。"""
        llm_queue = [
            {"page": 1, "step_no": 1, "name": "外观", "spec": "应澄清",
             "actual": "澄清", "unit": "", "kind": "param"},
        ]
        mock_llm.chat_json.return_value = {"_parse_error": True, "_raw": "garbage"}
        findings = await _llm_fallback_check(llm_queue)
        assert len(findings) == 1
        f = findings[0]
        assert f["type"] == "completeness"
        assert f["severity"] == "warning"
        assert f["source"] == "rule"
        assert f["page"] == 1
        assert "外观" in f["description"]
        assert "应澄清" in f["description"]
        assert "人工确认" in f["description"]

    @pytest.mark.asyncio
    async def test_polluted_items_do_not_crash(self, mock_llm):
        """P1-2: fallback 返回类型污染（items 含非 dict、index 非数字、
        result 非 list/dict）→ 跳过非法项，不抛异常。"""
        llm_queue = [
            {"page": 1, "step_no": 1, "name": "外观", "spec": "应澄清",
             "actual": "澄清", "unit": "", "kind": "param"},
            {"page": 2, "step_no": 1, "name": "pH", "spec": "5-9",
             "actual": "11", "unit": "", "kind": "param"},
        ]
        mock_llm.chat_json.return_value = [
            "garbage-item",
            {"index": "1", "in_spec": False, "reason": "浑浊"},
            {"index": "not-a-number", "in_spec": False, "reason": "bogus"},
            {"index": 99, "in_spec": False, "reason": "out of range"},
        ]
        findings = await _llm_fallback_check(llm_queue)
        # 只有 index=1 的合法项生效；非 dict / index 非法项被跳过
        assert len(findings) == 1
        assert findings[0]["page"] == 1
        assert findings[0]["type"] == "param_out_of_spec"

    @pytest.mark.asyncio
    async def test_polluted_result_object_returns_no_findings(self, mock_llm):
        """P1-2: fallback 返回非 list/dict（如字符串）→ 空 findings，不崩溃。"""
        llm_queue = [
            {"page": 1, "step_no": 1, "name": "外观", "spec": "应澄清",
             "actual": "澄清", "unit": "", "kind": "param"},
        ]
        mock_llm.chat_json.return_value = "not-json-at-all"
        findings = await _llm_fallback_check(llm_queue)
        assert findings == []

    @pytest.mark.asyncio
    async def test_dict_result_with_items_key(self, mock_llm):
        """LLM 返回 dict 包含 items 键时也能正确解析。"""
        llm_queue = [
            {"page": 1, "step_no": 1, "name": "外观", "spec": "应澄清",
             "actual": "浑浊", "unit": "", "kind": "param"},
        ]
        mock_llm.chat_json.return_value = {
            "items": [{"index": 1, "in_spec": False, "reason": "浑浊"}],
        }
        findings = await _llm_fallback_check(llm_queue)
        assert len(findings) == 1
        assert findings[0]["type"] == "param_out_of_spec"

    @pytest.mark.asyncio
    async def test_invalid_index_skipped(self, mock_llm):
        """index 越界时该条目应被跳过。"""
        llm_queue = [
            {"page": 1, "step_no": 1, "name": "外观", "spec": "应澄清",
             "actual": "浑浊", "unit": "", "kind": "param"},
        ]
        mock_llm.chat_json.return_value = [
            {"index": 99, "in_spec": False, "reason": "..."},  # 越界，跳过
            {"index": 1, "in_spec": False, "reason": "..."},   # 有效
        ]
        findings = await _llm_fallback_check(llm_queue)
        assert len(findings) == 1
        assert findings[0]["page"] == 1

    @pytest.mark.asyncio
    async def test_cell_kind_with_time_field(self, mock_llm):
        """cell 类型参数（带 time 字段）也能正确判定。"""
        llm_queue = [
            {"page": 1, "step_no": 2, "name": "温度", "time": "10:00",
             "spec": "符合要求", "actual": "异常", "unit": "℃", "kind": "cell"},
        ]
        mock_llm.chat_json.return_value = [
            {"index": 1, "in_spec": False, "reason": "温度异常"},
        ]
        findings = await _llm_fallback_check(llm_queue)
        assert len(findings) == 1
        assert findings[0]["type"] == "param_out_of_spec"
        assert "温度" in findings[0]["description"]

    @pytest.mark.asyncio
    async def test_multiple_items_mixed_judgements(self, mock_llm):
        """队列中多条目混合判定（合规/不合规/无法判定）。"""
        llm_queue = [
            {"page": 1, "step_no": 1, "name": "外观", "spec": "应澄清",
             "actual": "澄清", "unit": "", "kind": "param"},
            {"page": 1, "step_no": 2, "name": "颜色", "spec": "无色",
             "actual": "黄色", "unit": "", "kind": "param"},
            {"page": 2, "step_no": 1, "name": "气味", "spec": "符合要求",
             "actual": "异常", "unit": "", "kind": "param"},
        ]
        mock_llm.chat_json.return_value = [
            {"index": 1, "in_spec": True, "reason": "澄清"},
            {"index": 2, "in_spec": False, "reason": "颜色异常"},
            {"index": 3, "in_spec": None, "reason": "无法判定"},
        ]
        findings = await _llm_fallback_check(llm_queue)
        assert len(findings) == 2
        types = {f["type"] for f in findings}
        assert "param_out_of_spec" in types
        assert "completeness" in types


# ===========================================================================
# _llm_based_check 测试
# ===========================================================================


class TestLLMBasedCheck:
    """LLM 语义检查 — 规则跑完后捕获遗漏的语义异常。"""

    @pytest.mark.asyncio
    async def test_empty_summary_returns_empty(self, mock_llm):
        """空 summary 不应调用 LLM。"""
        result = await _llm_based_check("")
        assert result == []
        mock_llm.chat_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_whitespace_summary_returns_empty(self, mock_llm):
        """纯空白 summary 不应调用 LLM。"""
        result = await _llm_based_check("   \n  \t ")
        assert result == []
        mock_llm.chat_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_result_surfaces_findings(self, mock_llm):
        """LLM 返回 list 时应正确转换为 findings。"""
        mock_llm.chat_json.return_value = [
            {
                "page": 1,
                "type": "signature_mismatch",
                "severity": "warning",
                "description": "操作人与签名不一致",
                "ocr_text": "张三/李四",
                "operator": "张三",
            },
        ]
        findings = await _llm_based_check("第1页 ...")
        assert len(findings) == 1
        f = findings[0]
        assert f["source"] == "llm_cross"
        assert f["page"] == 1
        assert f["type"] == "signature_mismatch"
        assert f["severity"] == "warning"
        assert f["ocr_text"] == "张三/李四"
        assert f["operator"] == "张三"

    @pytest.mark.asyncio
    async def test_dict_result_with_findings_key(self, mock_llm):
        """LLM 返回 dict 包含 findings 键时也能正确解析。"""
        mock_llm.chat_json.return_value = {
            "findings": [
                {
                    "page": 2,
                    "type": "completeness",
                    "severity": "info",
                    "description": "缺少复核人签名",
                },
            ],
        }
        findings = await _llm_based_check("第2页 ...")
        assert len(findings) == 1
        assert findings[0]["page"] == 2
        assert findings[0]["source"] == "llm_cross"

    @pytest.mark.asyncio
    async def test_default_fields_added(self, mock_llm):
        """缺少 ocr_text/operator 时应填充默认值。"""
        mock_llm.chat_json.return_value = [
            {
                "page": 1,
                "type": "param_out_of_spec",
                "severity": "warning",
                "description": "参数漂移",
            },
        ]
        findings = await _llm_based_check("第1页 ...")
        assert len(findings) == 1
        assert findings[0]["ocr_text"] == ""
        assert findings[0]["operator"] == ""
        assert findings[0]["source"] == "llm_cross"

    @pytest.mark.asyncio
    async def test_invalid_finding_missing_fields_filtered(self, mock_llm):
        """缺少必需字段的 finding 应被过滤。"""
        mock_llm.chat_json.return_value = [
            {"page": 1, "type": "x", "severity": "warning"},  # 缺 description
            {"page": 1, "type": "x", "description": "...", "severity": "warning"},  # 有效
        ]
        findings = await _llm_based_check("第1页 ...")
        assert len(findings) == 1

    @pytest.mark.asyncio
    async def test_non_dict_finding_filtered(self, mock_llm):
        """非 dict 类型的 finding 应被过滤。"""
        mock_llm.chat_json.return_value = [
            "not a dict",
            {"page": 1, "type": "x", "severity": "warning", "description": "..."},
        ]
        findings = await _llm_based_check("第1页 ...")
        assert len(findings) == 1

    @pytest.mark.asyncio
    async def test_exception_returns_review_finding(self, mock_llm):
        """LLM 调用异常时应 fail-closed：返回人工复核 finding 而非空列表。"""
        mock_llm.chat_json.side_effect = RuntimeError("LLM unavailable")
        findings = await _llm_based_check("第1页 ...")
        assert len(findings) == 1
        f = findings[0]
        assert f["type"] == "completeness"
        assert f["severity"] == "info"
        assert f["source"] == "rule"
        assert f["page"] == 0
        assert "人工复核" in f["description"]
        assert "RuntimeError" in f["description"]

    @pytest.mark.asyncio
    async def test_parse_error_returns_review_finding(self, mock_llm):
        """LLM 返回 _parse_error 时应 fail-closed：返回人工复核 finding。"""
        mock_llm.chat_json.return_value = {"_parse_error": True, "_raw": "garbage"}
        findings = await _llm_based_check("第1页 ...")
        assert len(findings) == 1
        f = findings[0]
        assert f["type"] == "completeness"
        assert f["severity"] == "info"
        assert f["source"] == "rule"
        assert f["page"] == 0
        assert "人工复核" in f["description"]
        assert "JSON 解析失败" in f["description"]


# ===========================================================================
# _check_year_contradiction 测试
# ===========================================================================


class TestYearContradiction:
    """R2: 同事件类型内年份不一致。"""

    def test_single_event_multiple_years_produces_finding(self):
        """同事件类型有多个不同年份时应产生 finding。"""
        pages = _norm([
            _make_page(1, [], event_year_groups={"draft": [2024, 2025]}),
        ])
        findings = _check_year_contradiction(pages)
        assert len(findings) == 1
        f = findings[0]
        assert f["type"] == "year_contradiction"
        assert f["severity"] == "warning"
        assert f["page"] == 1
        assert f["source"] == "rule"
        assert f["operator"] == ""
        assert "draft" in f["description"]
        assert "2024" in f["ocr_text"]
        assert "2025" in f["ocr_text"]
        assert "需人工确认" in f["description"]

    def test_single_year_no_finding(self):
        """事件类型只有一个年份时不应产生 finding。"""
        pages = _norm([
            _make_page(1, [], event_year_groups={"draft": [2024]}),
        ])
        findings = _check_year_contradiction(pages)
        assert findings == []

    def test_duplicate_years_no_finding(self):
        """重复的同一年份不应产生 finding（去重后只有一个）。"""
        pages = _norm([
            _make_page(1, [], event_year_groups={"draft": [2024, 2024, 2024]}),
        ])
        findings = _check_year_contradiction(pages)
        assert findings == []

    def test_multiple_events_same_page(self):
        """同一页多个事件类型都有年份冲突时应分别产生 finding。"""
        pages = _norm([
            _make_page(1, [], event_year_groups={
                "draft": [2024, 2025],
                "production": [2023, 2024],
            }),
        ])
        findings = _check_year_contradiction(pages)
        assert len(findings) == 2
        event_types = {f["description"].split(" ")[1] for f in findings}
        assert "draft" in event_types
        assert "production" in event_types

    def test_empty_event_year_groups_no_finding(self):
        """空 event_year_groups 不应产生 finding。"""
        pages = _norm([_make_page(1, [])])
        findings = _check_year_contradiction(pages)
        assert findings == []

    def test_unsupported_event_type_ignored(self):
        """不在枚举范围内的事件类型不应被检查。"""
        pages = _norm([
            _make_page(1, [], event_year_groups={"custom_event": [2024, 2025]}),
        ])
        findings = _check_year_contradiction(pages)
        assert findings == []

    def test_multiple_pages_each_checked(self):
        """多页应分别检查。"""
        pages = _norm([
            _make_page(1, [], event_year_groups={"draft": [2024, 2025]}),
            _make_page(2, [], event_year_groups={"review": [2023, 2024]}),
        ])
        findings = _check_year_contradiction(pages)
        assert len(findings) == 2
        pages_in_findings = {f["page"] for f in findings}
        assert pages_in_findings == {1, 2}


# ===========================================================================
# _check_suspicious_dates 测试
# ===========================================================================


class TestSuspiciousDates:
    """R4: 异常年份（<2000 或 >当前年+1）。"""

    def test_old_year_produces_finding(self):
        """年份早于 2000 应产生 finding。"""
        pages = _norm([
            _make_page(1, [], page_info={"production_date": "1990.05.01"}),
        ])
        findings = _check_suspicious_dates(pages)
        assert len(findings) == 1
        f = findings[0]
        assert f["type"] == "suspicious_date"
        assert f["severity"] == "warning"
        assert f["page"] == 1
        assert f["source"] == "rule"
        assert f["operator"] == ""
        assert "1990" in f["description"]
        assert "异常" in f["description"]
        assert f["ocr_text"] == "1990.05.01"

    def test_future_year_produces_finding(self):
        """年份晚于当前年+1 应产生 finding。"""
        future_year = datetime.now().year + 10
        pages = _norm([
            _make_page(1, [], page_info={"production_date": f"{future_year}.06.15"}),
        ])
        findings = _check_suspicious_dates(pages)
        assert len(findings) == 1
        assert findings[0]["type"] == "suspicious_date"
        assert str(future_year) in findings[0]["description"]

    def test_normal_year_no_finding(self):
        """正常年份不应产生 finding。"""
        pages = _norm([
            _make_page(1, [], page_info={"production_date": "2024.05.01"}),
        ])
        findings = _check_suspicious_dates(pages)
        assert findings == []

    def test_step_time_old_year_produces_finding(self):
        """step 的 start_time/end_time 含异常年份也应产生 finding。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="1985.01.01 10:00", end_time="1985.01.01 11:00"),
            ]),
        ])
        findings = _check_suspicious_dates(pages)
        assert len(findings) >= 1
        assert all(f["type"] == "suspicious_date" for f in findings)
        assert all("1985" in f["description"] for f in findings)

    def test_signature_time_future_year_produces_finding(self):
        """签名时间含异常年份也应产生 finding。"""
        future_year = datetime.now().year + 5
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00", end_time="2024.01.01 11:00",
                           signatures=[{"role": "operator", "name": "张三",
                                        "sign_time": f"{future_year}.02.01 10:30"}]),
            ]),
        ])
        findings = _check_suspicious_dates(pages)
        assert any(f["type"] == "suspicious_date" and str(future_year) in f["description"]
                   for f in findings)

    def test_duplicate_date_strings_deduplicated(self):
        """相同的日期字符串应去重，只产生一个 finding。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00", end_time="2024.01.01 11:00",
                           signatures=[
                               {"role": "operator", "name": "张三",
                                "sign_time": "1990.01.01 09:00"},
                               {"role": "reviewer", "name": "李四",
                                "sign_time": "1990.01.01 09:00"},
                           ]),
            ]),
        ])
        findings = _check_suspicious_dates(pages)
        suspicious_1990 = [f for f in findings if "1990" in f.get("description", "")]
        assert len(suspicious_1990) == 1

    def test_multiple_pages_each_checked(self):
        """多页应分别检查。"""
        pages = _norm([
            _make_page(1, [], page_info={"production_date": "1990.01.01"}),
            _make_page(2, [], page_info={"production_date": "1985.06.15"}),
        ])
        findings = _check_suspicious_dates(pages)
        assert len(findings) == 2
        pages_in_findings = {f["page"] for f in findings}
        assert pages_in_findings == {1, 2}


# ===========================================================================
# _check_signature_time_anomaly 测试
# ===========================================================================


class TestSignatureTimeAnomaly:
    """R5: 签名时间早于操作时间。"""

    def test_sign_before_op_produces_finding(self):
        """签名时间早于操作开始时间应产生 finding。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00", end_time="2024.01.01 11:00",
                           signatures=[
                               {"role": "operator", "name": "张三",
                                "sign_time": "2024.01.01 09:00"},
                           ]),
            ]),
        ])
        findings = _check_signature_time_anomaly(pages)
        assert len(findings) == 1
        f = findings[0]
        assert f["type"] == "signature_time_anomaly"
        assert f["severity"] == "warning"
        assert f["page"] == 1
        assert f["source"] == "rule"
        assert "张三" in f["description"]
        assert "签名时间" in f["description"]
        assert "操作时间" in f["description"]
        assert f["operator"] == "张三"
        assert f["ocr_text"] == "张三 2024.01.01 09:00"

    def test_sign_after_op_no_finding(self):
        """签名时间晚于操作时间不应产生 finding。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00", end_time="2024.01.01 11:00",
                           signatures=[
                               {"role": "operator", "name": "张三",
                                "sign_time": "2024.01.01 12:00"},
                           ]),
            ]),
        ])
        findings = _check_signature_time_anomaly(pages)
        assert findings == []

    def test_sign_equal_op_no_finding(self):
        """签名时间等于操作时间不应产生 finding（仅严格早于才报）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00",
                           signatures=[
                               {"role": "operator", "name": "张三",
                                "sign_time": "2024.01.01 10:00"},
                           ]),
            ]),
        ])
        findings = _check_signature_time_anomaly(pages)
        assert findings == []

    def test_no_signatures_no_finding(self):
        """没有签名不应产生 finding。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00", end_time="2024.01.01 11:00"),
            ]),
        ])
        findings = _check_signature_time_anomaly(pages)
        assert findings == []

    def test_empty_sign_time_skipped(self):
        """空 sign_time 应被跳过。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00",
                           signatures=[
                               {"role": "operator", "name": "张三", "sign_time": ""},
                           ]),
            ]),
        ])
        findings = _check_signature_time_anomaly(pages)
        assert findings == []

    def test_unparseable_sign_time_skipped(self):
        """无法解析的 sign_time 应被跳过（不抛异常）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00",
                           signatures=[
                               {"role": "operator", "name": "张三", "sign_time": "无效日期"},
                           ]),
            ]),
        ])
        findings = _check_signature_time_anomaly(pages)
        assert findings == []

    def test_date_only_sign_vs_datetime_op_same_day_no_finding(self):
        """date-only 签名时间（整天）与同日带时间操作时间不应误报（年月日 vs
        年月日小时分钟混比场景：修复前 sign=00:00 < op=14:30 误报）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 14:30",
                           signatures=[
                               {"role": "operator", "name": "张三",
                                "sign_time": "2024.01.01"},
                           ]),
            ]),
        ])
        findings = _check_signature_time_anomaly(pages)
        assert findings == []

    def test_date_only_sign_previous_day_vs_datetime_op_finding(self):
        """date-only 签名时间（前一天整天）早于带时间操作时间应报异常。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00",
                           signatures=[
                               {"role": "operator", "name": "张三",
                                "sign_time": "2023.12.31"},
                           ]),
            ]),
        ])
        findings = _check_signature_time_anomaly(pages)
        assert len(findings) == 1
        assert findings[0]["type"] == "signature_time_anomaly"

    def test_time_only_sign_uses_fallback_date(self):
        """只有 HH:MM 的签名时间应使用 page_info.production_date 作为 fallback。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00",
                           signatures=[
                               {"role": "operator", "name": "张三", "sign_time": "09:00"},
                           ]),
            ], page_info={"production_date": "2024.01.01"}),
        ])
        findings = _check_signature_time_anomaly(pages)
        assert len(findings) == 1
        assert findings[0]["type"] == "signature_time_anomaly"

    def test_end_time_used_when_no_start_time(self):
        """没有 start_time 时应使用 end_time 作为操作时间。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, end_time="2024.01.01 11:00",
                           signatures=[
                               {"role": "reviewer", "name": "李四",
                                "sign_time": "2024.01.01 10:00"},
                           ]),
            ]),
        ])
        findings = _check_signature_time_anomaly(pages)
        assert len(findings) == 1
        assert "李四" in findings[0]["description"]
        assert "2024.01.01 11:00" in findings[0]["description"]

    def test_multiple_signatures_multiple_findings(self):
        """多个签名异常应分别产生 finding。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00",
                           signatures=[
                               {"role": "operator", "name": "张三",
                                "sign_time": "2024.01.01 09:00"},
                               {"role": "reviewer", "name": "李四",
                                "sign_time": "2024.01.01 08:30"},
                           ]),
            ]),
        ])
        findings = _check_signature_time_anomaly(pages)
        assert len(findings) == 2
        names = {f["operator"] for f in findings}
        assert names == {"张三", "李四"}

    def test_year_gap_over_2_flags_ocr_confusion(self):
        """签名时间与操作时间年份相差 >2 年时（OCR 2015/2025 混淆，
        签名真实为 2025 却被读成 2015），仍报异常但描述附带 OCR 混淆提示，
        而非铁口断言时间矛盾。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2025.01.20 10:00",
                           signatures=[
                               {"role": "operator", "name": "张三",
                                "sign_time": "2015.01.20"},
                           ]),
            ]),
        ])
        findings = _check_signature_time_anomaly(pages)
        assert len(findings) == 1
        assert "年份相差 10 年" in findings[0]["description"]
        assert "OCR 提取错误" in findings[0]["description"]

    def test_year_gap_within_2_no_ocr_hint(self):
        """年份差 ≤2 时不附加 OCR 混淆提示（正常时间矛盾）。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00",
                           signatures=[
                               {"role": "operator", "name": "张三",
                                "sign_time": "2023.12.31"},
                           ]),
            ]),
        ])
        findings = _check_signature_time_anomaly(pages)
        assert len(findings) == 1
        assert "OCR 提取错误" not in findings[0]["description"]


# ===========================================================================
# R9a: 签名间顺序（复核/QA 必须晚于操作者签名）
# ===========================================================================


class TestSignatureOrder:
    """签名顺序规则 — reviewer/QA 签名时间不得早于 operator。"""

    def test_reviewer_before_operator_finding(self):
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 08:00",
                           signatures=[
                               {"role": "operator", "name": "张三",
                                "sign_time": "2024.01.01 10:00"},
                               {"role": "reviewer", "name": "李四",
                                "sign_time": "2024.01.01 09:00"},
                           ]),
            ]),
        ])
        findings = _check_signature_order(pages)
        assert len(findings) == 1
        f = findings[0]
        assert f["type"] == "signature_time_anomaly"
        assert "李四" in f["description"]
        assert "早于" in f["description"]

    def test_reviewer_after_operator_no_finding(self):
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 08:00",
                           signatures=[
                               {"role": "operator", "name": "张三",
                                "sign_time": "2024.01.01 09:00"},
                               {"role": "reviewer", "name": "李四",
                                "sign_time": "2024.01.01 10:00"},
                           ]),
            ]),
        ])
        assert _check_signature_order(pages) == []

    def test_date_only_reviewer_same_day_no_finding(self):
        """date-only 复核签名（整天）与同日操作签名点不误报。"""
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 08:00",
                           signatures=[
                               {"role": "operator", "name": "张三",
                                "sign_time": "2024.01.01 09:30"},
                               {"role": "reviewer", "name": "李四",
                                "sign_time": "2024.01.01"},
                           ]),
            ]),
        ])
        assert _check_signature_order(pages) == []

    def test_unranked_role_ignored(self):
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 08:00",
                           signatures=[
                               {"role": "witness", "name": "王五",
                                "sign_time": "2024.01.01 07:00"},
                               {"role": "operator", "name": "张三",
                                "sign_time": "2024.01.01 10:00"},
                           ]),
            ]),
        ])
        assert _check_signature_order(pages) == []

    def test_year_gap_over_2_flags_ocr(self):
        pages = _norm([
            _make_page(1, [
                _make_step(1, start_time="2025.01.20 08:00",
                           signatures=[
                               {"role": "operator", "name": "张三",
                                "sign_time": "2025.01.20 09:00"},
                               {"role": "reviewer", "name": "李四",
                                "sign_time": "2015.01.20"},
                           ]),
            ]),
        ])
        findings = _check_signature_order(pages)
        assert len(findings) == 1
        assert "年份相差 10 年" in findings[0]["description"]


# ===========================================================================
# R8b: 勾选一致性（复核项勾选"否"/无法识别）
# ===========================================================================


class TestCheckConsistency:
    """勾选一致性 — 检查项勾选"否"必须提示偏差处理，勾选不清提示人工核对。"""

    def test_checked_no_warning(self):
        from core.rules.rule_doc import _check_check_consistency

        pages = _norm([
            _make_page(1, [
                _make_step(1, checks=[
                    {"item": "生产场地是否整洁", "selected": "否", "marker": "√"},
                ]),
            ]),
        ])
        findings = _check_check_consistency(pages)
        assert len(findings) == 1
        assert findings[0]["severity"] == "warning"
        assert "偏差" in findings[0]["description"]

    def test_checked_yes_no_finding(self):
        from core.rules.rule_doc import _check_check_consistency

        pages = _norm([
            _make_page(1, [
                _make_step(1, checks=[
                    {"item": "生产场地是否整洁", "selected": "是", "marker": "☑"},
                    {"item": "设备状态标志使用是否正确", "selected": "N/A", "marker": "☑"},
                ]),
            ]),
        ])
        assert _check_check_consistency(pages) == []

    def test_unrecognizable_selection_info(self):
        from core.rules.rule_doc import _check_check_consistency

        pages = _norm([
            _make_page(1, [
                _make_step(1, checks=[
                    {"item": "洁净区压差是否符合要求", "selected": "无法识别", "marker": "手绘"},
                ]),
            ]),
        ])
        findings = _check_check_consistency(pages)
        assert len(findings) == 1
        assert findings[0]["severity"] == "info"

    def test_empty_checks_no_finding(self):
        from core.rules.rule_doc import _check_check_consistency

        pages = _norm([
            _make_page(1, [
                _make_step(1, checks=[]),
                _make_step(2),
            ]),
        ])
        assert _check_check_consistency(pages) == []


# ===========================================================================
# R3: 边缘超范围降噪（手写 OCR 误读可能）
# ===========================================================================


class TestOutOfSpecEdgeMargin:
    """param_out_of_spec 边缘降噪：≤10% 偏差降 info，>10% 维持 warning。"""

    def _run(self, spec, actual):
        from core.rules.rule_spec import _check_param_out_of_spec

        pages = _norm([
            _make_page(1, [
                _make_step(1, parameters=[
                    {"name": "温度", "spec_range": spec, "value": actual},
                ]),
            ]),
        ])
        findings = _check_param_out_of_spec(pages, [])
        return findings

    def test_edge_deviation_info(self):
        """5.4 vs ≤5（8% 超差）→ info + OCR 提示。"""
        f = self._run("≤5.0℃", "5.4℃")
        assert len(f) == 1
        assert f[0]["severity"] == "info"
        assert "手写 OCR 误读" in f[0]["description"]

    def test_large_deviation_warning(self):
        """25 vs ≤5（400% 超差）→ warning 铁口。"""
        f = self._run("≤5.0℃", "25℃")
        assert len(f) == 1
        assert f[0]["severity"] == "warning"
        assert "OCR 误读" not in f[0]["description"]

    def test_between_edge_low_side_info(self):
        """0.96 vs 1.0-2.0（4% 低于下限）→ info。"""
        f = self._run("1.0-2.0", "0.96")
        assert len(f) == 1
        assert f[0]["severity"] == "info"

    def test_exact_boundary_is_in_spec(self):
        """恰好 5.0 vs ≤5 → 合格，无 finding。"""
        assert self._run("≤5.0℃", "5.0℃") == []


# ===========================================================================
# 集成：通过 analyze_cross_page 触发 LLM 路径
# ===========================================================================


class TestAnalyzeCrossPageLLMPaths:
    """通过 analyze_cross_page 主入口验证 LLM 路径被正确触发。"""

    def test_unparseable_spec_goes_to_llm_fallback(self, mock_llm):
        """spec 无法解析（如"应澄清"）时应进入 LLM fallback 队列并被判定。"""
        pages = [
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00", end_time="2024.01.01 11:00",
                           operator="张三", reviewer="李四",
                           parameters=[
                               {"name": "外观", "spec_range": "应澄清",
                                "value": "浑浊", "unit": ""},
                           ]),
            ]),
        ]
        # 第一次 chat_json 调用：_llm_fallback_check
        # 第二次 chat_json 调用：_llm_based_check
        mock_llm.chat_json.side_effect = [
            [{"index": 1, "in_spec": False, "reason": "溶液浑浊不符合澄清要求"}],
            [],
        ]
        findings = asyncio.run(analyze_cross_page(pages))
        fallback_findings = [f for f in findings if f.get("source") == "llm_fallback"]
        assert len(fallback_findings) == 1
        assert fallback_findings[0]["type"] == "param_out_of_spec"
        assert "外观" in fallback_findings[0]["description"]
        assert "应澄清" in fallback_findings[0]["description"]
        # 应调用 LLM 两次
        assert mock_llm.chat_json.call_count == 2

    def test_llm_based_check_findings_surfaced(self, mock_llm):
        """_llm_based_check 产生的 findings 应出现在最终结果中。"""
        pages = [
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00", end_time="2024.01.01 11:00",
                           operator="张三", reviewer="李四"),
            ]),
        ]
        # llm_queue 为空，_llm_fallback_check 不会调用 chat_json
        # 只有 _llm_based_check 调用一次 chat_json
        mock_llm.chat_json.return_value = [
            {
                "page": 1,
                "type": "signature_mismatch",
                "severity": "warning",
                "description": "签名与操作人不一致",
                "ocr_text": "张三/王五",
                "operator": "张三",
            },
        ]
        findings = asyncio.run(analyze_cross_page(pages))
        llm_findings = [f for f in findings if f.get("source") == "llm_cross"]
        assert len(llm_findings) == 1
        assert llm_findings[0]["type"] == "signature_mismatch"
        assert llm_findings[0]["page"] == 1
        assert llm_findings[0]["operator"] == "张三"
        # 只调用一次（llm_fallback 因空队列未调用）
        assert mock_llm.chat_json.call_count == 1

    def test_per_page_llm_findings_pass_through(self, mock_llm):
        """per-page LLM 产生的 findings 应透传到最终结果（source=llm_page）。"""
        pages = [
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00", end_time="2024.01.01 11:00",
                           operator="张三", reviewer="李四"),
            ], findings=[
                {
                    "type": "time_reversal",
                    "severity": "critical",
                    "description": "页面级 LLM 发现的时间倒序",
                    "ocr_text": "...",
                    "operator": "",
                },
            ]),
        ]
        mock_llm.chat_json.return_value = []
        findings = asyncio.run(analyze_cross_page(pages))
        per_page_findings = [f for f in findings if f.get("source") == "llm_page"]
        assert len(per_page_findings) == 1
        assert per_page_findings[0]["page"] == 1
        assert per_page_findings[0]["type"] == "time_reversal"
        assert per_page_findings[0]["severity"] == "critical"

    def test_empty_pages_no_llm_call(self, mock_llm):
        """空页面列表不应调用 LLM。"""
        findings = asyncio.run(analyze_cross_page([]))
        assert findings == []
        mock_llm.chat_json.assert_not_called()

    def test_llm_fallback_and_llm_based_both_invoked(self, mock_llm):
        """同时存在 unparseable spec 和正常 step 时，两个 LLM 路径都应被触发。"""
        pages = [
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00", end_time="2024.01.01 11:00",
                           operator="张三", reviewer="李四",
                           parameters=[
                               {"name": "外观", "spec_range": "应澄清",
                                "value": "澄清", "unit": ""},
                           ]),
            ]),
        ]
        mock_llm.chat_json.side_effect = [
            # _llm_fallback_check: 判定合规
            [{"index": 1, "in_spec": True, "reason": "澄清"}],
            # _llm_based_check: 返回 1 条语义 finding
            [{"page": 1, "type": "completeness", "severity": "info",
              "description": "批次逻辑存疑"}],
        ]
        findings = asyncio.run(analyze_cross_page(pages))
        # LLM fallback 合规，无 finding
        fallback_findings = [f for f in findings if f.get("source") == "llm_fallback"]
        assert len(fallback_findings) == 0
        # LLM based check 返回 1 条
        llm_findings = [f for f in findings if f.get("source") == "llm_cross"]
        assert len(llm_findings) == 1
        assert llm_findings[0]["type"] == "completeness"
        # 两个路径都调用了 chat_json
        assert mock_llm.chat_json.call_count == 2


# ===========================================================================
# Fail-closed 行为测试（GMP 安全原则：LLM 不可用时不得静默放行）
# ===========================================================================


class TestLlmFallbackFailClosed:
    """LLM 兜底/语义检查失败时应 fail-closed，标记人工复核而非返回空列表。"""

    @pytest.mark.asyncio
    async def test_llm_exception_returns_review_findings(self):
        """LLM 调用失败时应返回人工复核 findings 而非空列表。"""
        from core.cross_page_analyzer import _llm_fallback_check
        llm_queue = [
            {"page": 1, "step_no": 1, "name": "温度", "spec": "≤25°C", "actual": "30", "unit": "°C"},
        ]
        with patch("core.rules.llm_checks.get_llm_client") as mock:
            mock.return_value.chat_json.side_effect = RuntimeError("API down")
            findings = await _llm_fallback_check(llm_queue, job_id="test")
        assert len(findings) == 1
        assert findings[0]["type"] == "completeness"
        assert "人工确认" in findings[0]["description"]
        assert findings[0]["source"] == "rule"

    @pytest.mark.asyncio
    async def test_llm_exception_flags_all_queued_params(self):
        """LLM 调用失败时队列中所有参数都应被标记（不可只标记第一个）。"""
        from core.cross_page_analyzer import _llm_fallback_check
        llm_queue = [
            {"page": 1, "step_no": 1, "name": "温度", "spec": "≤25°C", "actual": "30", "unit": "°C"},
            {"page": 2, "step_no": 3, "name": "外观", "spec": "应澄清", "actual": "浑浊", "unit": ""},
            {"page": 3, "step_no": 5, "name": "pH", "spec": "6.0-7.0", "actual": "5.5", "unit": ""},
        ]
        with patch("core.rules.llm_checks.get_llm_client") as mock:
            mock.return_value.chat_json.side_effect = TimeoutError("timeout")
            findings = await _llm_fallback_check(llm_queue, job_id="test")
        assert len(findings) == 3
        pages = {f["page"] for f in findings}
        assert pages == {1, 2, 3}
        assert all(f["type"] == "completeness" for f in findings)
        assert all(f["source"] == "rule" for f in findings)
        assert all("人工确认" in f["description"] for f in findings)
        assert all("TimeoutError" in f["description"] for f in findings)

    @pytest.mark.asyncio
    async def test_llm_parse_error_returns_review_findings(self):
        """LLM 返回无法解析的 JSON 时应 fail-closed 标记人工复核。"""
        from core.cross_page_analyzer import _llm_fallback_check
        llm_queue = [
            {"page": 2, "step_no": 2, "name": "含量", "spec": "≥98.0%", "actual": "97.0", "unit": "%"},
        ]
        with patch("core.rules.llm_checks.get_llm_client") as mock:
            mock.return_value.chat_json = AsyncMock(return_value={"_parse_error": True, "_raw": "not json"})
            findings = await _llm_fallback_check(llm_queue, job_id="test")
        assert len(findings) == 1
        f = findings[0]
        assert f["type"] == "completeness"
        assert f["source"] == "rule"
        assert f["page"] == 2
        assert "JSON 解析失败" in f["description"]
        assert "人工确认" in f["description"]

    def test_fallback_fail_closed_through_analyze_cross_page(self, mock_llm):
        """端到端：LLM fallback 异常时 findings 应进入最终结果（不丢失）。"""
        pages = [
            _make_page(1, [
                _make_step(1, start_time="2024.01.01 10:00", end_time="2024.01.01 11:00",
                           operator="张三", reviewer="李四",
                           signatures=[{"role": "qa", "name": "王五"}],
                           parameters=[
                               {"name": "外观", "spec_range": "应澄清",
                                "value": "浑浊", "unit": ""},
                           ]),
            ]),
        ]
        # 第一次 chat_json：_llm_fallback_check 抛异常 → fail-closed
        # 第二次 chat_json：_llm_based_check 返回空 list（成功）
        mock_llm.chat_json.side_effect = [
            RuntimeError("API down"),
            [],
        ]
        findings = asyncio.run(analyze_cross_page(pages))
        review_findings = [f for f in findings if f.get("type") == "completeness"
                           and f.get("source") == "rule"]
        assert len(review_findings) == 1
        assert "外观" in review_findings[0]["description"]
        assert "人工确认" in review_findings[0]["description"]


class TestSummaryTruncation:
    """_build_summary context-budget 护栏：字段压缩 → 逐行截断 + 显式标记。

    对应 best practice：跨页 prompt 注入量必须封顶（51 页实测 ~23K tokens 约
    22% 窗口，100+ 页记录会逼近质量退化区），超限时宁可带标记 drop 也不
    静默截断（防 LLM 把缺失当正常，产生"内容不存在"类幻觉 finding）。
    """

    def _pages(self, n, op_len, findings=None):
        return [
            {
                "page": i,
                "page_info": {"title": f"记录页 {i}",
                              "production_date": "2026-01-01",
                              "batch_no": "B20260001"},
                "steps": [{
                    "step_no": 1,
                    "operation": "操" * op_len,
                    "start_time": "08:00",
                    "end_time": "09:00",
                    "operator": "张健",
                    "reviewer": "周华",
                    "parameters": [{"name": "温度", "value": "25", "unit": "℃"}],
                    "measurements": [{"time": "08:30", "values": {
                        "实测流速": {"actual": "0.97"},
                        "实测压力": {"actual": "0.15"},
                        "实测温度": {"actual": "25.0"},
                        "实测pH": {"actual": "7.2"},
                        "实测浊度": {"actual": "1.1"},
                    }}],
                    "signatures": [{"role": "operator", "name": "张健",
                                    "sign_time": "2026-01-01 09:00"}],
                }],
                "findings": findings or [],
                "event_year_groups": {},
            }
            for i in range(1, n + 1)
        ]

    def test_short_summary_within_cap(self):
        from core.rules.llm_checks import _build_summary, _SUMMARY_MAX_CHARS
        text = _build_summary(self._pages(2, 30))
        assert len(text) <= _SUMMARY_MAX_CHARS
        assert "内容过长已截断" not in text
        assert "B20260001" in text

    def test_oversized_compresses_fields_to_fit(self):
        from core.rules.llm_checks import (_build_summary, _summary_lines,
                                           _SUMMARY_MAX_CHARS)
        pages = self._pages(400, 700)
        # 前置：正常宽度（op 60）构建确实超限，压缩测试才有意义
        assert len("\n".join(_summary_lines(pages, op_len=60, meas_cols=4,
                                            findings_n=5))) > _SUMMARY_MAX_CHARS
        text = _build_summary(pages)
        assert len(text) <= _SUMMARY_MAX_CHARS
        # 第一级字段压缩（operation 60→24）后应放得下，无需逐行截断
        assert "内容过长已截断" not in text
        assert "操" * 24 in text

    def test_line_truncate_with_explicit_marker(self):
        from core.rules.llm_checks import _build_summary, _SUMMARY_MAX_CHARS
        pages = self._pages(5000, 700)
        text = _build_summary(pages)
        assert len(text) <= _SUMMARY_MAX_CHARS + 200
        assert "内容过长已截断" in text
        # 截断标记显式告知 LLM 输入不完整（防缺失当正常的幻觉）
        assert "未注入" in text

    def test_compression_reduces_sizes(self):
        from core.rules.llm_checks import _summary_lines, _OP_LEN_COMPRESSED
        full = _summary_lines(self._pages(1, 200), op_len=60, meas_cols=4,
                              findings_n=5)
        comp = _summary_lines(self._pages(1, 200), op_len=_OP_LEN_COMPRESSED,
                              meas_cols=2, findings_n=2)
        assert len("\n".join(comp)) < len("\n".join(full))
        assert any("... " in ln or ln.endswith("...") for ln in comp
                   if ln.strip().startswith("@"))

    def test_summary_max_chars_derivation(self):
        """窗口→摘要预算推导：35% 窗口 × 1.6 字符/token；非法输入回退默认。"""
        from core.rules.llm_checks import _summary_max_chars
        assert _summary_max_chars(128_000) == int(128_000 * 0.35 * 1.6)
        assert _summary_max_chars(32_000) == int(32_000 * 0.35 * 1.6)
        assert _summary_max_chars(None) == int(128_000 * 0.35 * 1.6)
        assert _summary_max_chars(0) == int(128_000 * 0.35 * 1.6)
        assert _summary_max_chars(-5) == int(128_000 * 0.35 * 1.6)
        assert _summary_max_chars("bogus") == int(128_000 * 0.35 * 1.6)
        assert _summary_max_chars(10_000) == 8_000  # floor 兜底，避免小窗饿死

    def test_small_window_triggers_line_truncate_sooner(self):
        """同一 400 页输入：32K 窗口 → 显式截断；默认窗口 → 仅字段压缩。"""
        from core.rules.llm_checks import (
            _build_summary, _summary_max_chars, _SUMMARY_MAX_CHARS)
        pages = self._pages(400, 700)
        cap32 = _summary_max_chars(32_000)
        assert cap32 < _SUMMARY_MAX_CHARS
        text = _build_summary(pages, context_window=32_000)
        assert len(text) <= cap32 + 200
        assert "内容过长已截断" in text
        assert "内容过长已截断" not in _build_summary(pages)
