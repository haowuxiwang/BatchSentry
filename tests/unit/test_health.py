"""Tests for core/health.py downstream service probes.

Covers:
- probe_paddle_ocr: configured / not configured / connection error
- probe_mineru: token configured / missing
- probe_llm: success / failure
- probe_all: parallel aggregation
- /api/health/downstream endpoint
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_probe_paddle_ocr_not_configured():
    """Returns ok=False when API URL or token is missing."""
    from config import config as _cfg
    orig_url = _cfg["paddle_ocr"].api_url
    orig_token = _cfg["paddle_ocr"].token
    _cfg["paddle_ocr"].api_url = ""
    _cfg["paddle_ocr"].token = ""
    try:
        from core.health import probe_paddle_ocr
        result = probe_paddle_ocr()
        assert result["ok"] is False
        assert "not configured" in result["reason"].lower()
    finally:
        _cfg["paddle_ocr"].api_url = orig_url
        _cfg["paddle_ocr"].token = orig_token


@pytest.mark.asyncio
async def test_probe_paddle_ocr_connection_error():
    """Returns ok=False on connection error."""
    from config import config as _cfg
    orig_url = _cfg["paddle_ocr"].api_url
    orig_token = _cfg["paddle_ocr"].token
    _cfg["paddle_ocr"].api_url = "http://nonexistent.invalid.localhost:9999/api"
    _cfg["paddle_ocr"].token = "fake-token"
    try:
        from core.health import probe_paddle_ocr
        result = probe_paddle_ocr()
        assert result["ok"] is False
        assert "reason" in result
    finally:
        _cfg["paddle_ocr"].api_url = orig_url
        _cfg["paddle_ocr"].token = orig_token


@pytest.mark.asyncio
async def test_probe_paddle_ocr_reachable():
    """Returns ok=True when service responds with <500 status."""
    from config import config as _cfg
    orig_url = _cfg["paddle_ocr"].api_url
    orig_token = _cfg["paddle_ocr"].token
    _cfg["paddle_ocr"].api_url = "http://example.test/api"
    _cfg["paddle_ocr"].token = "fake-token"
    try:
        from core.health import probe_paddle_ocr
        # Mock requests.request to return a 405 (Method Not Allowed) — still proves reachability
        mock_resp = MagicMock()
        mock_resp.status_code = 405
        with patch("core.health.requests.request", return_value=mock_resp):
            result = probe_paddle_ocr()
        assert result["ok"] is True
        assert result["status"] == 405
        assert "latency_ms" in result
    finally:
        _cfg["paddle_ocr"].api_url = orig_url
        _cfg["paddle_ocr"].token = orig_token


@pytest.mark.asyncio
async def test_probe_mineru_no_token():
    """Returns ok=False when MINERU_TOKEN not configured."""
    from config import config as _cfg
    orig = _cfg["mineru"].token
    _cfg["mineru"].token = ""
    try:
        from core.health import probe_mineru
        result = probe_mineru()
        assert result["ok"] is False
        assert "not configured" in result["reason"].lower()
    finally:
        _cfg["mineru"].token = orig


@pytest.mark.asyncio
async def test_probe_mineru_configured():
    """Returns ok=True when token is configured."""
    from config import config as _cfg
    orig = _cfg["mineru"].token
    _cfg["mineru"].token = "fake-mineru-token"
    try:
        from core.health import probe_mineru
        result = probe_mineru()
        assert result["ok"] is True
    finally:
        _cfg["mineru"].token = orig


@pytest.mark.asyncio
async def test_probe_llm_success():
    """Returns ok=True with latency when LLM responds.

    Probe goes through client.adapter.chat() (not the raw SDK client) so it
    works for both OpenAI and Anthropic protocols. Mock the adapter directly.
    """
    mock_adapter = MagicMock()
    mock_adapter.chat = AsyncMock(return_value=MagicMock(content="ok"))
    mock_adapter.client_info = MagicMock(return_value={"protocol": "openai"})

    with patch("core.health.get_llm_client", return_value=MagicMock(
        adapter=mock_adapter, model="test-model", provider="test"
    )):
        from core.health import probe_llm
        result = await probe_llm()
    assert result["ok"] is True
    assert result["model"] == "test-model"
    assert result["provider"] == "test"
    assert result["protocol"] == "openai"
    assert "latency_ms" in result


@pytest.mark.asyncio
async def test_probe_llm_failure():
    """Returns ok=False with reason on exception."""
    mock_adapter = MagicMock()
    mock_adapter.chat = AsyncMock(side_effect=Exception("Auth failed"))
    mock_adapter.client_info = MagicMock(return_value={"protocol": "openai"})

    with patch("core.health.get_llm_client", return_value=MagicMock(
        adapter=mock_adapter, model="test-model", provider="test"
    )):
        from core.health import probe_llm
        result = await probe_llm()
    assert result["ok"] is False
    assert "Auth failed" in result["reason"]


@pytest.mark.asyncio
async def test_probe_all_aggregation():
    """probe_all runs OCR + LLM probes in parallel and aggregates."""
    from config import config as _cfg
    orig_backend = _cfg["app"].ocr_backend
    _cfg["app"].ocr_backend = "paddle"  # 固定分支：测试假设 paddle 维度的断言
    try:
        with patch("core.health.probe_paddle_ocr", return_value={"ok": True, "latency_ms": 50, "reason": ""}):
            with patch("core.health.probe_llm", new_callable=AsyncMock, return_value={"ok": True, "latency_ms": 100, "reason": "", "model": "m", "provider": "p"}):
                from core.health import probe_all
                result = await probe_all()
    finally:
        _cfg["app"].ocr_backend = orig_backend
    assert result["ocr_backend"] == "paddle"
    assert result["ocr"]["ok"] is True
    assert result["llm"]["ok"] is True
    assert result["all_ok"] is True


@pytest.mark.asyncio
async def test_probe_all_partial_failure():
    """probe_all returns all_ok=False when one service fails."""
    from config import config as _cfg
    orig_backend = _cfg["app"].ocr_backend
    _cfg["app"].ocr_backend = "paddle"
    try:
        with patch("core.health.probe_paddle_ocr", return_value={"ok": False, "reason": "timeout"}):
            with patch("core.health.probe_llm", new_callable=AsyncMock, return_value={"ok": True, "reason": "", "model": "m", "provider": "p"}):
                from core.health import probe_all
                result = await probe_all()
    finally:
        _cfg["app"].ocr_backend = orig_backend
    assert result["all_ok"] is False
    assert result["ocr"]["ok"] is False
    assert result["llm"]["ok"] is True


@pytest.mark.asyncio
async def test_health_downstream_endpoint(test_client):
    """GET /api/health/downstream returns aggregated probe result."""
    with patch("core.health.probe_all", new_callable=AsyncMock, return_value={
        "ocr_backend": "paddle",
        "ocr": {"ok": True, "latency_ms": 50, "reason": ""},
        "llm": {"ok": True, "latency_ms": 100, "reason": "", "model": "m", "provider": "p"},
        "all_ok": True,
    }):
        r = await test_client.get("/api/health/downstream")
    assert r.status_code == 200
    data = r.json()
    assert data["all_ok"] is True
    assert data["ocr"]["ok"] is True
    assert data["llm"]["ok"] is True
    assert data["ocr_backend"] == "paddle"


@pytest.mark.asyncio
async def test_health_downstream_rejects_non_local_host(test_client):
    """对抗审查：downstream 探测有副作用的成本（token 配额），
    非本地来源（Host 非本机/内网）请求必须 403。"""
    from httpx import ASGITransport, AsyncClient
    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://evil.com:8000",
    ) as client:
        r = await client.get("/api/health/downstream")
    assert r.status_code == 403
