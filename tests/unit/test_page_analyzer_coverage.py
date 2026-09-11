"""core/page_analyzer.py 边界分支补测（M1b）。

补全 test_page_analyzer.py 未覆盖的：
- _validate_page_result 各类型/缺字段错误分支
- _sanitize_page_result 的 fail-closed 清洗分支
- analyze_page：剥离 OCR 警告后二次空页短路 / KB 注入失败旁路 /
  截断恢复标记 / schema 修复重试前取消
- _grounding_check 的畸形结构跳过分支
- _value_grounded 无数字 / 旧子串语义
- _truncate_tables_first / _truncate_plain 的截断分支
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.page_analyzer import (
    AnalysisCancelled,
    _clean_html,
    _grounding_check,
    _sanitize_page_result,
    _truncate_plain,
    _truncate_tables_first,
    _validate_page_result,
    _value_grounded,
    analyze_page,
)


def _ok_payload(page=1, **extra):
    payload = {
        "page_info": {"title": "提取工序", "batch_no": "B20240101"},
        "event_year_groups": {"production": [2024]},
        "steps": [{
            "step_no": 1,
            "operation": "加溶剂",
            "parameters": [{"name": "温度", "value": "25", "unit": "℃"}],
            "measurements": [],
            "signatures": [],
        }],
        "findings": [{
            "page": page,
            "type": "time_reversal",
            "severity": "warning",
            "description": "测试 finding",
            "ocr_text": "原文摘录",
        }],
        "overall_confidence": "high",
    }
    payload.update(extra)
    return payload


def _mock_client(payload):
    c = MagicMock()
    c.chat_json = AsyncMock(return_value=payload)
    return c


_VALID_HTML = "<table><tr><td>" + ("批号112701含量99.2%设备编码EQ01操作人张三" * 2) + "</td></tr></table>"


# ── _validate_page_result ───────────────────────────────────────────────────


class TestValidatePageResultBranches:
    def test_page_info_missing_title(self):
        errs = _validate_page_result({"page_info": {}, "steps": []})
        assert any("page_info missing: title" in e for e in errs)

    def test_steps_not_list_returns_early(self):
        errs = _validate_page_result({"page_info": {"title": "t"}, "steps": "nope"})
        assert "steps is not a list" in errs

    def test_event_year_groups_not_object(self):
        errs = _validate_page_result(
            {"page_info": {"title": "t"}, "steps": [], "event_year_groups": "x"}
        )
        assert "event_year_groups must be an object" in errs

    def test_findings_not_array(self):
        errs = _validate_page_result(
            {"page_info": {"title": "t"}, "steps": [], "findings": "x"}
        )
        assert "findings must be an array" in errs

    def test_step_measurements_not_array(self):
        errs = _validate_page_result(
            {"page_info": {"title": "t"}, "steps": [{"measurements": "x"}]}
        )
        assert any("measurements must be an array" in e for e in errs)

    def test_step_signatures_not_array(self):
        errs = _validate_page_result(
            {"page_info": {"title": "t"}, "steps": [{"signatures": "x"}]}
        )
        assert any("signatures must be an array" in e for e in errs)

    def test_step_checks_not_array(self):
        errs = _validate_page_result(
            {"page_info": {"title": "t"}, "steps": [{"checks": "x"}]}
        )
        assert any("checks must be an array" in e for e in errs)


# ── _sanitize_page_result ───────────────────────────────────────────────────


class TestSanitizePageResultBranches:
    def test_fail_closed_cleaning(self):
        data = {
            "steps": [
                # 非 list 的四个字段 → 归零（覆盖 314）
                {"parameters": "x", "measurements": "x", "signatures": "x", "checks": "x"},
                # 合法 list，内部类型污染 → 逐字段清洗
                {
                    "parameters": [{"name": 123, "value": 5}],          # → str
                    "measurements": [
                        {"values": None},                                # None → skip
                        {"values": "notadict"},                          # 非 dict → {}
                        {"values": {"c": {"actual": 7}}},                # 内层 → str
                    ],
                    "checks": [{"item": 9}],                             # → str
                    "signatures": [{"role": 3, "name": 4}],              # → str
                },
            ],
            "event_year_groups": {"2022": [True, "2023"]},               # bool 跳过 / str 保留
            "findings": "notalist",                                       # → []
        }
        out = _sanitize_page_result(data)
        assert out["steps"][0]["parameters"] == []
        assert out["steps"][1]["parameters"][0]["name"] == "123"
        assert out["steps"][1]["measurements"][0]["values"] is None
        assert out["steps"][1]["measurements"][1]["values"] == {}
        assert out["steps"][1]["measurements"][2]["values"]["c"]["actual"] == "7"
        assert out["steps"][1]["checks"][0]["item"] == "9"
        assert out["steps"][1]["signatures"][0]["role"] == "3"
        assert out["event_year_groups"] == {"2022": ["2023"]}
        assert out["findings"] == []

    def test_event_year_groups_not_dict(self):
        out = _sanitize_page_result({"steps": [], "event_year_groups": "x"})
        assert out["event_year_groups"] == {}


# ── analyze_page ────────────────────────────────────────────────────────────


class TestAnalyzePageBranches:
    @pytest.mark.asyncio
    async def test_ocr_warning_only_page_short_circuits(self):
        """剥离 OCR 警告前缀后为空 → 二次空页短路，不调 LLM（覆盖 440-456）。"""
        client = _mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=client):
            result = await analyze_page("[OCR 警告: 部分块被丢弃]", page_num=3)
        assert result["_ocr_empty"] is True
        assert result["_ocr_warning"] == "部分块被丢弃"
        client.chat_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_kb_context_failure_is_non_fatal(self):
        """KB 检索异常 → 仅告警，分析照常（覆盖 498-500 与 569-571 两处旁路）。"""
        client = _mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=client), \
             patch("core.kb.retriever.build_page_kb_context",
                   side_effect=RuntimeError("kb down")):
            result = await analyze_page(_VALID_HTML, page_num=1)
        assert result["page_number"] == 1
        assert result["overall_confidence"] == "high"

    @pytest.mark.asyncio
    async def test_truncated_recovered_sets_warn(self):
        """LLM 截断恢复标记 → 结果加 _truncated_warn（覆盖 644-648）。"""
        client = _mock_client(_ok_payload(page=1, _truncated_recovered=True))
        with patch("core.page_analyzer.get_llm_client", return_value=client):
            result = await analyze_page(_VALID_HTML, page_num=1)
        assert result.get("_truncated_warn") is True

    @pytest.mark.asyncio
    async def test_cancel_before_schema_fix_retry(self):
        """schema 校验失败后、发起修复重试前取消 → AnalysisCancelled（覆盖 699-700）。"""
        bad = {"page_info": {}, "steps": []}  # 缺 title → 触发修复分支
        client = _mock_client(bad)
        cancel = AsyncMock(side_effect=[False, False, True])
        with patch("core.page_analyzer.get_llm_client", return_value=client):
            with pytest.raises(AnalysisCancelled):
                await analyze_page(_VALID_HTML, page_num=1, cancel_check=cancel)
        assert cancel.await_count == 3


# ── _grounding_check ────────────────────────────────────────────────────────


class TestGroundingCheckBranches:
    def test_steps_not_list_returns_empty(self):
        assert _grounding_check("<table>1234</table>", {"steps": "x"}) == []

    def test_malformed_nested_structures_skipped(self):
        """各类非 dict 子结构一律跳过，不崩溃、不误报（覆盖 810,813,816,819,824,827）。"""
        data = {"steps": [
            {"measurements": ["notdict"]},                             # 810
            {"measurements": [{"values": "notdict"}]},                 # 813
            {"measurements": [{"values": {"c": "notdict"}}]},          # 816
            {"measurements": [{"values": {"c": {"actual": ""}}}]},     # 819
            {"parameters": ["notdict"]},                               # 824
            {"parameters": [{"value": ""}]},                           # 827
        ]}
        assert _grounding_check("<table>1234</table>", data) == []


class TestValueGroundedBranches:
    def test_non_numeric_value_passes(self):
        assert _value_grounded("任意文本", "无数字") is True

    def test_legacy_substring_semantics(self):
        """tokens 未提供时用旧子串语义（覆盖 897-898）。"""
        assert _value_grounded("abc 12345 def", "12345") is True


# ── _truncate_* ─────────────────────────────────────────────────────────────


class TestTruncateBranches:
    def test_tables_first_no_tables_delegates(self):
        """无 <table> → 委派 _truncate_plain（覆盖 967）。"""
        out = _truncate_tables_first("x" * 20000, 20000)
        assert "HTML 已截断" in out

    def test_tables_first_trailing_text_segment(self):
        """最后一个 </table> 之后仍有尾部文本 → 追加文本段（覆盖 978）。"""
        out = _truncate_tables_first(
            "<table><tr><td>数据1127</td></tr></table>尾部文本", 100
        )
        assert "尾部文本" in out

    def test_tables_first_table_over_budget_skips_text(self):
        """单超大表占满预算 → text_budget=0 → 文本段被裁剪标记（覆盖 999-1000）。"""
        big = "<table><tr><td>" + ("A" * 20000) + "</td></tr></table>"
        out = _truncate_tables_first("正文" + big, 20002)
        assert "HTML 已截断" in out

    def test_tables_first_keeps_text_when_budget_remains(self):
        """小表 + 文本，文本在剩余预算内保留（覆盖 1002-1003）。"""
        out = _truncate_tables_first("正文内容" + "<table><tr><td>1</td></tr></table>", 100)
        assert "正文内容" in out

    def test_truncate_plain_close_table_boundary(self):
        """截断点前存在 >60% 处的 </table> → 对齐到表尾（覆盖 1028）。"""
        html = ("x" * 8000) + "</table>" + ("y" * 5000)
        out = _truncate_plain(html, len(html))
        assert out.rstrip().endswith("本页信息可能不完整]")

    def test_truncate_plain_line_boundary(self):
        """无 </table> 但有 >60% 处的 <tr → 对齐到行边界（覆盖 1037）。"""
        html = ("x" * 7500) + "<tr>" + ("y" * 5000)
        out = _truncate_plain(html, len(html))
        assert out.rstrip().endswith("本页信息可能不完整]")


def test_clean_html_small_input_not_truncated():
    cleaned, truncated = _clean_html("<table><tr><td>ok</td></tr></table>")
    assert truncated is False
    assert "ok" in cleaned
