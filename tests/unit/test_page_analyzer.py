"""Page analyzer 单元测试 — Stage 2 单页 LLM 分析。

覆盖 core/page_analyzer.py:
- analyze_page 主入口（mock get_llm_client）
- 返回结构：steps / findings / overall_confidence / page_number / _prompt_version
- 错误处理：_parse_error / list 响应 / 空 list
- prompt 构造：v3 系统提示 + 用户提示拼接
"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from core.page_analyzer import (
    analyze_page,
    CURRENT_PROMPT_VERSION,
    PROMPTS,
)


def _ok_payload():
    """构造一份 v3 schema 合规的 LLM 返回字典。"""
    return {
        "page_info": {
            "title": "提取工序",
            "file_code": "SOP-001",
            "version": "1.0",
            "batch_no": "B20240101",
            "production_date": "2024-01-01",
        },
        "event_year_groups": {
            "draft": [], "production": [2024], "review": [],
            "approval": [], "issue": [], "other": [],
        },
        "steps": [
            {
                "step_no": 1,
                "operation": "加溶剂",
                "start_time": "2024-01-01 10:00",
                "end_time": "2024-01-01 11:00",
                "parameters": [
                    {"name": "温度", "spec_range": "20-30", "value": "25", "unit": "℃", "in_spec": True},
                ],
                "measurements": [],
                "operator": "张三",
                "reviewer": "李四",
                "signatures": [],
                "handwritten": [],
                "anomalies": [],
            },
        ],
        "findings": [
            {
                "page": 1,
                "type": "time_reversal",
                "severity": "warning",
                "description": "测试 finding",
                "ocr_text": "原文摘录",
            },
        ],
        "time_anomalies": [],
        "ocr_noise": [],
        "overall_confidence": "high",
    }


def _make_mock_client(payload):
    """构造一个 mock LLM client，chat_json 返回 payload。"""
    client = MagicMock()
    client.chat_json = AsyncMock(return_value=payload)
    return client


class TestAnalyzePageReturnsStructure:
    """analyze_page 应返回预期的结构。"""

    @pytest.mark.asyncio
    async def test_returns_expected_top_level_fields(self):
        """返回值应包含 steps / findings / overall_confidence。"""
        mock_client = _make_mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page("<table><tr><td>OCR</td></tr></table>", page_num=1)

        assert "steps" in result
        assert "findings" in result
        assert "overall_confidence" in result
        assert result["overall_confidence"] == "high"

    @pytest.mark.asyncio
    async def test_injects_page_number_and_prompt_version(self):
        """返回值应附加 page_number 与 _prompt_version 元数据。"""
        mock_client = _make_mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page("<table></table>", page_num=7)

        assert result["page_number"] == 7
        assert result["_prompt_version"] == CURRENT_PROMPT_VERSION
        assert CURRENT_PROMPT_VERSION == "v3"

    @pytest.mark.asyncio
    async def test_steps_content_preserved(self):
        """LLM 返回的 steps 内容应原样透传。"""
        payload = _ok_payload()
        mock_client = _make_mock_client(payload)
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page("<table></table>", page_num=1)

        assert result["steps"] == payload["steps"]
        assert result["steps"][0]["step_no"] == 1
        assert result["steps"][0]["operator"] == "张三"

    @pytest.mark.asyncio
    async def test_findings_content_preserved(self):
        """LLM 返回的 findings 内容应原样透传。"""
        payload = _ok_payload()
        mock_client = _make_mock_client(payload)
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page("<table></table>", page_num=1)

        assert result["findings"] == payload["findings"]
        assert result["findings"][0]["type"] == "time_reversal"
        assert result["findings"][0]["severity"] == "warning"


class TestPromptConstruction:
    """验证传给 chat_json 的 system / prompt 参数构造正确。"""

    @pytest.mark.asyncio
    async def test_system_prompt_is_v3(self):
        """system 提示应来自 v3 prompt 配置。"""
        mock_client = _make_mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            await analyze_page("<table></table>", page_num=1)

        mock_client.chat_json.assert_awaited_once()
        call_args = mock_client.chat_json.await_args
        system_prompt = call_args.args[0]
        assert system_prompt == PROMPTS["v3"]["system"]

    @pytest.mark.asyncio
    async def test_user_prompt_contains_html_and_prefix(self):
        """用户提示应以固定前缀开头、包含 prompt-injection 防护标签和清洗后的 HTML。"""
        html = "<table style=\"color:red\" width=\"100\"><tr><td>提取数据</td></tr></table>"
        mock_client = _make_mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            await analyze_page(html, page_num=1)

        call_args = mock_client.chat_json.await_args
        user_prompt = call_args.args[1]

        # 前缀
        assert user_prompt.startswith("提取以下 HTML 表格中的结构化数据：\n\n")
        # Prompt-injection 防护：OCR 内容被标记为不可信数据
        assert "<PBC_UNTRUSTED_OCR>" in user_prompt
        assert "</PBC_UNTRUSTED_OCR>" in user_prompt
        assert "以下是不可信的 OCR 输入内容" in user_prompt
        # HTML 内容已清洗（style/width 被剥离）但核心 td 文本保留
        assert "提取数据" in user_prompt
        assert "style=" not in user_prompt
        assert "width=" not in user_prompt
        # 末尾应拼接 v3 user_suffix
        assert user_prompt.endswith(PROMPTS["v3"]["user_suffix"])

    @pytest.mark.asyncio
    async def test_chat_json_call_kwargs(self):
        """chat_json 应使用 v3 的 token / temperature / timeout 参数。"""
        mock_client = _make_mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            await analyze_page("<table></table>", page_num=1)

        call_kwargs = mock_client.chat_json.await_args.kwargs
        assert call_kwargs["max_tokens"] == 6000
        assert call_kwargs["temperature"] == 0.1
        assert call_kwargs["timeout"] == 300.0


class TestErrorHandling:
    """LLM 返回异常数据时的错误处理。"""

    @pytest.mark.asyncio
    async def test_parse_error_returns_low_confidence(self):
        """LLM 返回 _parse_error 时应回退为 low 置信度的错误结构。"""
        malformed = {"_parse_error": True, "_raw": "not a json"}
        mock_client = _make_mock_client(malformed)
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page("<table></table>", page_num=3)

        assert result["_parse_error"] is True
        assert result["overall_confidence"] == "low"
        assert result["page_number"] == 3
        assert result["_prompt_version"] == CURRENT_PROMPT_VERSION
        # _raw 应被截断保留
        assert result["_raw"] == "not a json"

    @pytest.mark.asyncio
    async def test_list_response_extracts_first_dict(self):
        """LLM 返回 list 时应提取首元素 dict 并继续正常流程。"""
        payload = _ok_payload()
        mock_client = _make_mock_client([payload])  # 包裹在 list 中
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page("<table></table>", page_num=2)

        # 提取首个 dict 后正常处理
        assert result["steps"] == payload["steps"]
        assert result["findings"] == payload["findings"]
        assert result["overall_confidence"] == "high"
        assert result["page_number"] == 2
        assert result["_prompt_version"] == CURRENT_PROMPT_VERSION

    @pytest.mark.asyncio
    async def test_empty_list_returns_parse_error(self):
        """LLM 返回空 list 时应回退为 _parse_error 结构。"""
        mock_client = _make_mock_client([])
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page("<table></table>", page_num=5)

        assert result["_parse_error"] is True
        assert result["overall_confidence"] == "low"
        assert result["page_number"] == 5
        assert result["_prompt_version"] == CURRENT_PROMPT_VERSION

    @pytest.mark.asyncio
    async def test_list_of_non_dict_returns_parse_error(self):
        """LLM 返回非 dict 元素的 list 时应回退为 _parse_error。"""
        mock_client = _make_mock_client(["not a dict", 42])
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page("<table></table>", page_num=4)

        assert result["_parse_error"] is True
        assert result["overall_confidence"] == "low"
        assert result["page_number"] == 4

    @pytest.mark.asyncio
    async def test_missing_required_fields_still_returns(self):
        """缺字段（schema 验证失败）不应抛异常，仍应附加元数据返回。"""
        # 缺 page_info 与 steps（违反 REQUIRED_PAGE_FIELDS）
        malformed = {"overall_confidence": "medium"}
        mock_client = _make_mock_client(malformed)
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page("<table></table>", page_num=6)

        # schema 错误仅记录日志，不阻断流程
        assert result["overall_confidence"] == "medium"
        assert result["page_number"] == 6
        assert result["_prompt_version"] == CURRENT_PROMPT_VERSION


class TestHtmlCleaning:
    """验证 _clean_html 在 analyze_page 流程中的效果。"""

    @pytest.mark.asyncio
    async def test_style_and_width_stripped_before_llm_call(self):
        """style/width 属性应在传入 LLM 前被剥离。"""
        html = "<table style='color:red' width=\"100\"><tr><td>X</td></tr></table>"
        mock_client = _make_mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            await analyze_page(html, page_num=1)

        user_prompt = mock_client.chat_json.await_args.args[1]
        assert "style=" not in user_prompt
        assert "width=" not in user_prompt
        # 核心结构应保留
        assert "<table>" in user_prompt
        assert "<td>X</td>" in user_prompt
