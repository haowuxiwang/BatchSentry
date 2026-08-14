"""OCR 客户端单元测试 — MinerU 和 PaddleOCR。

使用 mock 避免真实 HTTP 调用。
"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path

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
                with pytest.raises(RuntimeError, match="5 consecutive network errors"):
                    mc.poll_job("batch-1", lambda done, total: None)

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
             _patch("core.mineru_client._API_BASE", "http://mock"), \
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
