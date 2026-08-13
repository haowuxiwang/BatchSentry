"""OCR 客户端单元测试 — 通过 mock HTTP 请求测试 PaddleOCR 和 MinerU 客户端。

覆盖：
- PaddleOCR (core/ocr_client.py): submit_pdf / poll_job / download_result / run_ocr
- MinerU (core/mineru_client.py): submit_pdf / poll_job / download_result / run_ocr
  以及 _headers / _block_to_markdown / _split_pages_by_content_list

所有 HTTP 调用（requests.post / get / put）通过 unittest.mock 拦截，不发送真实请求。
所有测试函数均为同步，不使用 @pytest.mark.asyncio。
"""
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

from core import ocr_client, mineru_client
from config import config


# ---------------------------------------------------------------------------
# 辅助函数与 fixture
# ---------------------------------------------------------------------------

def _make_zip_bytes(files: dict) -> bytes:
    """构建内存 zip 字节流。

    Args:
        files: {文件名: 内容(str 或 bytes)} 字典
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            if isinstance(content, str):
                zf.writestr(name, content)
            else:
                zf.writestr(name, content)
    return buf.getvalue()


@pytest.fixture
def fake_pdf(tmp_path):
    """创建一个临时 PDF 文件用于测试（内容无需是真实 PDF，只要能 open/stat）。"""
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%fake pdf content for testing\n%%EOF")
    return str(pdf)


@pytest.fixture
def paddle_cfg(monkeypatch):
    """配置 PaddleOCR 测试参数（自动恢复）。"""
    monkeypatch.setattr(config["paddle_ocr"], "api_url", "https://paddle.test/api")
    monkeypatch.setattr(config["paddle_ocr"], "token", "test-paddle-token")
    monkeypatch.setattr(config["paddle_ocr"], "model", "PaddleOCR-VL-TEST")
    return config["paddle_ocr"]


@pytest.fixture
def mineru_cfg(monkeypatch):
    """配置 MinerU 测试参数（自动恢复）。"""
    monkeypatch.setattr(config["mineru"], "token", "test-mineru-token")
    monkeypatch.setattr(config["mineru"], "model_version", "vlm")
    monkeypatch.setattr(config["mineru"], "language", "ch")
    monkeypatch.setattr(config["mineru"], "enable_formula", True)
    monkeypatch.setattr(config["mineru"], "enable_table", True)
    return config["mineru"]


# ===========================================================================
# PaddleOCR 客户端测试 (core/ocr_client.py)
# ===========================================================================

class TestPaddleOCRSubmit:
    """PaddleOCR submit_pdf — 提交 PDF 并返回 job_id。"""

    @patch('core.ocr_client.requests.post')
    def test_submit_pdf_success(self, mock_post, fake_pdf, paddle_cfg):
        """submit_pdf 成功返回 job_id（嵌套在 data.jobId 中）。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"jobId": "job-abc-123"}}
        mock_post.return_value = mock_resp

        job_id = ocr_client.submit_pdf(fake_pdf)
        assert job_id == "job-abc-123"
        mock_post.assert_called_once()

        # 验证请求参数
        args, kwargs = mock_post.call_args
        assert args[0] == "https://paddle.test/api"
        assert kwargs["headers"]["Authorization"] == "bearer test-paddle-token"
        assert "files" in kwargs
        assert "data" in kwargs
        assert kwargs["data"]["model"] == "PaddleOCR-VL-TEST"

    @patch('core.ocr_client.requests.post')
    def test_submit_pdf_top_level_jobId(self, mock_post, fake_pdf, paddle_cfg):
        """submit_pdf 兼容顶层 jobId 字段（无 data 包裹）。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"jobId": "job-top-456"}
        mock_post.return_value = mock_resp

        assert ocr_client.submit_pdf(fake_pdf) == "job-top-456"

    @patch('core.ocr_client.time.sleep')
    @patch('core.ocr_client.requests.post')
    def test_submit_pdf_no_jobid_retries_then_raises(self, mock_post, mock_sleep, fake_pdf, paddle_cfg):
        """响应缺少 jobId 时重试并最终抛 RuntimeError。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {}}
        mock_post.return_value = mock_resp

        with pytest.raises(RuntimeError, match="Submit failed after 3"):
            ocr_client.submit_pdf(fake_pdf, retries=3)
        assert mock_post.call_count == 3

    @patch('core.ocr_client.time.sleep')
    @patch('core.ocr_client.requests.post')
    def test_submit_pdf_http_error_retries(self, mock_post, mock_sleep, fake_pdf, paddle_cfg):
        """HTTP 非 200 时重试并最终抛 RuntimeError。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "internal server error"
        mock_post.return_value = mock_resp

        with pytest.raises(RuntimeError, match="Submit failed after 3"):
            ocr_client.submit_pdf(fake_pdf, retries=3)
        assert mock_post.call_count == 3

    @patch('core.ocr_client.time.sleep')
    @patch('core.ocr_client.requests.post')
    def test_submit_pdf_missing_token_raises(self, mock_post, mock_sleep, fake_pdf, monkeypatch):
        """token 缺失时 API 返回鉴权错误，submit_pdf 重试后抛 RuntimeError。

        PaddleOCR 客户端无显式 token 检查，空 token 会导致 API 拒绝请求。
        """
        monkeypatch.setattr(config["paddle_ocr"], "token", "")
        monkeypatch.setattr(config["paddle_ocr"], "api_url", "https://paddle.test/api")
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "unauthorized: invalid token"
        mock_post.return_value = mock_resp

        with pytest.raises(RuntimeError, match="Submit failed"):
            ocr_client.submit_pdf(fake_pdf)
        # 验证空 token 时 Authorization 头为 "bearer "
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["Authorization"] == "bearer "

    @patch('core.ocr_client.requests.post')
    def test_submit_pdf_reads_file_via_mock_open(self, mock_post, paddle_cfg):
        """submit_pdf 流式读取文件作为上传数据（使用 mock_open 避免真实文件）。

        更新：submit_pdf 现在用 open() 返回的文件对象直接传给 requests，
        通过 seek(0, 2) 获取文件大小，再 seek(0) 重置指针。
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"jobId": "job-read-file"}}
        mock_post.return_value = mock_resp

        # mock_open 的返回值需要支持 seek() 调用
        m = mock_open(read_data=b"fake-pdf-bytes")
        mock_file = m.return_value
        # seek(0, 2) 返回文件大小（流式上传时用于计算 timeout）
        mock_file.seek.return_value = 14  # len("fake-pdf-bytes")

        with patch('builtins.open', m):
            job_id = ocr_client.submit_pdf("/fake/nonexistent.pdf")

        assert job_id == "job-read-file"
        m.assert_called_once_with("/fake/nonexistent.pdf", "rb")


class TestPaddleOCRPoll:
    """PaddleOCR poll_job — 轮询任务状态。"""

    @patch('core.ocr_client.requests.get')
    def test_poll_job_done(self, mock_get, paddle_cfg):
        """state=done 时返回完整响应。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {
                "state": "done",
                "resultUrl": {"jsonUrl": "https://res.test/json"},
            }
        }
        mock_get.return_value = mock_resp

        result = ocr_client.poll_job("job-123")
        assert result["data"]["state"] == "done"
        mock_get.assert_called_once()

        url = mock_get.call_args[0][0]
        assert url == "https://paddle.test/api/job-123"
        assert mock_get.call_args[1]["headers"]["Authorization"] == "bearer test-paddle-token"

    @patch('core.ocr_client.requests.get')
    def test_poll_job_success_state(self, mock_get, paddle_cfg):
        """state=success 也视为完成。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"state": "success"}}
        mock_get.return_value = mock_resp

        result = ocr_client.poll_job("job-123")
        assert result["data"]["state"] == "success"

    @patch('core.ocr_client.requests.get')
    def test_poll_job_failed_raises(self, mock_get, paddle_cfg):
        """state=failed 时抛 RuntimeError。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"state": "failed"}}
        mock_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="Job failed"):
            ocr_client.poll_job("job-123")

    @patch('core.ocr_client.requests.get')
    def test_poll_job_error_state_raises(self, mock_get, paddle_cfg):
        """state=error 时抛 RuntimeError。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {"state": "error"}}
        mock_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="Job failed"):
            ocr_client.poll_job("job-123")

    @patch('core.ocr_client.requests.get')
    def test_poll_job_top_level_state(self, mock_get, paddle_cfg):
        """兼容顶层 state 字段（无 data 包裹）。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"state": "done"}
        mock_get.return_value = mock_resp

        result = ocr_client.poll_job("job-123")
        assert result["state"] == "done"


class TestPaddleOCRDownload:
    """PaddleOCR download_result — 下载并解析 OCR 结果。"""

    @patch('core.ocr_client.requests.get')
    def test_download_result_single_json(self, mock_get, paddle_cfg):
        """解析单个 JSON 响应，返回 layoutParsingResults。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps({
            "result": {
                "layoutParsingResults": [
                    {"markdown": {"text": "第1页内容"}},
                    {"markdown": {"text": "第2页内容"}},
                ],
                "dataInfo": {},
            }
        })
        mock_get.return_value = mock_resp

        poll_response = {"data": {"resultUrl": {"jsonUrl": "https://res.test/json"}}}
        pages = ocr_client.download_result(poll_response)

        assert len(pages) == 2
        assert pages[0]["markdown"]["text"] == "第1页内容"
        assert pages[1]["markdown"]["text"] == "第2页内容"

    @patch('core.ocr_client.requests.get')
    def test_download_result_jsonl(self, mock_get, paddle_cfg):
        """解析 JSONL 响应（每行一个 JSON 对象）。"""
        line1 = json.dumps({"result": {"layoutParsingResults": [{"markdown": {"text": "p1"}}]}})
        line2 = json.dumps({"result": {"layoutParsingResults": [{"markdown": {"text": "p2"}}]}})
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = f"{line1}\n{line2}"
        mock_get.return_value = mock_resp

        poll_response = {"resultUrl": "https://res.test/jsonl"}
        pages = ocr_client.download_result(poll_response)

        assert len(pages) == 2
        assert pages[0]["markdown"]["text"] == "p1"
        assert pages[1]["markdown"]["text"] == "p2"

    @patch('core.ocr_client.requests.get')
    def test_download_result_string_url(self, mock_get, paddle_cfg):
        """resultUrl 为字符串时直接作为下载 URL。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps({
            "result": {"layoutParsingResults": [{"markdown": {"text": "x"}}]}
        })
        mock_get.return_value = mock_resp

        pages = ocr_client.download_result({"data": {"resultUrl": "https://res.test/str"}})
        assert len(pages) == 1
        assert pages[0]["markdown"]["text"] == "x"
        assert mock_get.call_args[0][0] == "https://res.test/str"

    @patch('core.ocr_client.requests.get')
    def test_download_result_dict_url_field(self, mock_get, paddle_cfg):
        """resultUrl 为 dict 时优先取 jsonUrl，回退到 url。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = json.dumps({
            "result": {"layoutParsingResults": [{"markdown": {"text": "y"}}]}
        })
        mock_get.return_value = mock_resp

        ocr_client.download_result({"data": {"resultUrl": {"url": "https://res.test/fallback"}}})
        assert mock_get.call_args[0][0] == "https://res.test/fallback"

    @patch('core.ocr_client.requests.get')
    def test_download_result_no_url_raises(self, mock_get, paddle_cfg):
        """无 resultUrl 时抛 RuntimeError。"""
        with pytest.raises(RuntimeError, match="No result URL"):
            ocr_client.download_result({"data": {}})

    @patch('core.ocr_client.requests.get')
    def test_download_result_http_error_raises(self, mock_get, paddle_cfg):
        """下载 HTTP 失败时抛 RuntimeError。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="Download failed"):
            ocr_client.download_result({"data": {"resultUrl": "https://res.test/404"}})


class TestPaddleOCRRunOCR:
    """PaddleOCR run_ocr — 端到端编排。"""

    @patch('core.ocr_client.download_result')
    @patch('core.ocr_client.poll_job')
    @patch('core.ocr_client.submit_pdf')
    def test_run_ocr_end_to_end(self, mock_submit, mock_poll, mock_download, fake_pdf, paddle_cfg):
        """run_ocr 串联 submit → poll → download，返回页面列表。"""
        mock_submit.return_value = "job-e2e"
        mock_poll.return_value = {"data": {"state": "done"}}
        mock_download.return_value = [
            {"markdown": {"text": "第1页"}},
            {"markdown": {"text": "第2页"}},
        ]

        pages = ocr_client.run_ocr(fake_pdf)

        assert isinstance(pages, list)
        assert len(pages) == 2
        assert pages[0]["markdown"]["text"] == "第1页"
        assert pages[1]["markdown"]["text"] == "第2页"

        mock_submit.assert_called_once_with(fake_pdf)
        mock_poll.assert_called_once_with("job-e2e", progress_callback=None)
        mock_download.assert_called_once_with({"data": {"state": "done"}})

    @patch('core.ocr_client.download_result')
    @patch('core.ocr_client.poll_job')
    @patch('core.ocr_client.submit_pdf')
    def test_run_ocr_propagates_progress_callback(self, mock_submit, mock_poll, mock_download, fake_pdf, paddle_cfg):
        """progress_callback 透传给 poll_job（Stage 1 流式反馈）。"""
        mock_submit.return_value = "job-e2e"
        mock_poll.return_value = {"data": {"state": "done"}}
        mock_download.return_value = [{"markdown": {"text": "x"}}]

        calls = []
        ocr_client.run_ocr(fake_pdf, progress_callback=lambda d, t: calls.append((d, t)))

        assert mock_poll.call_args[0] == ("job-e2e",)
        assert callable(mock_poll.call_args[1]["progress_callback"])

    @patch('core.ocr_client.download_result')
    @patch('core.ocr_client.poll_job')
    @patch('core.ocr_client.submit_pdf')
    def test_run_ocr_logs_job_id_prefix(self, mock_submit, mock_poll, mock_download,
                                        fake_pdf, paddle_cfg, caplog):
        """robustness-F1: 传 job_id 时模块日志自动带 [job_id] 前缀。"""
        mock_submit.return_value = "job-e2e"
        mock_poll.return_value = {"data": {"state": "done"}}
        mock_download.return_value = []

        import logging
        with caplog.at_level(logging.INFO, logger="core.ocr_client"):
            ocr_client.run_ocr(fake_pdf, job_id="job-123")

        # 空结果触发 defensive warning，filter 应已加 [job-123] 前缀
        assert any("[job-123]" in r.message for r in caplog.records)

    def test_run_ocr_without_job_id_has_no_prefix(self, fake_pdf, paddle_cfg, caplog):
        """robustness-F1: 不传 job_id 时日志无前缀（默认行为不变）。"""
        import logging
        with caplog.at_level(logging.INFO, logger="core.ocr_client"):
            with patch('core.ocr_client.requests.post',
                       side_effect=RuntimeError("conn refused")):
                with pytest.raises(RuntimeError):
                    ocr_client.submit_pdf(fake_pdf)

        assert all(not r.message.startswith("[") for r in caplog.records)

    def test_poll_job_invokes_progress_callback(self, paddle_cfg):
        """poll_job 轮询到 extractProgress 时回调 (done, total)。"""
        from unittest.mock import patch as _patch
        progress_responses = iter([
            # 第一次轮询：running + 进度 12/51
            type("R", (), {"status_code": 200, "json": lambda self: {
                "data": {"state": "running",
                         "extractProgress": {"extractedPages": 12, "totalPages": 51}}
            }})(),
            # 第二次轮询：done
            type("R", (), {"status_code": 200, "json": lambda self: {
                "data": {"state": "done",
                         "extractProgress": {"extractedPages": 51, "totalPages": 51}}
            }})(),
        ])
        with _patch('core.ocr_client.requests.get', side_effect=lambda *a, **k: next(progress_responses)):
            progress_calls = []
            ocr_client.poll_job("job-prog", progress_callback=lambda d, t: progress_calls.append((d, t)))

        assert progress_calls == [(12, 51), (51, 51)]

    @patch('core.ocr_client.download_result')
    @patch('core.ocr_client.poll_job')
    @patch('core.ocr_client.submit_pdf')
    def test_run_ocr_propagates_submit_error(self, mock_submit, mock_poll, mock_download, fake_pdf, paddle_cfg):
        """submit_pdf 失败时 run_ocr 向上抛异常。"""
        mock_submit.side_effect = RuntimeError("Submit failed: network error")
        with pytest.raises(RuntimeError, match="Submit failed"):
            ocr_client.run_ocr(fake_pdf)
        mock_poll.assert_not_called()
        mock_download.assert_not_called()


# ===========================================================================
# MinerU 客户端测试 (core/mineru_client.py)
# ===========================================================================

class TestMinerUHeaders:
    """MinerU _headers — 认证头构建。"""

    def test_headers_with_token(self, mineru_cfg):
        """有 token 时返回正确认证头。"""
        h = mineru_client._headers()
        assert h["Authorization"] == "Bearer test-mineru-token"
        assert h["Content-Type"] == "application/json"

    def test_headers_without_token_raises(self, monkeypatch):
        """无 token 时 _headers 抛 RuntimeError。"""
        monkeypatch.setattr(config["mineru"], "token", "")
        with pytest.raises(RuntimeError, match="MinerU token 未配置"):
            mineru_client._headers()


class TestMinerUSubmit:
    """MinerU submit_pdf — 申请上传链接并上传 PDF。"""

    @patch('core.mineru_client.requests.put')
    @patch('core.mineru_client.requests.post')
    def test_submit_pdf_success(self, mock_post, mock_put, fake_pdf, mineru_cfg):
        """submit_pdf 成功返回 (batch_id, pdf_name)。"""
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {
            "code": 0,
            "data": {
                "batch_id": "batch-001",
                "file_urls": ["https://oss.test/upload"],
            },
        }
        mock_post.return_value = mock_post_resp

        mock_put_resp = MagicMock()
        mock_put_resp.status_code = 200
        mock_put.return_value = mock_put_resp

        batch_id, name = mineru_client.submit_pdf(fake_pdf)

        assert batch_id == "batch-001"
        assert name == "sample.pdf"
        mock_post.assert_called_once()
        mock_put.assert_called_once()

        # 验证 POST 请求参数
        post_args, post_kwargs = mock_post.call_args
        assert post_args[0] == "https://mineru.net/api/v4/file-urls/batch"
        assert post_kwargs["headers"]["Authorization"] == "Bearer test-mineru-token"
        assert post_kwargs["json"]["files"][0]["name"] == "sample.pdf"
        assert post_kwargs["json"]["files"][0]["is_ocr"] is True
        assert post_kwargs["json"]["model_version"] == "vlm"

        # 验证 PUT 上传到返回的 URL
        put_args, put_kwargs = mock_put.call_args
        assert put_args[0] == "https://oss.test/upload"

    @patch('core.mineru_client.requests.put')
    @patch('core.mineru_client.requests.post')
    def test_submit_pdf_no_token_raises(self, mock_post, mock_put, fake_pdf, monkeypatch):
        """无 token 时 submit_pdf 抛 RuntimeError（不发任何请求）。"""
        monkeypatch.setattr(config["mineru"], "token", "")
        with pytest.raises(RuntimeError, match="MinerU token 未配置"):
            mineru_client.submit_pdf(fake_pdf)
        mock_post.assert_not_called()
        mock_put.assert_not_called()

    @patch('core.mineru_client.requests.put')
    @patch('core.mineru_client.requests.post')
    def test_submit_pdf_api_error_code_raises(self, mock_post, mock_put, fake_pdf, mineru_cfg):
        """API 返回非 0 code 时抛 RuntimeError。"""
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {"code": 4001, "msg": "invalid params"}
        mock_post.return_value = mock_post_resp

        with pytest.raises(RuntimeError, match="申请上传链接失败"):
            mineru_client.submit_pdf(fake_pdf)
        mock_put.assert_not_called()

    @patch('core.mineru_client.requests.put')
    @patch('core.mineru_client.requests.post')
    def test_submit_pdf_http_error_raises(self, mock_post, mock_put, fake_pdf, mineru_cfg):
        """POST HTTP 非 200 时抛 RuntimeError。"""
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 500
        mock_post_resp.text = "server error"
        mock_post.return_value = mock_post_resp

        with pytest.raises(RuntimeError, match="申请上传链接失败 HTTP 500"):
            mineru_client.submit_pdf(fake_pdf)

    @patch('core.mineru_client.requests.put')
    @patch('core.mineru_client.requests.post')
    def test_submit_pdf_no_file_urls_raises(self, mock_post, mock_put, fake_pdf, mineru_cfg):
        """返回空 file_urls 时抛 RuntimeError。"""
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {
            "code": 0,
            "data": {"batch_id": "b1", "file_urls": []},
        }
        mock_post.return_value = mock_post_resp

        with pytest.raises(RuntimeError, match="未返回上传 URL"):
            mineru_client.submit_pdf(fake_pdf)

    @patch('core.mineru_client.requests.put')
    @patch('core.mineru_client.requests.post')
    def test_submit_pdf_upload_failed_raises(self, mock_post, mock_put, fake_pdf, mineru_cfg):
        """PUT 上传失败时抛 RuntimeError。"""
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {
            "code": 0,
            "data": {"batch_id": "b1", "file_urls": ["https://oss.test/u"]},
        }
        mock_post.return_value = mock_post_resp

        mock_put_resp = MagicMock()
        mock_put_resp.status_code = 403
        mock_put_resp.text = "forbidden"
        mock_put.return_value = mock_put_resp

        with pytest.raises(RuntimeError, match="文件上传失败"):
            mineru_client.submit_pdf(fake_pdf)


class TestMinerUPoll:
    """MinerU poll_job — 轮询批次结果。"""

    @patch('core.mineru_client.time.sleep')
    @patch('core.mineru_client.requests.get')
    def test_poll_job_done(self, mock_get, mock_sleep, mineru_cfg):
        """state=done 时返回 task 结果。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 0,
            "data": {
                "extract_result": [
                    {"task_id": "t1", "state": "done", "full_zip_url": "https://zip.test/1"}
                ]
            },
        }
        mock_get.return_value = mock_resp

        task = mineru_client.poll_job("batch-001")
        assert task["task_id"] == "t1"
        assert task["state"] == "done"
        assert task["full_zip_url"] == "https://zip.test/1"

        url = mock_get.call_args[0][0]
        assert url == "https://mineru.net/api/v4/extract-results/batch/batch-001"

    @patch('core.mineru_client.time.sleep')
    @patch('core.mineru_client.requests.get')
    def test_poll_job_failed_raises(self, mock_get, mock_sleep, mineru_cfg):
        """state=failed 时抛 RuntimeError。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 0,
            "data": {
                "extract_result": [
                    {"task_id": "t1", "state": "failed", "err_msg": "parse error"}
                ]
            },
        }
        mock_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="解析失败"):
            mineru_client.poll_job("batch-001")

    @patch('core.mineru_client.time.sleep')
    @patch('core.mineru_client.requests.get')
    def test_poll_job_no_result_then_done(self, mock_get, mock_sleep, mineru_cfg):
        """无 extract_result 时等待，之后完成。"""
        empty_resp = MagicMock()
        empty_resp.status_code = 200
        empty_resp.json.return_value = {"code": 0, "data": {"extract_result": []}}

        done_resp = MagicMock()
        done_resp.status_code = 200
        done_resp.json.return_value = {
            "code": 0,
            "data": {"extract_result": [{"task_id": "t1", "state": "done"}]},
        }
        mock_get.side_effect = [empty_resp, done_resp]

        task = mineru_client.poll_job("batch-001")
        assert task["state"] == "done"
        assert mock_get.call_count == 2
        assert mock_sleep.call_count == 1  # 第一次空结果后 sleep 一次

    @patch('core.mineru_client.time.sleep')
    @patch('core.mineru_client.requests.get')
    def test_poll_job_http_error_retries(self, mock_get, mock_sleep, mineru_cfg):
        """HTTP 非 200 时重试，之后成功。"""
        err_resp = MagicMock()
        err_resp.status_code = 502

        done_resp = MagicMock()
        done_resp.status_code = 200
        done_resp.json.return_value = {
            "code": 0,
            "data": {"extract_result": [{"task_id": "t1", "state": "done"}]},
        }
        mock_get.side_effect = [err_resp, done_resp]

        task = mineru_client.poll_job("batch-001")
        assert task["state"] == "done"


class TestMinerUDownload:
    """MinerU download_result — 下载 zip 并按页拆分。"""

    @patch('core.mineru_client.requests.get')
    def test_download_result_with_content_list(self, mock_get, mineru_cfg):
        """解析 content_list.json 按 page_idx 分组，保留整页段落结构。"""
        content_list = [
            {"page_idx": 0, "type": "text", "text": "第1页文本"},
            {"page_idx": 0, "type": "text", "text": "继续"},
            {"page_idx": 1, "type": "text", "text": "第2页文本"},
            {"page_idx": 1, "type": "table", "markdown": "| 列1 | 列2 |"},
        ]
        zip_bytes = _make_zip_bytes({
            "full.md": "# 全文",
            "abc_content_list.json": json.dumps(content_list),
        })
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = zip_bytes
        mock_get.return_value = mock_resp

        pages = mineru_client.download_result({"full_zip_url": "https://zip.test/1"})

        assert len(pages) == 2
        # 第1页：标题 + 段落合并（"第1页文本" 和 "继续" 无句末标点，合并为一行）
        assert pages[0]["markdown"]["text"] == "## 第 1 页\n第1页文本 继续"
        assert pages[0]["page_count"] == 1
        assert pages[0]["_source"] == "mineru"
        # 第2页：标题 + 文本 + 表格（前后空行分隔）
        assert pages[1]["markdown"]["text"] == "## 第 2 页\n第2页文本\n\n| 列1 | 列2 |"
        assert pages[1]["page_count"] == 2
        assert pages[1]["_source"] == "mineru"

    @patch('core.mineru_client.requests.get')
    def test_download_result_content_list_v2(self, mock_get, mineru_cfg):
        """v2 格式（content_list_v2.json 按页二维数组）正确拆分 + 表格保留 HTML + 噪音过滤。"""
        content_list_v2 = [
            # 第 1 页：页眉 + 页脚/页码噪音 + 正文段落 + 表格（HTML）
            [
                {"type": "page_header", "content": {"header_content": [{"type": "text", "content": "批记录表头"}]}},
                {"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "工序开始"}]}},
                {"type": "table", "content": {"html": "<table><tr><td>浓度</td></tr></table>", "table_content": []}},
                {"type": "page_footer", "content": {"footer_content": [{"type": "text", "content": "15.60%"}]}},
                {"type": "page_number", "content": {"page_number_content": [{"type": "text", "content": "1/24"}]}},
            ],
            # 第 2 页：标题 + 图片占位 + 公式占位
            [
                {"type": "title", "content": {"title_content": [{"type": "text", "content": "工序二"}]}},
                {"type": "image", "content": {"image_content": [{"type": "text", "content": "图A"}]}},
                {"type": "equation_interline", "content": {"equation_content": [{"type": "text", "content": "E=mc2"}]}},
            ],
        ]
        zip_bytes = _make_zip_bytes({
            "full.md": "# 全文",
            "abc_content_list_v2.json": json.dumps(content_list_v2),
        })
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = zip_bytes
        mock_get.return_value = mock_resp

        pages = mineru_client.download_result({"full_zip_url": "https://zip.test/v2"})

        assert len(pages) == 2
        # 第1页：页眉(###) + 段落 + HTML 表格；页脚"15.60%"和页码"1/24"被过滤
        t1 = pages[0]["markdown"]["text"]
        assert "### 批记录表头" in t1
        assert "工序开始" in t1
        assert "<table><tr><td>浓度</td></tr></table>" in t1
        assert "15.60%" not in t1
        assert "1/24" not in t1
        # 第2页：标题(##) + 图片/公式占位
        t2 = pages[1]["markdown"]["text"]
        assert "## 工序二" in t2
        assert "[image: 图A]" in t2
        assert "[equation_interline: E=mc2]" in t2

    def test_content_text_recursive_nested(self):
        """_content_text 递归提取 v2 嵌套结构（列表套列表 + str 叶子节点）。"""
        block = {
            "type": "paragraph",
            "content": {
                "paragraph_content": [
                    {"type": "text", "content": "第一段"},
                    {"type": "superscript", "content": [
                        {"type": "text", "content": "上标"}
                    ]},
                ]
            },
        }
        assert mineru_client._content_text(block) == "第一段 上标"

    def test_content_text_string_content(self):
        """content 为 str 时直接返回（叶子节点）。"""
        assert mineru_client._content_text({"content": "直接文本"}) == "直接文本"

    def test_content_text_empty(self):
        """无 content 或 content 非 dict/str 时返回空。"""
        assert mineru_client._content_text({"type": "text"}) == ""
        assert mineru_client._content_text({"content": 42}) == ""

    def test_block_noise_filters(self):
        """page_footer / page_number / footer 噪音块返回空（LLM 无需看到页码页脚）。"""
        for btype in ("footer", "page_footer", "page_number"):
            assert mineru_client._block_to_markdown(
                {"type": btype, "text": "15.60%"}
            ) == "", f"{btype} 应被过滤"

    def test_block_header_page_header_title(self):
        """页眉和标题块输出为 markdown 标题（含起草/审核日期信息）。"""
        assert mineru_client._block_to_markdown(
            {"type": "page_header", "text": "起草日期 2024-01-01"}
        ) == "### 起草日期 2024-01-01"
        assert mineru_client._block_to_markdown(
            {"type": "title", "text": "工序二"}
        ) == "## 工序二"

    def test_block_aside_text(self):
        """页侧注释块保留为普通文本。"""
        assert mineru_client._block_to_markdown(
            {"type": "page_aside_text", "text": "旁注"}
        ) == "旁注"

    @patch('core.mineru_client.requests.get')
    def test_download_result_full_md_fallback(self, mock_get, mineru_cfg):
        """无 content_list.json 时降级按分页符拆分 full.md。"""
        full_md = "第一页内容\n\f第二页内容"
        zip_bytes = _make_zip_bytes({"full.md": full_md})
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = zip_bytes
        mock_get.return_value = mock_resp

        pages = mineru_client.download_result({"full_zip_url": "https://zip.test/1"})

        assert len(pages) == 2
        assert "第一页" in pages[0]["markdown"]["text"]
        assert "第二页" in pages[1]["markdown"]["text"]
        assert pages[0]["_source"] == "mineru"

    @patch('core.mineru_client.requests.get')
    def test_download_result_full_md_no_separator(self, mock_get, mineru_cfg):
        """full.md 无分页符时作为单页返回。"""
        zip_bytes = _make_zip_bytes({"full.md": "整篇文档无分页"})
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = zip_bytes
        mock_get.return_value = mock_resp

        pages = mineru_client.download_result({"full_zip_url": "https://zip.test/1"})
        assert len(pages) == 1
        assert pages[0]["markdown"]["text"] == "整篇文档无分页"

    @patch('core.mineru_client.requests.get')
    def test_download_result_no_zip_url_raises(self, mock_get, mineru_cfg):
        """无 full_zip_url 时抛 RuntimeError。"""
        with pytest.raises(RuntimeError, match="full_zip_url"):
            mineru_client.download_result({})

    @patch('core.mineru_client.requests.get')
    def test_download_result_http_error_raises(self, mock_get, mineru_cfg):
        """下载 HTTP 失败时抛 RuntimeError。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="下载 zip 失败"):
            mineru_client.download_result({"full_zip_url": "https://zip.test/404"})

    @patch('core.mineru_client.requests.get')
    def test_download_result_empty_zip_raises(self, mock_get, mineru_cfg):
        """zip 中无 content_list.json 也无 full.md 时抛 RuntimeError。"""
        zip_bytes = _make_zip_bytes({"other.txt": "hello"})
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = zip_bytes
        mock_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="未找到"):
            mineru_client.download_result({"full_zip_url": "https://zip.test/1"})

    @patch('core.mineru_client.requests.get')
    def test_download_result_invalid_content_list_raises(self, mock_get, mineru_cfg):
        """content_list.json 不是有效 JSON 时抛 RuntimeError。"""
        zip_bytes = _make_zip_bytes({
            "x_content_list.json": "not a valid json {{{",
        })
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = zip_bytes
        mock_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="content_list.json 解析失败"):
            mineru_client.download_result({"full_zip_url": "https://zip.test/1"})

    @patch('core.mineru_client.requests.get')
    def test_download_result_v1_discarded_counted(self, mock_get, mineru_cfg):
        """v1 格式：discarded 块不进入正文，但页 dict 暴露 _discarded_count。"""
        content_list = [
            {"page_idx": 0, "type": "text", "text": "正常文本"},
            {"page_idx": 0, "type": "discarded", "text": "被丢弃的残缺片段"},
            {"page_idx": 0, "type": "discarded", "text": "另一块被丢弃内容"},
        ]
        zip_bytes = _make_zip_bytes({
            "full.md": "# 全文",
            "abc_content_list.json": json.dumps(content_list),
        })
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = zip_bytes
        mock_get.return_value = mock_resp

        pages = mineru_client.download_result({"full_zip_url": "https://zip.test/disc1"})

        assert len(pages) == 1
        text = pages[0]["markdown"]["text"]
        assert "正常文本" in text
        assert "被丢弃" not in text
        assert pages[0]["_discarded_count"] == 2

    @patch('core.mineru_client.requests.get')
    def test_download_result_v2_discarded_counted(self, mock_get, mineru_cfg):
        """v2 格式：discard 标记块计数，无丢弃时不含 _discarded_count 字段。"""
        content_list_v2 = [
            [
                {"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "保留内容"}]}},
                {"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "低置信度"}]}, "discard": True},
            ],
        ]
        zip_bytes = _make_zip_bytes({
            "full.md": "# 全文",
            "abc_content_list_v2.json": json.dumps(content_list_v2),
        })
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = zip_bytes
        mock_get.return_value = mock_resp

        pages = mineru_client.download_result({"full_zip_url": "https://zip.test/disc2"})

        assert len(pages) == 1
        text = pages[0]["markdown"]["text"]
        assert "保留内容" in text
        assert "低置信度" not in text
        assert pages[0]["_discarded_count"] == 1

        # 无丢弃块时不应出现 _discarded_count（避免下游误判不完整）
        zip_bytes2 = _make_zip_bytes({
            "full.md": "# 全文",
            "abc_content_list_v2.json": json.dumps([[{"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "干净页"}]}}]]),
        })
        mock_resp2 = MagicMock()
        mock_resp2.status_code = 200
        mock_resp2.content = zip_bytes2
        mock_get.return_value = mock_resp2

        pages2 = mineru_client.download_result({"full_zip_url": "https://zip.test/disc3"})
        assert "_discarded_count" not in pages2[0]


class TestMinerUBlockToMarkdown:
    """MinerU _block_to_markdown — 块类型转 Markdown。"""

    def test_text_block(self):
        """text 块返回纯文本（去除首尾空白）。"""
        assert mineru_client._block_to_markdown(
            {"type": "text", "text": "  hello  "}
        ) == "hello"

    def test_text_block_empty(self):
        """text 块无 text 字段时返回空字符串。"""
        assert mineru_client._block_to_markdown({"type": "text"}) == ""

    def test_table_block_markdown_field(self):
        """table 块优先返回 markdown 字段。"""
        result = mineru_client._block_to_markdown({
            "type": "table", "markdown": "| a | b |", "text": "fallback"
        })
        assert result == "| a | b |"

    def test_table_block_fallback_to_text(self):
        """table 块无 HTML/markdown 时返回空（宁缺毋滥，避免纯文本表格丢失结构）。"""
        assert mineru_client._block_to_markdown(
            {"type": "table", "text": "fallback"}
        ) == ""

    def test_table_block_v1_table_body_html(self):
        """v1 table 块从 table_body 取 HTML（page_analyzer 的 prompt 按 HTML 表格设计）。"""
        assert mineru_client._block_to_markdown(
            {"type": "table", "table_body": "<table><tr><td>浓度</td></tr></table>"}
        ) == "<table><tr><td>浓度</td></tr></table>"

    def test_table_block_v2_content_html(self):
        """v2 table 块从 content.html 取 HTML。"""
        assert mineru_client._block_to_markdown(
            {"type": "table", "content": {"html": "<table><tr><td>a</td></tr></table>"}}
        ) == "<table><tr><td>a</td></tr></table>"

    def test_table_block_both_empty(self):
        """table 块无 markdown 和 text 时返回空字符串。"""
        assert mineru_client._block_to_markdown({"type": "table"}) == ""

    def test_image_block_with_caption(self):
        """image 块有 caption 时返回占位符。"""
        assert mineru_client._block_to_markdown(
            {"type": "image", "text": "图1"}
        ) == "[image: 图1]"

    def test_image_block_no_caption(self):
        """image 块无 caption 时返回 [image] 占位符（让 LLM 知道此处有图）。"""
        assert mineru_client._block_to_markdown({"type": "image"}) == "[image]"

    def test_equation_block_with_caption(self):
        """equation 块有 caption 时返回占位符。"""
        assert mineru_client._block_to_markdown(
            {"type": "equation", "caption": "公式1"}
        ) == "[equation: 公式1]"

    def test_equation_block_text_as_caption(self):
        """equation 块无 caption 时用 text 作为 caption。"""
        assert mineru_client._block_to_markdown(
            {"type": "equation", "text": "E=mc2"}
        ) == "[equation: E=mc2]"

    def test_unknown_type_falls_back_to_text(self):
        """未知类型回退到 text 字段。"""
        assert mineru_client._block_to_markdown(
            {"type": "other", "text": "  x  "}
        ) == "x"

    def test_default_type_is_text(self):
        """无 type 字段时按 text 处理。"""
        assert mineru_client._block_to_markdown({"text": "default"}) == "default"


class TestMinerURunOCR:
    """MinerU run_ocr — 端到端编排。"""

    @patch('core.mineru_client.download_result')
    @patch('core.mineru_client.poll_job')
    @patch('core.mineru_client.submit_pdf')
    def test_run_ocr_end_to_end(self, mock_submit, mock_poll, mock_download, fake_pdf, mineru_cfg):
        """run_ocr 串联 submit → poll → download，返回页面列表。"""
        mock_submit.return_value = ("batch-e2e", "sample.pdf")
        mock_poll.return_value = {
            "task_id": "t1",
            "state": "done",
            "full_zip_url": "https://zip.test/e2e",
        }
        mock_download.return_value = [
            {"markdown": {"text": "第1页"}, "page_count": 1, "_source": "mineru"},
            {"markdown": {"text": "第2页"}, "page_count": 2, "_source": "mineru"},
        ]

        pages = mineru_client.run_ocr(fake_pdf)

        assert isinstance(pages, list)
        assert len(pages) == 2
        assert pages[0]["markdown"]["text"] == "第1页"
        assert pages[0]["_source"] == "mineru"
        assert pages[1]["markdown"]["text"] == "第2页"

        mock_submit.assert_called_once_with(fake_pdf)
        mock_poll.assert_called_once_with("batch-e2e", progress_callback=None)
        mock_download.assert_called_once_with({
            "task_id": "t1",
            "state": "done",
            "full_zip_url": "https://zip.test/e2e",
        })

    @patch('core.mineru_client.download_result')
    @patch('core.mineru_client.poll_job')
    @patch('core.mineru_client.submit_pdf')
    def test_run_ocr_propagates_submit_error(self, mock_submit, mock_poll, mock_download, fake_pdf, mineru_cfg):
        """submit_pdf 失败时 run_ocr 向上抛异常。"""
        mock_submit.side_effect = RuntimeError("MinerU token 未配置")
        with pytest.raises(RuntimeError, match="MinerU token 未配置"):
            mineru_client.run_ocr(fake_pdf)
        mock_poll.assert_not_called()
        mock_download.assert_not_called()

    @patch('core.mineru_client.download_result')
    @patch('core.mineru_client.poll_job')
    @patch('core.mineru_client.submit_pdf')
    def test_run_ocr_propagates_poll_error(self, mock_submit, mock_poll, mock_download, fake_pdf, mineru_cfg):
        """poll_job 失败时 run_ocr 向上抛异常。"""
        mock_submit.return_value = ("batch-x", "sample.pdf")
        mock_poll.side_effect = RuntimeError("[MinerU] 解析失败 task=t1: error")
        with pytest.raises(RuntimeError, match="解析失败"):
            mineru_client.run_ocr(fake_pdf)
        mock_download.assert_not_called()


# ---------------------------------------------------------------------------
# MinerU 分片 OCR（core/mineru_client.py: split_pdf / cleanup_slices / run_ocr_sliced）
# ---------------------------------------------------------------------------

@pytest.fixture
def real_pdf(tmp_path):
    """用 PyMuPDF 生成真实 3 页 PDF（split_pdf 需要 fitz 可解析）。"""
    import fitz

    doc = fitz.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"page {i + 1}")
    out = tmp_path / "real.pdf"
    doc.save(out)
    doc.close()
    return str(out)


class TestMinerUSplitPdf:
    """split_pdf 按 slice_pages 拆片，返回 (total, [(start_1based, path), ...])。"""

    def test_split_2_slices(self, real_pdf, tmp_path):
        total, slices = mineru_client.split_pdf(real_pdf, 2)
        assert total == 3
        assert [s for s, _ in slices] == [1, 3]
        for _, p in slices:
            assert Path(p).exists()
            assert Path(p).parent.name.startswith("pbc_slice_")  # 系统临时目录下
        mineru_client.cleanup_slices(slices)
        for _, p in slices:
            assert not Path(p).exists()

    def test_split_single_slice_when_slice_pages_ge_total(self, real_pdf):
        total, slices = mineru_client.split_pdf(real_pdf, 99)
        assert total == 3
        assert len(slices) == 1
        assert slices[0][0] == 1
        mineru_client.cleanup_slices(slices)

    def test_cleanup_slices_ignores_missing(self, real_pdf):
        _, slices = mineru_client.split_pdf(real_pdf, 2)
        mineru_client.cleanup_slices([(1, str(real_pdf))])
        assert Path(slices[0][1]).exists()  # 只清理传入的路径
        mineru_client.cleanup_slices(slices)


class TestMinerURunOcrSliced:
    """run_ocr_sliced 并行提交/轮询/下载，一片完成即回调 on_batch。"""

    def _mock_chain(self, mock_submit, mock_poll, mock_download):
        mock_submit.side_effect = [("batch-1", "a.pdf"), ("batch-2", "b.pdf")]

        def fake_poll(batch_id, progress_callback=None, **kwargs):
            if progress_callback:
                progress_callback(2, 2)
            return {"full_zip_url": f"https://zip.test/{batch_id}"}

        mock_poll.side_effect = fake_poll
        mock_download.side_effect = [
            [
                {"markdown": {"text": "s1p1"}, "page_count": 1},
                {"markdown": {"text": "s1p2"}, "page_count": 2},
            ],
            [{"markdown": {"text": "s2p1"}, "page_count": 1}],
        ]

    @patch('core.mineru_client.cleanup_slices')
    @patch('core.mineru_client.download_result')
    @patch('core.mineru_client.poll_job')
    @patch('core.mineru_client.submit_pdf')
    def test_on_batch_remaps_global_page_numbers(
        self, mock_submit, mock_poll, mock_download, mock_cleanup, real_pdf, mineru_cfg
    ):
        """page_count 重映射为全局页号；on_batch 每片回调一次；进度合并为全局。"""
        self._mock_chain(mock_submit, mock_poll, mock_download)
        batches, progress = [], []

        results = mineru_client.run_ocr_sliced(
            real_pdf,
            2,
            on_batch=lambda start, pages: batches.append((start, pages)),
            progress_callback=lambda done, total: progress.append((done, total)),
        )

        assert results[0][0] == 1 and results[1][0] == 3
        assert [b[0] for b in batches] == [1, 3]
        pages1 = batches[0][1]
        assert pages1[0]["page_count"] == 1 and pages1[1]["page_count"] == 2
        pages2 = batches[1][1]
        assert pages2[0]["page_count"] == 3
        # 进度回调合并为全局：total 恒为文档总页数，done 单调递增
        assert all(t == 3 for _, t in progress)
        dones = [d for d, _ in progress]
        assert dones == sorted(dones)
        mock_cleanup.assert_called_once()

    @patch('core.mineru_client.cleanup_slices')
    @patch('core.mineru_client.download_result')
    @patch('core.mineru_client.poll_job')
    @patch('core.mineru_client.submit_pdf')
    def test_slice_error_is_contained_and_cleans_up(
        self, mock_submit, mock_poll, mock_download, mock_cleanup, real_pdf, mineru_cfg
    ):
        """P1-1: 单片失败不再上抛（整单 error）— 记录并继续等待其余片，
        成功片照常回调 on_batch，finally 清理切片。"""
        self._mock_chain(mock_submit, mock_poll, mock_download)
        mock_download.side_effect = [
            RuntimeError("download boom"),
            [{"markdown": {"text": "s2p1"}, "page_count": 1}],
        ]
        batches = []

        results = mineru_client.run_ocr_sliced(
            real_pdf,
            2,
            on_batch=lambda start, pages: batches.append((start, pages)),
        )

        # 失败片不阻塞：正常片（start=3）仍回调并返回（page_count 已重映射为全局 3）
        assert [b[0] for b in batches] == [3]
        assert results[1] == (3, [{"markdown": {"text": "s2p1"}, "page_count": 3}])
        mock_cleanup.assert_called_once()
        mock_poll.assert_called()
        mock_submit.assert_called()

    @patch('core.mineru_client.cleanup_slices')
    @patch('core.mineru_client.download_result')
    @patch('core.mineru_client.poll_job')
    @patch('core.mineru_client.submit_pdf')
    def test_sliced_calls_submit_poll_download_per_slice(
        self, mock_submit, mock_poll, mock_download, mock_cleanup, real_pdf, mineru_cfg
    ):
        """每片走 submit → poll → download 完整链路。"""
        self._mock_chain(mock_submit, mock_poll, mock_download)

        mineru_client.run_ocr_sliced(real_pdf, 2)

        assert mock_submit.call_count == 2
        assert mock_poll.call_count == 2
        assert mock_download.call_count == 2
