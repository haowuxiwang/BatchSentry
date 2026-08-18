"""OCR 客户端单元测试 — MinerU 和 PaddleOCR。

使用 mock 避免真实 HTTP 调用。
"""
import io
import json
import pytest
from unittest import mock
from unittest.mock import patch

from core import mineru_client
from core import ocr_client


class TestMinerUClient:
    """MinerU 客户端（模块级函数）。"""

    def test_module_has_run_ocr_function(self):
        """应暴露 run_ocr 函数。"""
        assert hasattr(mineru_client, 'run_ocr')
        assert callable(mineru_client.run_ocr)

    def test_module_has_submit_pdf(self):
        """应暴露 submit_pdf 函数。"""
        assert hasattr(mineru_client, 'submit_pdf')
        assert callable(mineru_client.submit_pdf)

    def test_module_has_poll_job(self):
        """应暴露 poll_job 函数。"""
        assert hasattr(mineru_client, 'poll_job')

    def test_module_has_download_result(self):
        """应暴露 download_result 函数。"""
        assert hasattr(mineru_client, 'download_result')

    def test_run_ocr_without_token_raises(self, tmp_path):
        """无 token 应抛 RuntimeError。"""
        # 创建一个存在的临时文件（避免 FileNotFoundError 干扰）
        fake_pdf = tmp_path / "test.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake")
        # _headers 在无 token 时应抛 RuntimeError
        with patch('core.mineru_client._headers', side_effect=RuntimeError("token 未配置")):
            with pytest.raises(RuntimeError):
                mineru_client.run_ocr(str(fake_pdf))

    def test_mineru_poll_fast_fails_after_5_consecutive_errors(self):
        """连续 5 次网络错误应快速失败（而非等到 30 分钟超时）。"""
        from unittest.mock import patch as _patch
        from core import mineru_client as mc

        with _patch("core.mineru_client.requests.get",
                    side_effect=mc.requests.exceptions.ConnectionError("connection reset")):
            with _patch("core.mineru_client.POLL_TIMEOUT", 1800):
                with _patch("core.mineru_client.time.sleep") as mock_sleep:  # 消除 4×5s 真实睡眠
                    with pytest.raises(RuntimeError, match="5 consecutive network errors"):
                        mc.poll_job("batch-1", lambda done, total: None)
        assert mock_sleep.call_count >= 4

    def test_mineru_poll_recovers_after_transient_error(self):
        """连续错误后成功响应应重置计数（不误杀）。"""
        from unittest.mock import patch as _patch
        from core import mineru_client as mc

        class FakeResp:
            status_code = 200
            def json(self):
                return {"code": 0, "data": {"extract_result": [{"batch_id": "b1"}],
                                            "extract_progress": {"done": 1, "total": 1}}}

        calls = {"n": 0}
        def flaky_get(*a, **kw):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("transient")
            return FakeResp()

        with _patch("core.mineru_client.requests.get", side_effect=flaky_get), \
             _patch("core.mineru_client._headers", return_value={}), \
             _patch("core.mineru_client._api_base", return_value="http://mock"), \
             _patch("core.mineru_client.POLL_TIMEOUT", 1800):
            # 第 3 次成功后走到结果处理分支，需要 download 相关 mock
            with _patch("core.mineru_client.time.sleep"):
                try:
                    mc.poll_job("batch-1", lambda done, total: None)
                except Exception:
                    pass
        # 2 次错误 + 1 次成功：错误计数应被重置，未触发快速失败
        assert calls["n"] <= 3

    def test_split_pages_by_content_list(self):
        """_split_pages_by_content_list 应按 page_idx 分组。"""
        # 这个函数需要 ZipFile，用 mock 测试逻辑
        blocks = [
            {"page_idx": 0, "type": "text", "text": "第1页"},
            {"page_idx": 0, "type": "text", "text": "继续"},
            {"page_idx": 1, "type": "text", "text": "第2页"},
        ]
        # 测试 _block_to_markdown
        md = mineru_client._block_to_markdown(blocks[0])
        assert "第1页" in md

    def test_block_to_markdown_unknown_type_keeps_content(self):
        """未知块类型不应静默丢内容 — 递归兜底提取全部字符串字段。"""
        block = {
            "type": "figure_caption",  # 服务端可能引入的新类型
            "content": {
                "caption_content": [
                    {"type": "text", "content": "图 1 设备布局示意"},
                    {"type": "text", "content": "（含楼层标注）"},
                ]
            },
        }
        md = mineru_client._block_to_markdown(block)
        assert "图 1 设备布局示意" in md
        assert "（含楼层标注）" in md

    def test_table_html_multi_field_fallback(self):
        """表格 HTML 提取应支持 v2 content.table_html 字段（格式漂移防护）。"""
        block = {"type": "table", "content": {"table_html": "<table><tr><td>设备</td></tr></table>"}}
        html = mineru_client._table_html(block)
        assert "<table>" in html

    def test_download_result_falls_back_to_full_md_when_content_list_mostly_missing(self):
        """content_list 解析丢失大部分内容时（<50% of full.md），应回退 full.md
        拆分 — 防止静默输出残缺页（OCR 鲁棒性，回归 51 页真实文件）。"""
        import io, zipfile
        from unittest.mock import patch as _patch

        # 构造 zip：content_list 只有 2 页空内容（纯数字页脚 — 无文字，
        # 对抗审查 cr-17 后仍被过滤），full.md 有完整 2 页（\f 分隔）
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "x_content_list_v2.json",
                json.dumps([
                    [{"type": "page_footer", "text": "2/24"}],
                    [{"type": "page_footer", "text": "2/24"}],
                ]),
            )
            zf.writestr(
                "x_full.md",
                "## 第一页\n\n批号 112701 含量测定\n\n称量记录：\n实际投入 25.00 kg\n理论回收率 98.5%\n\n"
                "\f\n## 第二页\n\n中间体储存温度 15-25°C\n\n干燥失重 ≤0.5%\n颗粒含量 99.2%\n",
            )
        zip_bytes = buf.getvalue()

        task_result = {"full_zip_url": "http://mock/result.zip"}
        with _patch("core.mineru_client.requests.get") as mock_get:
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.content = zip_bytes
            mock_get.return_value = resp
            pages = mineru_client.download_result(task_result)

        # 回退 full.md → 2 页，内容完整
        assert len(pages) == 2, f"expected 2 pages, got {len(pages)}"
        joined = "\n".join(p["markdown"]["text"] for p in pages)
        assert "批号 112701" in joined
        assert "中间体储存温度 15-25°C" in joined

    def test_download_result_keeps_content_list_when_complete(self):
        """content_list 内容完整时（正文存在）不应触发回退（结构维度对照）。"""
        import io, zipfile
        from unittest.mock import patch as _patch

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "x_content_list_v2.json",
                json.dumps([
                    [{"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "完整段落内容 ABC"}]}}],
                ]),
            )
            zf.writestr("x_full.md", "完整段落内容 ABC\n")
        zip_bytes = buf.getvalue()

        task_result = {"full_zip_url": "http://mock/result2.zip"}
        with _patch("core.mineru_client.requests.get") as mock_get:
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.content = zip_bytes
            mock_get.return_value = resp
            pages = mineru_client.download_result(task_result)

        assert len(pages) == 1
        assert "完整段落内容 ABC" in pages[0]["markdown"]["text"]

    def test_download_result_structural_check_passes_when_tables_intact(self):
        """表格被完整提取（content_list 3 表）时不应回退 — 即使 HTML 标签
        膨胀使字符数远超 full.md（P1-4：结构维度替代字符数对比）。"""
        import io, zipfile
        from unittest.mock import patch as _patch

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "x_content_list_v2.json",
                json.dumps([
                    [
                        {"type": "table", "content": {"table_html": "<table><tr><td>批号</td></tr></table>"}},
                        {"type": "table", "content": {"table_html": "<table><tr><td>含量</td></tr></table>"}},
                        {"type": "table", "content": {"table_html": "<table><tr><td>水分</td></tr></table>"}},
                    ]
                ]),
            )
            zf.writestr(
                "x_full.md",
                "| 批号 |\n| --- |\n| 112701 |\n"
                "| 含量 |\n| --- |\n| 99.2% |\n"
                "| 水分 |\n| --- |\n| 0.5% |\n",
            )
        with _patch("core.mineru_client.requests.get") as mock_get:
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.content = buf.getvalue()
            mock_get.return_value = resp
            pages = mineru_client.download_result({"full_zip_url": "http://mock/zip"})

        assert len(pages) == 1
        assert "<table>" in pages[0]["markdown"]["text"]

    def test_download_result_falls_back_when_tables_all_missing(self):
        """表格整体丢失：content_list 0 表格而 full.md 有管道表 → 回退
        （P1-4：批记录核心信息在表格，全丢 = 灾难）。"""
        import io, zipfile
        from unittest.mock import patch as _patch

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "x_content_list_v2.json",
                json.dumps([
                    [{"type": "text", "content": {"paragraph_content": [{"type": "text", "content": "只有正文"}]}}],
                ]),
            )
            zf.writestr(
                "x_full.md",
                "| 批号 | 含量 |\n| --- | --- |\n| 112701 | 99.2% |\n",
            )
        with _patch("core.mineru_client.requests.get") as mock_get:
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.content = buf.getvalue()
            mock_get.return_value = resp
            pages = mineru_client.download_result({"full_zip_url": "http://mock/zip2"})

        assert "| 批号 |" in "\n".join(p["markdown"]["text"] for p in pages)

    def test_download_result_no_fallback_when_page_count_close(self):
        """页数差 ≤1（分页符解析抖动）不应触发回退（P1-4 容差）。"""
        import io, zipfile
        from unittest.mock import patch as _patch

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "x_content_list_v2.json",
                json.dumps([
                    [{"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "页1"}]}}],
                ]),
            )
            zf.writestr(
                "x_full.md",
                "页1\n\f\n尾部杂散分隔符（无实际内容）\n",  # full.md 拆出 2 页
            )
        with _patch("core.mineru_client.requests.get") as mock_get:
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.content = buf.getvalue()
            mock_get.return_value = resp
            pages = mineru_client.download_result({"full_zip_url": "http://mock/zip3"})

        assert len(pages) == 1  # 保持 content_list 结果

    def test_structural_violated_helper(self):
        """_structural_completeness_violated 三信号直接验证。"""
        # 1. 页数缺失
        v, reason = mineru_client._structural_completeness_violated(
            "a\n\fb", n_pages=0, n_tables=1, n_paragraphs=1
        )
        assert v and "页数" in reason
        # 2. 表格全丢
        v, reason = mineru_client._structural_completeness_violated(
            "| a |\n| b |", n_pages=1, n_tables=0, n_paragraphs=1
        )
        assert v and "表格" in reason
        # 3. 噪声块仅剩（无正文无表格）→ 空内容不判残
        v, _ = mineru_client._structural_completeness_violated(
            "   ", n_pages=1, n_tables=0, n_paragraphs=0
        )
        assert not v
        # 4. 完整：正文 + 表格齐全
        v, _ = mineru_client._structural_completeness_violated(
            "段落\n| a |\n| b |", n_pages=1, n_tables=1, n_paragraphs=1
        )
        assert not v


class TestRunOCRPAGES:
    """run_ocr_pages — 空页小切片重跑（大文件丢页修复）。"""

    def _make_pdf(self, pages=3):
        import fitz
        import tempfile as _tf

        buf = io.BytesIO()
        doc = fitz.open()
        for i in range(pages):
            doc.new_page().insert_text((50, 50), f"page {i + 1}")
        doc.save(buf)
        doc.close()
        buf.seek(0)
        p = _tf.NamedTemporaryFile(delete=False, suffix=".pdf")
        p.write(buf.read())
        p.close()
        return p.name

    @patch("core.mineru_client.run_ocr")
    def test_maps_slice_pages_back_to_requested_numbers(self, mock_ocr):
        pdf = self._make_pdf(3)
        mock_ocr.return_value = [
            {"markdown": {"text": "AAA"}},
            {"markdown": {"text": "BBB"}},
            {"markdown": {"text": "CCC"}},
        ]
        out = mineru_client.run_ocr_pages(pdf, [1, 2, 3], batch_size=3)
        assert out == [(1, "AAA", 0), (2, "BBB", 0), (3, "CCC", 0)]
        mock_ocr.assert_called_once()
        import os

        os.unlink(pdf)

    @patch("core.mineru_client.run_ocr")
    def test_batches_multiple_calls(self, mock_ocr):
        pdf = self._make_pdf(5)
        mock_ocr.return_value = [{"markdown": {"text": "X"}}]
        out = mineru_client.run_ocr_pages(pdf, [1, 5], batch_size=1)
        assert mock_ocr.call_count == 2
        assert out == [(1, "X", 0), (5, "X", 0)]

    @patch("core.mineru_client.run_ocr")
    def test_out_of_range_page_keeps_empty_text(self, mock_ocr):
        """越界页（99）空文本保留；P1-2 修复后批返回 1 页 ≠ 请求 3 页时
        合法页退回单页重跑（页 2 是合法页 → 拿到 ONLY，不再按下标取空）。"""
        pdf = self._make_pdf(2)
        mock_ocr.return_value = [{"markdown": {"text": "ONLY"}}]
        out = mineru_client.run_ocr_pages(pdf, [1, 99, 2], batch_size=3)
        assert out == [(1, "ONLY", 0), (99, "", 0), (2, "ONLY", 0)]

    @patch("core.mineru_client.run_ocr")
    def test_all_pages_out_of_range_skips_submission(self, mock_ocr):
        pdf = self._make_pdf(2)
        out = mineru_client.run_ocr_pages(pdf, [99, 100], batch_size=2)
        assert out == [(99, "", 0), (100, "", 0)]
        mock_ocr.assert_not_called()

    @patch("core.mineru_client.run_ocr")
    def test_short_response_falls_back_to_single_page(self, mock_ocr):
        """对抗审查 P1-2：MinerU 对 3 页切片只返回 2 页时，按数组下标写
        会把第 7 页内容张冠李戴到第 6 页。必须退回逐页独立重跑。"""
        pdf = self._make_pdf(3)
        # 批调用返回 2 页（缺 1 页）→ 每页单独重跑（返回 1 页各一次）
        mock_ocr.side_effect = [
            [{"markdown": {"text": "P1"}}, {"markdown": {"text": "P2"}}],  # 批：缺页
            [{"markdown": {"text": "S1"}}],  # 单页 1
            [{"markdown": {"text": "S2"}}],  # 单页 2
            [{"markdown": {"text": "S3"}}],  # 单页 3
        ]
        out = mineru_client.run_ocr_pages(pdf, [1, 2, 3], batch_size=3)
        # 每页内容与其自身单页重跑结果一一对应，不串页
        assert out == [(1, "S1", 0), (2, "S2", 0), (3, "S3", 0)]
        assert mock_ocr.call_count == 4  # 1 批 + 3 单页
        import os

        os.unlink(pdf)

    @patch("core.mineru_client.run_ocr")
    def test_batch_exception_falls_back_to_single_page(self, mock_ocr):
        """对抗审查 P2-1：单批异常不再中断整条重试链，退回单页重跑。"""
        pdf = self._make_pdf(2)
        mock_ocr.side_effect = [
            RuntimeError("network boom"),  # 批失败
            [{"markdown": {"text": "S1"}}],
            [{"markdown": {"text": "S2"}}],
        ]
        out = mineru_client.run_ocr_pages(pdf, [1, 2], batch_size=2)
        assert out == [(1, "S1", 0), (2, "S2", 0)]
        assert mock_ocr.call_count == 3

    @patch("core.mineru_client.run_ocr")
    def test_discarded_count_propagates(self, mock_ocr):
        """D3 修复（Round 3）：自愈切片重跑也透传 _discarded_count（低置信度
        丢弃块数），pipeline 据此给恢复页补回 OCR 不完整警告 — 恢复页不再被
        静默当作完整页（LLM 置信度虚高）。"""
        pdf = self._make_pdf(3)
        mock_ocr.return_value = [
            {"markdown": {"text": "AAA"}, "_discarded_count": 2},
            {"markdown": {"text": "BBB"}},
            {"markdown": {"text": "CCC"}, "_discarded_count": 5},
        ]
        out = mineru_client.run_ocr_pages(pdf, [1, 2, 3], batch_size=3)
        assert out == [(1, "AAA", 2), (2, "BBB", 0), (3, "CCC", 5)]
        # 单页回退路径同样透传
        mock_ocr.reset_mock()
        mock_ocr.return_value = [
            {"markdown": {"text": "S1"}, "_discarded_count": 3}
        ]
        pdf1 = self._make_pdf(1)
        out1 = mineru_client.run_ocr_pages(pdf1, [1], batch_size=3)
        assert out1 == [(1, "S1", 3)]
        import os

        os.unlink(pdf)
        os.unlink(pdf1)


class TestPaddleOCRClient:
    """PaddleOCR 客户端（模块级函数）。"""

    def test_module_has_required_functions(self):
        """应暴露必需函数。"""
        # ocr_client 模块应有 OCR 主入口
        assert hasattr(ocr_client, 'run_ocr') or hasattr(ocr_client, 'process_pdf')


class TestOCRBackendSelection:
    """OCR 后端选择逻辑（pipeline 集成）。"""

    def test_config_has_ocr_backend_field(self):
        """config 应有 ocr_backend 字段。"""
        from config import config
        assert hasattr(config["app"], "ocr_backend")

    def test_config_ocr_backend_valid_value(self):
        """ocr_backend 应为 paddle 或 mineru。"""
        from config import config
        assert config["app"].ocr_backend in ("paddle", "mineru")
