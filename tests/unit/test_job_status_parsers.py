"""api/jobs/status.py 纯函数单测 — OCR/自愈/跨页进度解析与阶段派生。

覆盖 SSE 推送与状态端点共用的解析层（此前 _parse_* 的异常/边界分支、
_derive_phase 的 ocr_done/idle/cross_started 分支从未被触发）。
"""
from __future__ import annotations

import pytest

from api.jobs.status import (
    _derive_phase,
    _parse_cross_progress,
    _parse_failed_pages,
    _parse_ocr_progress,
    _parse_self_heal_progress,
)


class TestParseOcrProgress:
    def test_none_and_empty(self):
        assert _parse_ocr_progress(None) == {}
        assert _parse_ocr_progress("") == {}

    def test_valid_values(self):
        assert _parse_ocr_progress('{"done": 3, "total": 9}') == {"done": 3, "total": 9}

    def test_missing_keys_default_to_zero(self):
        assert _parse_ocr_progress('{"done": 5}') == {"done": 5, "total": 0}

    def test_non_dict_payload_returns_empty(self):
        assert _parse_ocr_progress("[1, 2, 3]") == {}

    def test_invalid_json_returns_empty(self):
        assert _parse_ocr_progress("{not json") == {}

    def test_non_numeric_values_raise_into_guard(self):
        """done/total 非数值 → int() 抛 TypeError → 兜底空 dict。"""
        assert _parse_ocr_progress('{"done": "x", "total": "y"}') == {}


class TestParseSelfHealProgress:
    def test_none_returns_none(self):
        assert _parse_self_heal_progress(None) is None

    def test_no_self_heal_key(self):
        assert _parse_self_heal_progress('{"done": 1, "total": 2}') is None

    def test_valid_subkey(self):
        got = _parse_self_heal_progress(
            '{"self_heal": {"done": 2, "total": 6, "pages": [3, 7]}}'
        )
        assert got == {"done": 2, "total": 6, "pages": [3, 7]}

    def test_zero_total_hides_progress(self):
        assert _parse_self_heal_progress('{"self_heal": {"done": 0, "total": 0}}') is None

    def test_missing_pages_defaults_empty(self):
        got = _parse_self_heal_progress('{"self_heal": {"done": 1, "total": 3}}')
        assert got == {"done": 1, "total": 3, "pages": []}

    def test_invalid_json_returns_none(self):
        assert _parse_self_heal_progress("{bad") is None


class TestParseCrossProgress:
    def test_none_returns_none(self):
        assert _parse_cross_progress(None) is None

    def test_valid_subkey(self):
        got = _parse_cross_progress(
            '{"cross": {"done": 1, "total": 3, "label": "LLM 语义分析"}}'
        )
        assert got == {"done": 1, "total": 3, "label": "LLM 语义分析"}

    def test_missing_total_returns_none(self):
        assert _parse_cross_progress('{"cross": {"done": 1}}') is None

    def test_non_dict_cross_returns_none(self):
        assert _parse_cross_progress('{"cross": 5}') is None

    def test_invalid_json_returns_none(self):
        assert _parse_cross_progress("oops") is None


class TestParseFailedPages:
    """#132 — `jobs.failed_pages` 是 TEXT 列，但**字段语义是列表**。

    回归守卫：接口曾原样透传该 TEXT，响应里出现 `"failed_pages": "[2, 1]"`
    （装着 JSON 的字符串）。前端 `Array.isArray()` 拿到字符串即静默退化为
    `[]` ⇒ 失败页在任务列表里永不显示，而复核页（自行 json.loads）却正常。
    所以这里断言的是**返回类型**，不只是取值。
    """

    def test_returns_list_not_string(self):
        got = _parse_failed_pages("[2, 1]")
        assert got == [2, 1]
        assert isinstance(got, list), "必须是 list —— 字符串会被前端静默丢弃"

    def test_none_and_empty_are_unknown_not_zero(self):
        """无失败页 → None；**不得**伪造 `[]`（那是在断言"确认零失败页"）。"""
        assert _parse_failed_pages(None) is None
        assert _parse_failed_pages("") is None

    def test_single_page(self):
        assert _parse_failed_pages("[3]") == [3]

    def test_non_list_payload_is_unknown(self):
        assert _parse_failed_pages('{"page": 2}') is None

    def test_invalid_json_is_unknown(self):
        assert _parse_failed_pages("not-valid-json{") is None

    def test_non_numeric_element_is_unknown(self):
        """页码不可解析 → 整体按未知处理（与 _parse_self_heal_progress 同款）。"""
        assert _parse_failed_pages('["x"]') is None


class TestDerivePhase:
    @pytest.mark.parametrize("status,expected", [
        ("ocr_running", "ocr"),
        ("ocr_done", "analyze"),
        ("pending", "idle"),
        ("review", "done"),
        ("partial_review", "done"),
        ("error", "done"),
        ("cancelled", "done"),
    ])
    def test_status_only_mapping(self, status, expected):
        assert _derive_phase(status, 0, 10) == expected

    def test_analyzing_with_cross_started(self):
        assert _derive_phase("analyzing", 2, 10, cross_started=True) == "cross"

    def test_analyzing_heuristic_all_pages_done(self):
        assert _derive_phase("analyzing", 10, 10, cross_started=False) == "cross"

    def test_analyzing_still_in_analyze(self):
        assert _derive_phase("analyzing", 4, 10, cross_started=False) == "analyze"

    def test_analyzing_zero_total_stays_analyze(self):
        assert _derive_phase("analyzing", 0, 0, cross_started=False) == "analyze"
