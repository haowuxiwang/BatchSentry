"""P1-7 结构化输出（json_object 模式）测试。

覆盖：
- LLM_JSON_MODE 关闭（默认）→ 不传 response_format
- 开启 + openai 协议 → chat_json 传 {"type": "json_object"}
- 网关 400 拒绝 → 自动降级重试一次 + 会话级禁用
- anthropic 协议不传（无等价参数）
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import llm.client as client_mod
from config import config
from llm.adapters.base import ChatResult
from llm.client import LLMClient, _looks_like_rf_unsupported


class _FakeAdapter:
    """记录 chat() 调用参数的最小适配器。"""

    def __init__(self, protocol="openai", fail_with_rf=False):
        self.protocol = protocol
        self.calls = []
        self.fail_with_rf = fail_with_rf

    async def chat(self, system_prompt="", user_content="", max_tokens=4000,
                   temperature=0.1, timeout=180.0, response_format=None,
                   **kwargs):
        self.calls.append({"response_format": response_format})
        if response_format and self.fail_with_rf:
            raise RuntimeError(
                "Error code: 400 - Unrecognized request argument "
                "supplied: response_format"
            )
        return ChatResult(content='{"ok": true}', model="test-model")

    def client_info(self):
        return {}


@pytest.fixture(autouse=True)
def _reset_json_mode_flag():
    LLMClient._json_mode_disabled.clear()
    yield
    LLMClient._json_mode_disabled.clear()


def _make_client(monkeypatch_adapter):
    c = LLMClient.__new__(LLMClient)
    c.provider = "test"
    c.adapter = monkeypatch_adapter
    c.model = "test-model"
    return c


class TestJsonMode:
    def test_rf_unsupported_detector(self):
        assert _looks_like_rf_unsupported(
            RuntimeError("400 Bad Request: response_format is not supported")
        )
        assert _looks_like_rf_unsupported(
            RuntimeError("Unrecognized request argument: json_object")
        )
        # 无关错误不误判
        assert not _looks_like_rf_unsupported(RuntimeError("connection reset"))
        assert not _looks_like_rf_unsupported(
            RuntimeError("401 unauthorized")
        )

    @pytest.mark.asyncio
    async def test_json_mode_off_no_response_format(self):
        orig = config["app"].llm_json_mode
        config["app"].llm_json_mode = False
        adapter = _FakeAdapter()
        try:
            c = _make_client(adapter)
            await c.chat_json("sys", "user")
            assert adapter.calls[0]["response_format"] is None
        finally:
            config["app"].llm_json_mode = orig

    @pytest.mark.asyncio
    async def test_json_mode_on_passes_response_format(self):
        orig = config["app"].llm_json_mode
        config["app"].llm_json_mode = True
        adapter = _FakeAdapter()
        try:
            c = _make_client(adapter)
            result = await c.chat_json("sys", "user")
            assert result == {"ok": True}
            assert adapter.calls[0]["response_format"] == {"type": "json_object"}
        finally:
            config["app"].llm_json_mode = orig

    @pytest.mark.asyncio
    async def test_gateway_rejection_degrades_and_disables(self):
        orig = config["app"].llm_json_mode
        config["app"].llm_json_mode = True
        adapter = _FakeAdapter(fail_with_rf=True)
        try:
            c = _make_client(adapter)
            result = await c.chat_json("sys", "user")
            assert result == {"ok": True}
            # 第一次带 rf 失败，第二次降级成功
            assert len(adapter.calls) == 2
            assert adapter.calls[0]["response_format"] is not None
            assert adapter.calls[1]["response_format"] is None
            # 会话级禁用生效（仅对当前 provider）
            assert "test" in LLMClient._json_mode_disabled
            await c.chat_json("sys", "user again")
            assert len(adapter.calls) == 3
            assert adapter.calls[2]["response_format"] is None
        finally:
            config["app"].llm_json_mode = orig

    @pytest.mark.asyncio
    async def test_anthropic_protocol_skips_json_mode(self):
        orig = config["app"].llm_json_mode
        config["app"].llm_json_mode = True
        adapter = _FakeAdapter(protocol="anthropic")
        try:
            c = _make_client(adapter)
            await c.chat_json("sys", "user")
            assert adapter.calls[0]["response_format"] is None
        finally:
            config["app"].llm_json_mode = orig

    @pytest.mark.asyncio
    async def test_non_json_error_not_swallowed(self):
        """非 rf 相关错误不被降级逻辑吞掉（原样上抛）。"""
        orig = config["app"].llm_json_mode
        config["app"].llm_json_mode = True

        class _Boom(_FakeAdapter):
            async def chat(self, **kw):
                self.calls.append(kw)
                if kw.get("response_format"):
                    raise RuntimeError("401 unauthorized: bad key")
                raise RuntimeError("401 unauthorized: bad key")

        adapter = _Boom()
        try:
            c = _make_client(adapter)
            with pytest.raises(RuntimeError, match="401"):
                await c.chat_json("sys", "user", retries=1)
            # 未触发会话禁用（错误与 rf 无关）
            assert "test" not in LLMClient._json_mode_disabled
        finally:
            config["app"].llm_json_mode = orig
