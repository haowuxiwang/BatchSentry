"""core/ocr_client.py 边界分支补测（M1b）。

补全 test_ocr_client.py 未覆盖的：
- poll_job HTTP 非 200 重试 / 连续 5 次快速失败 / 网络错误 5 次快速失败
  / 轮询超时出口
- _ensure_page_text 的 markdown 非 dict 跳过；_persist_paddle_original 落盘失败旁路
- download_result：网络异常、resultUrl 形态（dict.url / str）、
  空 layoutParsingResults、dataInfo 补页码、JSONL 路径
  （BOM 剥离 / 空行跳过 / 坏行占位 / dataInfo 补页码 / 全空抛错）
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from core import ocr_client


def _resp(status=200, *, text="", payload=None):
    r = MagicMock()
    r.status_code = status
    r.text = text
    if payload is not None:
        r.json.return_value = payload
    return r


# ── poll_job ────────────────────────────────────────────────────────────────


class TestPollJobBranches:
    def test_http_error_then_success_retries(self):
        """一次 HTTP 500 → 退避重试 → 第二次 200 完成（覆盖 149-154）。"""
        r500 = _resp(500, text="gateway boom")
        r200 = _resp(200, payload={"data": {"state": "done",
                                            "extractProgress": {"extractedPages": 3, "total": 3}}})
        with patch("core.ocr_client.requests.get", side_effect=[r500, r200]), \
             patch("core.ocr_client.time.sleep"):
            j = ocr_client.poll_job("job-1")
        assert j["data"]["state"] == "done"

    def test_http_error_five_times_fast_fails(self):
        """连续 5 次 HTTP 非 200 → 快速失败（不空转满超时）。"""
        with patch("core.ocr_client.requests.get", return_value=_resp(503, text="x")), \
             patch("core.ocr_client.time.sleep"):
            with pytest.raises(RuntimeError, match="连续 5 次 HTTP 状态异常"):
                ocr_client.poll_job("job-2")

    def test_network_error_five_times_fast_fails(self):
        """连续 5 次网络异常 → 快速失败（覆盖 189-197 及超时出口以上的重试）。"""
        with patch("core.ocr_client.requests.get",
                   side_effect=requests.exceptions.ConnectionError("reset")), \
             patch("core.ocr_client.time.sleep"):
            with pytest.raises(RuntimeError, match="连续 5 次网络错误"):
                ocr_client.poll_job("job-3")

    def test_poll_timeout_raises(self):
        """超时出口：timeout_s 预算耗尽且未 done → RuntimeError（覆盖 196-199）。"""
        with patch("core.ocr_client.POLL_TIMEOUT", -1):
            with pytest.raises(RuntimeError, match="轮询超时"):
                ocr_client.poll_job("job-4")

    def test_cancel_check_raises_ocr_cancelled(self):
        """cancel_check 返回 True → OCRCancelled（failover 链须放行）。"""
        with pytest.raises(ocr_client.OCRCancelled):
            ocr_client.poll_job("job-5", cancel_check=lambda: True)


# ── _ensure_page_text / _persist_paddle_original ────────────────────────────


class TestEnsurePageText:
    def test_markdown_not_dict_skipped(self):
        """markdown 非 dict → continue，不改写（覆盖 241）。"""
        pages = [{"markdown": "raw-string"}]
        ocr_client._ensure_page_text(pages)
        assert pages[0]["markdown"] == "raw-string"

    def test_short_text_rebuilt_from_block_list(self):
        """markdown.text 过短 → 由 parsing_res_list 组装兜底并打降级标记。"""
        pages = [{
            "markdown": {"text": ""},
            "prunedResult": {"parsing_res_list": [
                {"text": "设备编码 EQ-01"},
                {"content": "批号 112701 含量 99.2%"},
            ]},
        }]
        ocr_client._ensure_page_text(pages)
        rebuilt = pages[0]["markdown"]["text"]
        assert "块级文本兜底" in rebuilt
        assert "设备编码 EQ-01" in rebuilt
        assert "含量 99.2%" in rebuilt

    def test_long_text_untouched(self):
        """markdown.text 已足够长 → 保持原样。"""
        original = "正文内容" * 10
        pages = [{"markdown": {"text": original}}]
        ocr_client._ensure_page_text(pages)
        assert pages[0]["markdown"]["text"] == original


class TestPersistPaddleOriginal:
    def test_write_failure_is_swallowed(self, tmp_path):
        """落盘失败仅告警不阻断主流程（覆盖 277-280）。"""
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        with patch("core.ocr_client.os.replace", side_effect=OSError("read-only fs")):
            ocr_client._persist_paddle_original('{"a": 1}', str(pdf), "json")  # 不抛

    def test_empty_inputs_noop(self, tmp_path):
        pdf = tmp_path / "x.pdf"
        ocr_client._persist_paddle_original("", str(pdf), "json")
        assert not (tmp_path / "paddle_original.json").exists()


# ── download_result ─────────────────────────────────────────────────────────


class TestDownloadResultBranches:
    def test_missing_result_url_raises(self):
        with pytest.raises(RuntimeError, match="No result URL"):
            ocr_client.download_result({"data": {}})

    def test_network_error_wrapped(self):
        """下载网络异常 → RuntimeError（脱敏，覆盖 310-311）。"""
        with patch("core.ocr_client.requests.get",
                   side_effect=requests.exceptions.Timeout("slow")):
            with pytest.raises(RuntimeError, match="Download network error"):
                ocr_client.download_result(
                    {"data": {"resultUrl": {"jsonUrl": "http://cdn/x.json"}}}
                )

    def test_http_non_200_raises(self):
        with patch("core.ocr_client.requests.get", return_value=_resp(404)):
            with pytest.raises(RuntimeError, match="Download failed HTTP 404"):
                ocr_client.download_result({"data": {"resultUrl": "http://cdn/x.json"}})

    def test_empty_layout_results_raises(self):
        """JSON 合法但 layoutParsingResults 为空 → 抛错（覆盖 327-337）。"""
        r = _resp(200, text=json.dumps({"result": {"layoutParsingResults": []}}))
        with patch("core.ocr_client.requests.get", return_value=r):
            with pytest.raises(RuntimeError, match="layoutParsingResults 为空"):
                ocr_client.download_result({"data": {"resultUrl": "http://cdn/x.json"}})

    def test_result_url_dict_url_key(self):
        """resultUrl 为 dict 且只有 url 键 → 也能取到（覆盖 296 的 url 分支）。"""
        payload = {"result": {"layoutParsingResults": [{"markdown": {"text": "内容" * 15}}]}}
        r = _resp(200, text=json.dumps(payload))
        with patch("core.ocr_client.requests.get", return_value=r):
            pages = ocr_client.download_result(
                {"data": {"resultUrl": {"url": "http://cdn/x.json"}}}
            )
        assert len(pages) == 1
        assert pages[0]["source"] == "paddle"

    def test_data_info_backfills_page_count(self):
        """单 JSON 且 dataInfo 非空 → 缺 page_count 的页补序号（覆盖 341-343）。"""
        payload = {"result": {
            "layoutParsingResults": [
                {"markdown": {"text": "第一页内容" * 5}},
                {"markdown": {"text": "第二页内容" * 5}},
            ],
            "dataInfo": {"pages": 2},
        }}
        r = _resp(200, text=json.dumps(payload))
        with patch("core.ocr_client.requests.get", return_value=r):
            pages = ocr_client.download_result({"data": {"resultUrl": "http://cdn/x.json"}})
        assert [p["page_count"] for p in pages] == [1, 2]


class TestDownloadResultJsonl:
    def _get(self, text):
        return patch("core.ocr_client.requests.get", return_value=_resp(200, text=text))

    def test_bom_and_empty_and_bad_lines(self):
        """BOM 剥离 + 空行跳过 + 坏行插 4 占位 + 合法行解析（覆盖 360-382,389-391）。"""
        good = json.dumps({
            "result": {
                "layoutParsingResults": [{"markdown": {"text": "第五页正文内容" * 3}}],
                "dataInfo": {"pages": 5},
            }
        })
        raw = "\ufeff{bad json line}\n\n" + good + "\n"
        with self._get(raw):
            pages = ocr_client.download_result({"data": {"resultUrl": "http://cdn/x.jsonl"}})
        # 坏行 = 4 个空占位 + 合法行 1 页
        assert len(pages) == 5
        assert pages[0]["markdown"]["text"] == ""
        assert "第五页正文内容" in pages[4]["markdown"]["text"]

    def test_jsonl_zero_pages_raises(self):
        """JSONL 全部行合法但无 layoutParsingResults → 抛错（覆盖 396-403）。"""
        with self._get('{"a": 1}\n{"b": 2}'):
            with pytest.raises(RuntimeError, match="0 页"):
                ocr_client.download_result({"data": {"resultUrl": "http://cdn/x.jsonl"}})


# ── run_ocr ─────────────────────────────────────────────────────────────────


class TestRunOcrBranches:
    def test_run_ocr_no_job_id_skips_context(self, tmp_path):
        """无 job_id → 不设 ContextVar，正常返回（覆盖 finally 的 False 分支）。"""
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        with patch.object(ocr_client, "submit_pdf", return_value="pj"), \
             patch.object(ocr_client, "poll_job", return_value={"data": {}}), \
             patch.object(ocr_client, "download_result", return_value=[{"page": 1}]):
            out = ocr_client.run_ocr(str(pdf))
        assert out == [{"page": 1}]

    def test_run_ocr_zero_pages_defensive(self, tmp_path):
        """download_result 返回空 → 防御性告警，仍返回空列表。"""
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        with patch.object(ocr_client, "submit_pdf", return_value="pj"), \
             patch.object(ocr_client, "poll_job", return_value={"data": {}}), \
             patch.object(ocr_client, "download_result", return_value=[]):
            out = ocr_client.run_ocr(str(pdf), job_id="j-1")
        assert out == []
