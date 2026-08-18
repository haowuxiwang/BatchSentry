"""Page analyzer 单元测试 — Stage 2 单页 LLM 分析。

覆盖 core/page_analyzer.py:
- analyze_page 主入口（mock get_llm_client）
- 返回结构：steps / findings / overall_confidence / page_number / _prompt_version
- 错误处理：_parse_error / list 响应 / 空 list
- prompt 构造：v3 系统提示 + 用户提示拼接
"""
import re
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from core.page_analyzer import (
    analyze_page,
    CURRENT_PROMPT_VERSION,
    PROMPTS,
    _grounding_check,
)


def _ok_payload(page=1):
    """构造一份 v3 schema 合规的 LLM 返回字典。

    page: findings[0].page 的页码 — 默认 1 与多数测试的 page_num 一致；
    P1-页码 后校验器强制 findings[].page == page_num，非 1 页的测试
    需传匹配页码避免误触发 C1 修复重试。
    """
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
                "page": page,
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
        mock_client = _make_mock_client(_ok_payload(page=7))
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
        html = ("<table style=\"color:red\" width=\"100\""
                "<tr><td>步骤</td><td>参数</td></tr>"
                "<tr><td>压片</td><td>25.5</td></tr>"
                "<tr><td>包衣</td><td>40.0</td></tr>"
                "<tr><td>灌装</td><td>提取数据</td></tr></table>")
        mock_client = _make_mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            await analyze_page(html, page_num=1)

        call_args = mock_client.chat_json.await_args
        user_prompt = call_args.args[1]

        # 前缀
        assert user_prompt.startswith("提取以下 HTML 表格中的结构化数据：\n\n")
        # P1-页码: prompt 注入物理页码
        assert "这是本批记录的第 1 页（PDF 物理页码，1-indexed）" in user_prompt
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
        assert call_kwargs["timeout"] == 240.0


class TestErrorHandling:
    """LLM 返回异常数据时的错误处理。"""

    async def _always_cancelled(self) -> bool:
        return True

    @pytest.mark.asyncio
    async def test_cancel_check_aborts_before_llm_call(self):
        """P1-2：cancel_check 在调用前返回 True → 抛 AnalysisCancelled，
        不发起 LLM 调用（取消后不再消耗配额）。"""
        from core.page_analyzer import AnalysisCancelled
        mock_client = _make_mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            with pytest.raises(AnalysisCancelled):
                await analyze_page(
                    "<table></table>", page_num=1,
                    cancel_check=self._always_cancelled,  # 已取消
                )
        mock_client.chat_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_check_aborts_after_llm_call(self):
        """P1-2：cancel_check 在首个调用后变为 True → 抛 AnalysisCancelled，
        不进入 schema 修复重试。"""
        from core.page_analyzer import AnalysisCancelled

        calls = {"n": 0}

        async def cancel_after_first():
            calls["n"] += 1
            return calls["n"] > 1  # 第一次返回 False，之后 True

        # 构造 schema 校验失败的结果 → 触发修复重试路径
        payload = {"page_info": {}, "steps": [], "findings": []}
        mock_client = _make_mock_client(payload)
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            with pytest.raises(AnalysisCancelled):
                await analyze_page(
                    "<table></table>", page_num=1,
                    cancel_check=cancel_after_first,
                )
        # 仅一次调用（未进入 schema 修复重试）
        assert mock_client.chat_json.await_count == 1

    @pytest.mark.asyncio
    async def test_finding_page_mismatch_fixed_by_retry(self):
        """P1-页码：LLM 填错 findings[].page（页码漂移）→ 校验失败触发
        C1 修复重试；retry 填对页码 → 采用 retry 结果且无 _schema_warn。"""
        wrong = _ok_payload(page=3)   # 漂移到第 3 页
        fixed = _ok_payload(page=7)   # 修复重试填对

        client = MagicMock()
        client.chat_json = AsyncMock(side_effect=[wrong, fixed])
        with patch("core.page_analyzer.get_llm_client", return_value=client):
            result = await analyze_page("<table></table>", page_num=7)

        assert client.chat_json.await_count == 2
        assert result["findings"][0]["page"] == 7
        assert "_schema_warn" not in result

    @pytest.mark.asyncio
    async def test_finding_page_mismatch_forced_when_retry_fails(self):
        """P1-页码：LLM 两次都填错页码 → 保留结果 + _schema_warn 标记，
        且后端强制修正 findings[].page == page_num（库存数据永不错页）。"""
        wrong = _ok_payload(page=3)
        client = MagicMock()
        client.chat_json = AsyncMock(return_value=wrong)
        with patch("core.page_analyzer.get_llm_client", return_value=client):
            result = await analyze_page("<table></table>", page_num=7)

        assert client.chat_json.await_count == 2  # 原调用 + 1 次修复重试
        assert result["findings"][0]["page"] == 7
        assert "_schema_warn" in result

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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scalar", [None, "plain string", 123, True, 3.14])
    async def test_scalar_json_returns_parse_error(self, scalar):
        """对抗审查 P1-4：_parse_json 对合法 JSON 标量直接放行（null/数字/
        字符串/bool）。此前 dict/list 之外的类型进 _validate_page_result
        抛 TypeError（str 还会在 _sanitize_page_result 的 item assignment
        崩），该页被归失败 — 现在统一转 _parse_error 不炸 pipeline。"""
        mock_client = _make_mock_client(scalar)
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page("<table></table>", page_num=8)

        assert result["_parse_error"] is True
        assert result["overall_confidence"] == "low"
        assert result["page_number"] == 8
        assert "steps" not in result or result["steps"] != "plain string"


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

    def test_truncation_keeps_all_tables_trims_text(self):
        """P1-5 表格优先：两小表 + 巨大正文 → 表格全保留，正文按预算裁剪。"""
        from core.page_analyzer import _clean_html, _MAX_HTML_CHARS
        table1 = "<table><tr><td>批号</td><td>112701</td></tr></table>"
        table2 = "<table><tr><td>含量</td><td>99.2%</td></tr></table>"
        huge_text = "<p>工序说明</p>" * 3000  # ~36K 字符，远超预算
        html = table1 + huge_text + table2
        out = _clean_html(html)
        assert len(out) <= _MAX_HTML_CHARS
        assert table1 in out  # 表1 完整
        assert table2 in out  # 表2 完整（表格优先 — 即使它在正文后面）
        assert "部分正文被裁剪" in out

    def test_truncation_single_huge_table_falls_back_to_plain_alignment(self):
        """P1-5 单表超预算：回退 P2-5 表内安全截断（保留前半数据 + 不切标签）。"""
        from core.page_analyzer import _clean_html, _MAX_HTML_CHARS
        huge_table = "<table><tr><td>" + "x" * (_MAX_HTML_CHARS + 500) + "</td></tr></table>"
        out = _clean_html(huge_table)
        assert len(out) <= _MAX_HTML_CHARS + 80  # 标记少量溢出容忍
        # 不允许出现切开标签的残片（截断点后不应残留孤立的 <tr/></tr> 片段）
        assert not re.search(r"<tr[^>]*$", out)

    def test_truncation_skips_later_table_when_budget_exhausted(self):
        """P1-5 多表超预算：按序保留放得下的表，超预算的表整表跳过 + 标记。"""
        from core.page_analyzer import _clean_html, _MAX_HTML_CHARS
        row = "<tr><td>行数据</td></tr>"
        t1 = "<table>" + row * 200 + "</table>"  # ~4K
        t2 = "<table>" + row * 200 + "</table>"  # ~4K（t1+t2 ≈ 8K < 预算）
        t3 = "<table>" + row * 300 + "</table>"  # ~6K — 加上后超预算
        out = _clean_html(t1 + t2 + t3)
        assert t1 in out  # 前表保留
        assert t2 in out  # 中表保留
        assert t3 not in out  # 后表整表跳过（不切开）
        assert "跳过" in out
        assert len(out) <= _MAX_HTML_CHARS

    def test_truncation_matches_table_with_attributes(self):
        """B3 修复（对抗性审查）：带属性表格（<table border=...>）不再落入
        非对齐截断 — 正则放宽为 <table[\s>]，带属性表也按表格优先策略处理。"""
        from core.page_analyzer import _clean_html, _MAX_HTML_CHARS
        html = '<table border="1" cellspacing="0"><tr><td>关键数据</td></tr></table>'
        out = _clean_html(html)
        assert "关键数据" in out  # 表格内容完整保留
        assert out.count("<tr") == 1  # 行未被切开

    def test_truncation_plain_closes_open_table(self):
        """B3 修复：单表超预算走表内对齐截断时，补齐 </table> 闭合标签 —
        LLM 收到结构合法 HTML（此前截断点是未闭合的 <table> 开头）。"""
        from core.page_analyzer import _truncate_plain, _MAX_HTML_CHARS
        huge = "<table><tr><td>" + "x" * (_MAX_HTML_CHARS + 300) + "</td></tr></table>"
        out = _truncate_plain(huge, len(huge))
        assert "</table>\n[HTML 已截断" in out  # 闭合标签在截断标记前补齐
        assert "<tr" in out  # 前半数据保留

    def test_truncation_plain_no_extra_close_for_complete_table(self):
        """B3 回归防护：已完整闭合的表不得被追加多余 </table>。

        防子串陷阱：str.count("<table") 会把 </table> 里的 "<table"
        子串也算进去，导致完整闭合的表被误判为未闭合、多补闭合标签。
        断言闭合计数守恒。
        """
        from core.page_analyzer import _truncate_plain, _MAX_HTML_CHARS
        t1 = "<table><tr><td>" + "a" * 2000 + "</td></tr></table>"
        t2 = "<table><tr><td>" + "b" * 2000 + "</td></tr></table>"
        body = "<p>正文</p>" * 5000  # 远超预算 → 表在截断点前均完整闭合
        out = _truncate_plain(t1 + t2 + body, len(t1 + t2 + body))
        open_tables = len(re.findall(r"<table[\s>]", out))
        close_tables = out.count("</table>")
        assert open_tables == close_tables == 2  # 两表完整，无多余闭合


class TestEmptyPageShortCircuit:
    """robustness-D1: 空/无内容页短路 — 不调 LLM，返回 _ocr_empty 标记。"""

    @pytest.mark.asyncio
    async def test_mineru_empty_marker_skips_llm(self):
        """MinerU 空块页标记（此页无文本内容）→ 短路，不调 LLM。"""
        mock_client = _make_mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page("## 第 3 页\n\n（此页无文本内容）", page_num=3)

        assert result["_ocr_empty"] is True
        assert result["_parse_error"] is False
        assert result["steps"] == []
        assert result["findings"] == []
        assert result["overall_confidence"] == "low"
        mock_client.chat_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_blank_text_skips_llm(self):
        """Paddle 空串/纯空白 → 短路，不调 LLM。"""
        mock_client = _make_mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page("", page_num=5)

        assert result["_ocr_empty"] is True
        mock_client.chat_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_page_still_calls_llm(self):
        """正常内容页不受影响，照常调 LLM。"""
        mock_client = _make_mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page("<table><tr><td>浓度 25.5</td></tr></table>", page_num=1)

        assert "_ocr_empty" not in result or result["_ocr_empty"] is not True
        mock_client.chat_json.assert_awaited_once()


class TestSparsePageShortCircuit:
    """robustness-E1: 稀疏内容页（无表格且文本极少）→ 仍调 LLM 但注入
    保守警告 + 返回 _ocr_sparse 标记。"""

    @pytest.mark.asyncio
    async def test_sparse_text_gets_warning_and_marker(self):
        """短文本无表格 → prompt 含系统警告 + _ocr_sparse 标记。"""
        mock_client = _make_mock_client(_ok_payload(page=4))
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page("日期：2024-01-01", page_num=4)

        assert result["_ocr_sparse"] is True
        mock_client.chat_json.assert_awaited_once()
        prompt_arg = mock_client.chat_json.call_args.args[1]
        assert "系统警告" in prompt_arg
        assert "overall_confidence 设为 low" in prompt_arg

    @pytest.mark.asyncio
    async def test_short_table_with_few_rows_marked_sparse(self):
        """含表格但仅 1-2 行且文本极少 → 表格可能残缺，标记稀疏。"""
        html = "<table><tr><td>批次</td></tr></table>"
        mock_client = _make_mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page(html, page_num=2)

        assert result.get("_ocr_sparse") is True
        prompt_arg = mock_client.chat_json.call_args.args[1]
        assert "系统警告" in prompt_arg

    @pytest.mark.asyncio
    async def test_table_page_with_rows_not_marked_sparse(self):
        """含表格且行数正常（≥3）→ 不判定为稀疏。"""
        rows = "".join(f"<tr><td>第{i}行 {i}</td></tr>" for i in range(1, 6))
        html = f"<table>{rows}</table>"
        mock_client = _make_mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page(html, page_num=2)

        assert result.get("_ocr_sparse") is not True

    @pytest.mark.asyncio
    async def test_long_text_page_not_marked_sparse(self):
        """长文本（无表格）不判定为稀疏。"""
        long_text = "工序记录" * 60  # 300 chars > threshold
        mock_client = _make_mock_client(_ok_payload(page=7))
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page(long_text, page_num=7)

        assert result.get("_ocr_sparse") is not True


class TestOcrWarningSeparation:
    """B1 修复（Round 3）：pipeline 注入的 `[OCR 警告: ...]` 前缀必须从
    <PBC_UNTRUSTED_OCR> fenced 数据区剥离，转为 system 区显式警告 —
    否则 LLM 把降级提示当作 OCR 数据的一部分（可被忽略）。"""

    @pytest.mark.asyncio
    async def test_warning_moved_out_of_data_zone(self):
        """警告前缀从 prompt 数据区移除，转为 [系统警告]，result 带标记。"""
        rows = "".join(f"<tr><td>第{i}行 {i}</td></tr>" for i in range(1, 6))
        html = (
            "[OCR 警告: 本页有 3 个内容块因置信度过低被 OCR 丢弃, "
            "以下内容可能不完整, 分析仅供参考]\n\n"
            f"<table>{rows}</table>"
        )
        mock_client = _make_mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page(html, page_num=4)

        prompt_arg = mock_client.chat_json.call_args.args[1]
        # 数据区不再含警告前缀（fence 内干净）
        assert "[OCR 警告:" not in prompt_arg.split("<PBC_UNTRUSTED_OCR>")[1]
        # 系统警告区含该内容
        assert "[系统警告] OCR 后端报告本页存在内容缺失" in prompt_arg
        assert "3 个内容块" in prompt_arg
        # 结果标记透出（review 横幅用）
        assert result["_ocr_warning"] == (
            "本页有 3 个内容块因置信度过低被 OCR 丢弃, 以下内容可能不完整, "
            "分析仅供参考"
        )
        # 表格内容仍完整送入 LLM
        assert "第1行" in prompt_arg

    @pytest.mark.asyncio
    async def test_no_warning_prefix_untouched(self):
        """无警告前缀的普通页：prompt 不含 OCR 警告段，无 _ocr_warning。"""
        rows = "".join(f"<tr><td>第{i}行 {i}</td></tr>" for i in range(1, 6))
        mock_client = _make_mock_client(_ok_payload())
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page(f"<table>{rows}</table>", page_num=2)

        prompt_arg = mock_client.chat_json.call_args.args[1]
        assert "OCR 后端报告本页存在内容缺失" not in prompt_arg
        assert result.get("_ocr_warning") is None


class TestSanitizePageResult:
    """P1-2: _sanitize_page_result 深层类型消毒 — LLM 类型污染输出不崩 Stage 3。"""

    def _polluted(self):
        return {
            "page_info": {"title": "污染页"},
            "steps": [
                "not-a-dict-step",
                {
                    "step_no": 1,
                    "parameters": ["温度", {"name": "pH", "spec_range": "5-9", "value": "7"}],
                    "measurements": [
                        {"time": "10:00", "values": {"温度": {"actual": "25"}, "压力": "非dict"}},
                        "garbage-measurement",
                    ],
                    "signatures": ["签名串", {"role": "operator", "name": "张三", "sign_time": "10:30"}],
                },
            ],
            "event_year_groups": {
                "draft": ["2022年", 2021, "not-a-year"],
                "production": "not-a-list",
            },
            "findings": ["bad-finding", {"type": "x", "severity": "warning", "description": "ok"}],
        }

    def test_non_dict_steps_parameters_measurements_signatures_removed(self):
        from core.page_analyzer import _sanitize_page_result

        data = self._polluted()
        _sanitize_page_result(data)

        assert data["steps"] == [{"step_no": 1, "parameters": [{"name": "pH", "spec_range": "5-9", "value": "7"}], "measurements": [{"time": "10:00", "values": {"温度": {"actual": "25"}}}], "signatures": [{"role": "operator", "name": "张三", "sign_time": "10:30"}]}]
        assert len(data["findings"]) == 1

    def test_year_groups_non_numeric_dropped(self):
        from core.page_analyzer import _sanitize_page_result

        data = self._polluted()
        _sanitize_page_result(data)

        assert data["event_year_groups"] == {"draft": [2021], "production": []}

    def test_values_dict_non_dict_cells_removed(self):
        from core.page_analyzer import _sanitize_page_result

        data = self._polluted()
        _sanitize_page_result(data)

        step = data["steps"][0]
        assert step["measurements"][0]["values"] == {"温度": {"actual": "25"}}

    def test_steps_not_list_becomes_empty(self):
        from core.page_analyzer import _sanitize_page_result

        data = {"page_info": {"title": "x"}, "steps": "oops"}
        _sanitize_page_result(data)
        assert data["steps"] == []

    def test_scalar_pollution_coerced_not_crash(self):
        """标量污染: page_info 非 dict / overall_confidence 非 str / step 标量
        非 str → fail-closed 转义，规则层 .get()/.lower()/[:40] 不再抛异常。"""
        from core.page_analyzer import _sanitize_page_result

        data = {
            "page_info": "摘要页",
            "overall_confidence": 5,
            "steps": [
                {
                    "step_no": 1,
                    "operation": 123,
                    "operator": None,
                    "reviewer": True,
                    "start_time": 100,
                    "end_time": 0,
                    "signatures": [{"role": 7, "name": None}],
                }
            ],
        }
        _sanitize_page_result(data)

        assert data["page_info"] == {}
        assert data["overall_confidence"] == "5"
        step = data["steps"][0]
        assert step["operation"] == "123"
        assert step["operator"] is None
        assert step["reviewer"] == "True"
        assert step["start_time"] == "100"
        assert step["end_time"] == "0"
        sig = step["signatures"][0]
        assert sig["role"] == "7"
        assert sig["name"] is None

    @pytest.mark.asyncio
    async def test_analyze_page_sanitizes_polluted_llm_output(self):
        """analyze_page 返回的已是消毒后数据（page_cache 只存干净结构）。"""

        polluted = self._polluted()
        polluted["page_number"] = 1
        mock_client = MagicMock()
        mock_client.chat_json = AsyncMock(return_value=polluted)
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page("<table><tr><td>X</td></tr></table>", page_num=1)

        assert result["steps"] == [{"step_no": 1, "parameters": [{"name": "pH", "spec_range": "5-9", "value": "7"}], "measurements": [{"time": "10:00", "values": {"温度": {"actual": "25"}}}], "signatures": [{"role": "operator", "name": "张三", "sign_time": "10:30"}]}]
        assert result["event_year_groups"] == {"draft": [2021], "production": []}
        assert len(result["findings"]) == 1


class TestGroundingCheck:
    """幻觉防护：LLM 提取数值必须能在 OCR 原文中找到（零 LLM 成本）。"""

    def _data(self, actual="0.974", param_value="12.50"):
        params = [] if param_value is None else [{"name": "温度", "value": param_value}]
        return {
            "page_info": {"title": "x"},
            "steps": [{
                "step_no": 1,
                "measurements": [{"time": "11:04", "values": {
                    "设备A_流速": {"actual": actual, "spec": "0.5-1.0", "unit": "m³/h"},
                }}],
                "parameters": params,
            }],
            "findings": [],
        }

    def test_grounded_values_pass(self):
        """原文包含全部数值 → 无警告。"""
        html = "<table><tr><td>流速 0.974 m³/h</td><td>温度 12.50 ℃</td></tr></table>"
        assert _grounding_check(html, self._data()) == []

    def test_hallucinated_value_flagged(self):
        """actual 在原文中找不到 → 可疑项列入警告。"""
        html = "<table><tr><td>流速 0.974 m³/h</td><td>温度 12.50 ℃</td></tr></table>"
        suspects = _grounding_check(html, self._data(actual="0.988"))
        assert len(suspects) == 1
        assert "0.988" in suspects[0]
        assert "流速" in suspects[0]

    def test_parameter_value_flagged(self):
        """参数 value 找不到 → 警告（上下文含参数名）。"""
        html = "<table><tr><td>流速 0.974 m³/h</td><td>温度 12.50 ℃</td></tr></table>"
        suspects = _grounding_check(html, self._data(param_value="99.99"))
        assert len(suspects) == 1
        assert "99.99" in suspects[0]
        assert "温度" in suspects[0]

    def test_short_number_inside_longer_not_grounded(self):
        """短数字（2 位）嵌在长数字内（"25" ⊂ "250.0"）→ 边界检查拒绝：
        文本中不存在独立的 "25"，应判未 grounded（严格边界防误命中）。"""
        html = "<table><tr><td>250.0</td><td>温度 12.50 ℃</td></tr></table>"
        data = self._data()
        data["steps"][0]["measurements"][0]["values"]["设备A_流速"]["actual"] = "25"
        suspects = _grounding_check(html, data)
        assert len(suspects) == 1
        assert "25" in suspects[0]

    def test_short_number_with_boundary_matches(self):
        """短数字带边界（逗号/空格分隔）仍可命中。"""
        html = "<table><tr><td>流速 25, 30</td><td>温度 12.50 ℃</td></tr></table>"
        data = self._data()
        data["steps"][0]["measurements"][0]["values"]["设备A_流速"]["actual"] = "25"
        assert _grounding_check(html, data) == []

    def test_unit_suffix_tolerated(self):
        """值带单位（"25.5℃"）→ 数字分量 25.5 命中即通过。"""
        html = "<table><tr><td>25.5℃</td><td>温度 12.50 ℃</td></tr></table>"
        data = self._data(actual="25.5℃")
        assert _grounding_check(html, data) == []

    def test_empty_html_skipped(self):
        """原文过短（空页）→ 不做核对（避免噪声）。"""
        assert _grounding_check("", self._data()) == []
        assert _grounding_check("<table></table>", self._data()) == []

    def test_sanitized_type_pollution_tolerated(self):
        """非 dict 污染元素（steps 里的乱值）不炸检查器。"""
        data = {"steps": ["污染", None], "findings": []}
        assert _grounding_check("<table><tr><td>0.974</td></tr></table>", data) == []

    @pytest.mark.asyncio
    async def test_analyze_page_sets_grounding_warn(self):
        """analyze_page 端到端：数值找不到 → _grounding_warn 随结果透出。"""
        html = "<table><tr><td>流速 0.974 m³/h</td></tr></table>"
        payload = _ok_payload()
        payload["steps"] = [{
            "step_no": 1,
            "measurements": [{"time": "11:04", "values": {
                "设备A_流速": {"actual": "9.999", "spec": "0.5-1.0", "unit": "m³/h"},
            }}],
            "parameters": [],
        }]
        mock_client = _make_mock_client(payload)
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page(html, page_num=1)

        assert "_grounding_warn" in result
        assert "9.999" in result["_grounding_warn"][0]

    @pytest.mark.asyncio
    async def test_analyze_page_no_warn_when_grounded(self):
        """数值都能找到 → 无 _grounding_warn 键（干净页不打扰复核者）。"""
        html = "<table><tr><td>流速 0.974 m³/h</td></tr></table>"
        payload = _ok_payload()
        payload["steps"] = [{
            "step_no": 1,
            "measurements": [{"time": "11:04", "values": {
                "设备A_流速": {"actual": "0.974", "spec": "0.5-1.0", "unit": "m³/h"},
            }}],
            "parameters": [],
        }]
        mock_client = _make_mock_client(payload)
        with patch("core.page_analyzer.get_llm_client", return_value=mock_client):
            result = await analyze_page(html, page_num=1)

        assert "_grounding_warn" not in result
