"""core/health.py · core/security.py · core/procpool.py 边界分支补测（M1b）。

补全既有测试未覆盖的异常/防御分支：
- probe_paddle_ocr: 探测超时 / 非 requests 的通用异常
- probe_llm: 未配置 API Key 短路 / 双重失败回落 "?"
- _parse_host_as_ip: 非法八进制 / 带符号非法字面量
- is_local_request: Origin 解析异常
- redact_urls: URL 解析异常兜底
- run_cpu: 进程池提交期 pickling 失败 → 回退线程
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from core.health import probe_llm, probe_paddle_ocr
from core.security import _parse_host_as_ip, is_local_request, redact_urls


# ── core/health.py ──────────────────────────────────────────────────────────


class TestProbePaddleBranches:
    def _configured(self, cfg):
        cfg["paddle_ocr"].api_url = "http://example.test/api"
        cfg["paddle_ocr"].token = "fake-token"

    def test_probe_timeout(self):
        from config import config as cfg
        orig_url, orig_token = cfg["paddle_ocr"].api_url, cfg["paddle_ocr"].token
        self._configured(cfg)
        try:
            with patch("core.health.requests.request",
                       side_effect=requests.exceptions.Timeout("t")):
                r = probe_paddle_ocr()
            assert r["ok"] is False
            assert "超时" in r["reason"]
        finally:
            cfg["paddle_ocr"].api_url, cfg["paddle_ocr"].token = orig_url, orig_token

    def test_probe_unexpected_exception(self):
        from config import config as cfg
        orig_url, orig_token = cfg["paddle_ocr"].api_url, cfg["paddle_ocr"].token
        self._configured(cfg)
        try:
            with patch("core.health.requests.request", side_effect=ValueError("weird")):
                r = probe_paddle_ocr()
            assert r["ok"] is False
            assert "ValueError" in r["reason"]
        finally:
            cfg["paddle_ocr"].api_url, cfg["paddle_ocr"].token = orig_url, orig_token


class TestProbeLlmBranches:
    @pytest.mark.asyncio
    async def test_not_configured_short_circuits(self):
        adapter = MagicMock()
        adapter.client_info.return_value = {"protocol": "openai"}
        adapter.is_configured = False
        client = MagicMock(adapter=adapter, model="m1", provider="deepseek")
        with patch("core.health.get_llm_client", return_value=client):
            r = await probe_llm()
        assert r["ok"] is False
        assert r["reason"] == "未配置 API Key"
        assert r["provider"] == "deepseek"

    @pytest.mark.asyncio
    async def test_double_failure_reports_unknown(self):
        """首次取 client 失败、兜底再取也失败 → model/provider 回落 "?"。"""
        with patch("core.health.get_llm_client", side_effect=RuntimeError("boom")):
            r = await probe_llm()
        assert r["ok"] is False
        assert r["model"] == "?" and r["provider"] == "?"


# ── core/security.py ────────────────────────────────────────────────────────


class TestParseHostAsIpBranches:
    def test_legacy_octal_invalid_returns_none(self):
        """0 前缀纯数字但含 8/9 → int(,0) 与 int(,8) 均失败 → None。"""
        assert _parse_host_as_ip("08") is None

    def test_signed_literal_rejected(self):
        """int(,0) 接受 +5，但带符号形态必须拒绝。"""
        assert _parse_host_as_ip("+5") is None


class TestIsLocalRequestBranches:
    class _Req:
        def __init__(self, headers):
            self.headers = headers

    def test_origin_parse_failure_rejected(self):
        req = self._Req({"host": "localhost:8000", "origin": "http://broken"})
        with patch("urllib.parse.urlparse", side_effect=ValueError("bad")):
            assert is_local_request(req) is False


class TestRedactUrlsBranches:
    def test_urlsplit_failure_falls_back(self):
        with patch("urllib.parse.urlsplit", side_effect=ValueError("bad")):
            assert redact_urls("see https://x/y?z=1 now") == "see <redacted-url> now"


# ── core/procpool.py ────────────────────────────────────────────────────────


class TestRunCpuFallback:
    @pytest.mark.asyncio
    async def test_submit_pickle_failure_falls_back_to_thread(self, monkeypatch):
        """进程池提交期 TypeError → 回退线程执行，结果不丢。"""
        import core.procpool as pp

        monkeypatch.setattr(pp, "_get_pool", lambda: object())

        class _FakeLoop:
            async def run_in_executor(self, *a, **k):
                raise TypeError("unpicklable")

        monkeypatch.setattr(pp.asyncio, "get_running_loop", lambda: _FakeLoop())
        assert await pp.run_cpu(len, "abcd") == 4
