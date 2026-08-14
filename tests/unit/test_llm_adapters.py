"""LLM adapter 单元测试。

覆盖：
- OpenAIAdapter: 构造 + chat 调用 + client_info
- AnthropicAdapter: 构造（SDK 未安装时 RuntimeError） + chat 调用
- get_adapter 工厂函数: 协议路由
- ChatResult 数据类
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from config import ProviderConfig
from llm.adapters import get_adapter, ChatResult
from llm.adapters.openai_adapter import OpenAIAdapter


def _make_cfg(name="deepseek", protocol="openai", **kw):
    return ProviderConfig(
        name=name,
        protocol=protocol,
        api_key=kw.get("api_key", "sk-test"),
        base_url=kw.get("base_url", "https://test.com/v1"),
        model=kw.get("model", "test-model"),
    )


class TestGetAdapter:
    """get_adapter 工厂函数 — 协议路由。"""

    def test_openai_protocol_returns_openai_adapter(self):
        cfg = _make_cfg(protocol="openai")
        adapter = get_adapter(cfg)
        assert isinstance(adapter, OpenAIAdapter)
        assert adapter.protocol == "openai"

    def test_anthropic_protocol_returns_anthropic_adapter(self):
        """如果 anthropic SDK 已安装，应返回 AnthropicAdapter；否则 RuntimeError。"""
        cfg = _make_cfg(name="claude", protocol="anthropic")
        try:
            import anthropic  # noqa: F401
            adapter = get_adapter(cfg)
            assert adapter.protocol == "anthropic"
        except ImportError:
            with pytest.raises(RuntimeError, match="anthropic"):
                get_adapter(cfg)

    def test_unknown_protocol_raises_value_error(self):
        cfg = _make_cfg(protocol="weird-protocol")
        with pytest.raises(ValueError, match="Unknown LLM protocol"):
            get_adapter(cfg)

    def test_empty_protocol_defaults_to_openai(self):
        cfg = ProviderConfig(name="test", protocol="", api_key="sk", base_url="x", model="m")
        adapter = get_adapter(cfg)
        assert adapter.protocol == "openai"


class TestOpenAIAdapter:
    """OpenAIAdapter — chat 调用 + client_info。"""

    def test_init_creates_async_openai_client(self):
        cfg = _make_cfg()
        adapter = OpenAIAdapter(cfg)
        assert adapter.provider_name == "deepseek"
        assert adapter.model == "test-model"
        assert adapter.client is not None

    @pytest.mark.asyncio
    async def test_chat_returns_chat_result(self):
        """chat() 应返回 ChatResult，content/usage 字段映射正确。"""
        cfg = _make_cfg()
        adapter = OpenAIAdapter(cfg)
        # Mock the underlying AsyncOpenAI client
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="hello"))]
        mock_usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        mock_resp.usage = mock_usage
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        result = await adapter.chat("sys", "user")
        assert isinstance(result, ChatResult)
        assert result.content == "hello"
        assert result.prompt_tokens == 10
        assert result.completion_tokens == 5
        assert result.total_tokens == 15
        assert result.model == "test-model"

    @pytest.mark.asyncio
    async def test_chat_handles_no_usage(self):
        """如果 API 不返回 usage，token 字段应为 None。"""
        cfg = _make_cfg()
        adapter = OpenAIAdapter(cfg)
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="ok"))]
        mock_resp.usage = None
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        result = await adapter.chat("sys", "user")
        assert result.content == "ok"
        assert result.prompt_tokens is None
        assert result.completion_tokens is None
        assert result.total_tokens is None

    def test_client_info_includes_provider_and_protocol(self):
        cfg = _make_cfg(name="glm", protocol="openai")
        adapter = OpenAIAdapter(cfg)
        info = adapter.client_info()
        assert info["provider"] == "glm"
        assert info["protocol"] == "openai"
        assert info["model"] == "test-model"
        assert info["base_url"] == "https://test.com/v1"
        assert info["configured"] is True

    def test_is_configured_property(self):
        """is_configured 应反映 api_key 是否设置。"""
        cfg_with_key = _make_cfg(api_key="sk-xxx")
        assert OpenAIAdapter(cfg_with_key).is_configured is True

        cfg_no_key = _make_cfg(api_key="")
        assert OpenAIAdapter(cfg_no_key).is_configured is False


class TestChatResult:
    """ChatResult 数据类。"""

    def test_defaults(self):
        result = ChatResult(content="hello")
        assert result.content == "hello"
        assert result.prompt_tokens is None
        assert result.completion_tokens is None
        assert result.total_tokens is None
        assert result.model == ""

    def test_all_fields(self):
        result = ChatResult(
            content="hi",
            prompt_tokens=5,
            completion_tokens=3,
            total_tokens=8,
            model="gpt-4",
        )
        assert result.total_tokens == 8
        assert result.model == "gpt-4"


class TestAnthropicAdapter:
    """AnthropicAdapter — mock anthropic SDK to test chat() + client_info().

    These tests work even when the `anthropic` package is NOT installed,
    by mocking sys.modules to inject a fake `anthropic` module.
    """

    def _make_fake_anthropic_module(self):
        """Create a fake `anthropic` module with AsyncAnthropic we can mock."""
        fake_mod = MagicMock()
        fake_mod.AsyncAnthropic = MagicMock()
        return fake_mod

    def _make_anthropic_cfg(self, **kw):
        return ProviderConfig(
            name=kw.get("name", "anthropic"),
            protocol="anthropic",
            api_key=kw.get("api_key", "sk-ant-test"),
            base_url=kw.get("base_url", "https://api.anthropic.com"),
            model=kw.get("model", "claude-3-5-sonnet-20241022"),
        )

    def test_init_imports_anthropic_sdk(self):
        """Adapter construction imports anthropic SDK lazily."""
        import sys
        fake_mod = self._make_fake_anthropic_module()
        with patch.dict(sys.modules, {"anthropic": fake_mod}):
            from llm.adapters.anthropic_adapter import AnthropicAdapter
            cfg = self._make_anthropic_cfg()
            adapter = AnthropicAdapter(cfg)
            assert adapter.protocol == "anthropic"
            assert adapter.model == "claude-3-5-sonnet-20241022"
            # AsyncAnthropic was called with our api_key + base_url
            fake_mod.AsyncAnthropic.assert_called_once()

    def test_init_raises_runtime_error_when_sdk_missing(self):
        """If anthropic package is not installed, raise RuntimeError."""
        import sys
        # Temporarily remove anthropic from sys.modules so import fails
        original = sys.modules.pop("anthropic", None)
        try:
            with patch.dict(sys.modules, {"anthropic": None}):
                from llm.adapters.anthropic_adapter import AnthropicAdapter
                cfg = self._make_anthropic_cfg()
                with pytest.raises(RuntimeError, match="anthropic"):
                    AnthropicAdapter(cfg)
        finally:
            if original is not None:
                sys.modules["anthropic"] = original

    @pytest.mark.asyncio
    async def test_chat_returns_chat_result(self):
        """chat() should return a ChatResult with content + token usage."""
        import sys
        fake_mod = self._make_fake_anthropic_module()
        with patch.dict(sys.modules, {"anthropic": fake_mod}):
            from llm.adapters.anthropic_adapter import AnthropicAdapter
            cfg = self._make_anthropic_cfg()
            adapter = AnthropicAdapter(cfg)

            # Mock the messages.create call
            mock_text_block = MagicMock()
            mock_text_block.text = "Hello from Claude"
            mock_resp = MagicMock()
            mock_resp.content = [mock_text_block]
            mock_usage = MagicMock()
            mock_usage.input_tokens = 12
            mock_usage.output_tokens = 8
            mock_resp.usage = mock_usage
            adapter.client.messages.create = AsyncMock(return_value=mock_resp)

            result = await adapter.chat("system prompt", "user input")
            assert isinstance(result, ChatResult)
            assert result.content == "Hello from Claude"
            assert result.prompt_tokens == 12
            assert result.completion_tokens == 8
            assert result.total_tokens == 20
            assert result.model == "claude-3-5-sonnet-20241022"

    @pytest.mark.asyncio
    async def test_chat_handles_no_usage(self):
        """chat() should return None token fields when usage is missing."""
        import sys
        fake_mod = self._make_fake_anthropic_module()
        with patch.dict(sys.modules, {"anthropic": fake_mod}):
            from llm.adapters.anthropic_adapter import AnthropicAdapter
            cfg = self._make_anthropic_cfg()
            adapter = AnthropicAdapter(cfg)

            mock_text_block = MagicMock()
            mock_text_block.text = "no usage"
            mock_resp = MagicMock()
            mock_resp.content = [mock_text_block]
            mock_resp.usage = None
            adapter.client.messages.create = AsyncMock(return_value=mock_resp)

            result = await adapter.chat("sys", "user")
            assert result.content == "no usage"
            assert result.prompt_tokens is None
            assert result.completion_tokens is None
            assert result.total_tokens is None

    def test_client_info_includes_provider_and_protocol(self):
        """client_info() should report provider, protocol, model, base_url."""
        import sys
        fake_mod = self._make_fake_anthropic_module()
        with patch.dict(sys.modules, {"anthropic": fake_mod}):
            from llm.adapters.anthropic_adapter import AnthropicAdapter
            cfg = self._make_anthropic_cfg(base_url="https://custom.anthropic.com")
            adapter = AnthropicAdapter(cfg)
            info = adapter.client_info()
            assert info["provider"] == "anthropic"
            assert info["protocol"] == "anthropic"
            assert info["model"] == "claude-3-5-sonnet-20241022"
            assert info["base_url"] == "https://custom.anthropic.com"
            assert info["configured"] is True

    def test_is_configured_reflects_api_key(self):
        """is_configured should reflect whether api_key is set."""
        import sys
        fake_mod = self._make_fake_anthropic_module()
        with patch.dict(sys.modules, {"anthropic": fake_mod}):
            from llm.adapters.anthropic_adapter import AnthropicAdapter
            cfg_with_key = self._make_anthropic_cfg(api_key="sk-ant-xxx")
            assert AnthropicAdapter(cfg_with_key).is_configured is True

            cfg_no_key = self._make_anthropic_cfg(api_key="")
            assert AnthropicAdapter(cfg_no_key).is_configured is False
