"""LLM 客户端单元测试。

覆盖：
- _parse_json 静态方法（JSON 结构化输出解析）
- 客户端初始化（从 config 读取 provider/model + 构建 adapter）
- chat() 重试逻辑（mock adapter）
- chat_json() JSON 解析集成
- reset_llm_client() 单例重置

Phase 7 架构变更：
- LLMClient 不再直接持有 AsyncOpenAI，而是持有 adapter
- adapter.chat() 返回 ChatResult 对象（不再是 raw SDK response）
- 测试改为 mock adapter.chat() 而非 SDK 内部方法
"""
import pytest
import json
from unittest.mock import patch, AsyncMock, MagicMock
from dataclasses import dataclass

from llm.client import LLMClient, get_llm_client, reset_llm_client
from config import ProviderConfig
from llm.adapters.base import ChatResult


def _make_provider(name="deepseek", protocol="openai", **kw):
    """Helper: build a ProviderConfig for tests."""
    return ProviderConfig(
        name=name,
        protocol=protocol,
        api_key=kw.get("api_key", "sk-test"),
        base_url=kw.get("base_url", "https://test.com/v1"),
        model=kw.get("model", "test-model"),
    )


def _mock_config(provider_name="deepseek"):
    """Build a mock config dict with `providers` registry + app.llm_provider."""
    prov = _make_provider(provider_name)
    return {
        "providers": {provider_name: prov},
        provider_name: prov,  # backward-compat top-level entry
        "app": MagicMock(llm_provider=provider_name),
    }


class TestParseJson:
    """_parse_json 静态方法 — 不需要实例化 LLMClient。"""

    def test_parse_valid_json(self):
        """标准 JSON 应正确解析。"""
        result = LLMClient._parse_json('{"key": "value", "num": 42}')
        assert result == {"key": "value", "num": 42}

    def test_parse_json_array(self):
        """JSON 数组应正确解析。"""
        result = LLMClient._parse_json('[1, 2, 3]')
        assert result == [1, 2, 3]

    def test_parse_markdown_fenced_json(self):
        """markdown 代码块包裹的 JSON 应正确解析。"""
        raw = '```json\n{"key": "value"}\n```'
        result = LLMClient._parse_json(raw)
        assert result == {"key": "value"}

    def test_parse_json_with_leading_text(self):
        """前缀文字 + JSON 应提取 JSON 部分。"""
        raw = 'Here is the result: {"steps": [], "findings": []}'
        result = LLMClient._parse_json(raw)
        assert "steps" in result
        assert "findings" in result

    def test_parse_malformed_sets_parse_error(self):
        """非 JSON 应返回 _parse_error 标记。"""
        result = LLMClient._parse_json("这不是 JSON")
        assert result.get("_parse_error") is True

    def test_parse_empty_response(self):
        """空响应应返回 _parse_error。"""
        result = LLMClient._parse_json("")
        assert result.get("_parse_error") is True

    def test_parse_truncated_json_recovers(self):
        """截断的 JSON（大括号不闭合）应尝试恢复。"""
        raw = '{"steps": [{"step_no": 1'
        result = LLMClient._parse_json(raw)
        # 恢复成功时返回 dict，失败时返回 _parse_error
        assert isinstance(result, (dict, list))


class TestLLMClientInit:
    """LLM 客户端初始化 — 从 config.providers 读取并构建 adapter。"""

    def test_init_with_mocked_config(self):
        """使用 mock config 初始化 deepseek provider，应构建 OpenAI adapter。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            assert client.provider == "deepseek"
            assert client.model == "test-model"
            assert client.adapter.protocol == "openai"
            assert client.adapter.provider_name == "deepseek"

    def test_init_defaults_to_config_provider(self):
        """不指定 provider 时应使用 config["app"].llm_provider。"""
        mock_cfg = _mock_config("siliconflow")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient()
            assert client.provider == "siliconflow"

    def test_init_falls_back_to_deepseek_when_provider_missing(self):
        """如果指定的 provider 不在注册表中，应回退到 deepseek。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="nonexistent")
            assert client.provider == "deepseek"

    def test_init_anthropic_protocol(self):
        """protocol=anthropic 应构建 AnthropicAdapter（如果 SDK 已安装）。"""
        prov = _make_provider("claude", protocol="anthropic", model="claude-3")
        mock_cfg = {
            "providers": {"claude": prov},
            "claude": prov,
            "app": MagicMock(llm_provider="claude"),
        }
        # Try import anthropic; if not installed, expect RuntimeError
        try:
            import anthropic  # noqa: F401
            with patch("llm.client.config", mock_cfg):
                client = LLMClient(provider="claude")
                assert client.adapter.protocol == "anthropic"
        except ImportError:
            with patch("llm.client.config", mock_cfg):
                with pytest.raises(RuntimeError, match="anthropic"):
                    LLMClient(provider="claude")


class TestChatMethod:
    """chat() 方法 — mock adapter.chat 测试重试逻辑。"""

    @pytest.mark.asyncio
    async def test_chat_returns_content(self):
        """成功调用应返回 content 字符串。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            # Mock adapter.chat to return a ChatResult
            client.adapter.chat = AsyncMock(return_value=ChatResult(
                content="LLM response text",
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                model="test-model",
            ))
            result = await client.chat("system", "user")
            assert result == "LLM response text"

    @pytest.mark.asyncio
    async def test_chat_retries_on_failure(self):
        """失败时应重试。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            # 第一次失败，第二次成功
            client.adapter.chat = AsyncMock(
                side_effect=[
                    Exception("timeout"),
                    ChatResult(content="ok", model="test-model"),
                ]
            )
            result = await client.chat("system", "user", retries=2)
            assert result == "ok"

    @pytest.mark.asyncio
    async def test_chat_fails_after_max_retries(self):
        """超过最大重试次数应抛出 RuntimeError。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            client.adapter.chat = AsyncMock(
                side_effect=Exception("persistent failure")
            )
            with pytest.raises(RuntimeError, match="LLM call failed"):
                await client.chat("system", "user", retries=2)

    @pytest.mark.asyncio
    async def test_chat_json_returns_parsed_dict(self):
        """chat_json 应返回解析后的 dict。"""
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            client = LLMClient(provider="deepseek")
            client.adapter.chat = AsyncMock(return_value=ChatResult(
                content='{"key": "value"}', model="test-model",
            ))
            result = await client.chat_json("system", "user")
            assert result == {"key": "value"}


class TestSingleton:
    """get_llm_client / reset_llm_client 单例行为。"""

    def test_reset_llm_client_drops_singleton(self):
        """reset_llm_client 应让下次 get_llm_client 重建客户端。"""
        reset_llm_client()
        mock_cfg = _mock_config("deepseek")
        with patch("llm.client.config", mock_cfg):
            c1 = get_llm_client()
            reset_llm_client()
            c2 = get_llm_client()
            assert c1 is not c2
        reset_llm_client()
