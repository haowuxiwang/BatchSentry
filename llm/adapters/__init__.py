"""LLM protocol adapters — abstraction over OpenAI vs Anthropic wire formats.

Why this exists:
  Different LLM providers speak different HTTP/JSON protocols. To keep the
  core LLMClient (retry, JSON parsing, token logging) provider-agnostic, we
  route every request through an adapter that exposes a uniform `chat()`
  method. Adding a new provider then only requires declaring it in config
  (ProviderConfig.protocol) — no client code changes.

Supported protocols:
  - "openai":    OpenAI Chat Completions (/v1/chat/completions).
                 Used by DeepSeek, SiliconFlow, GLM (Zhipu), Kimi (Moonshot),
                 Qwen (DashScope compatible mode), MiMo (Xiaomi), OpenAI itself.
  - "anthropic": Anthropic Messages API (/v1/messages) with x-api-key auth +
                 anthropic-version header + top-level system field.
                 Used by Claude models via the native Anthropic SDK.
"""
from config import ProviderConfig

from .base import LLMAdapter, ChatResult
from .openai_adapter import OpenAIAdapter
from .anthropic_adapter import AnthropicAdapter


_ADAPTERS = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
}


def get_adapter(provider_cfg: ProviderConfig) -> LLMAdapter:
    """Build the right adapter for a provider's configured protocol.

    Raises:
        ValueError: if `provider_cfg.protocol` is not a known protocol.
        RuntimeError: if the SDK for the protocol is not installed
            (e.g. `anthropic` package missing for an anthropic provider).
    """
    protocol = (provider_cfg.protocol or "openai").lower()
    cls = _ADAPTERS.get(protocol)
    if cls is None:
        raise ValueError(
            f"Unknown LLM protocol '{protocol}' for provider "
            f"'{provider_cfg.name}'. Supported: {list(_ADAPTERS)}"
        )
    return cls(provider_cfg)


__all__ = [
    "LLMAdapter",
    "ChatResult",
    "OpenAIAdapter",
    "AnthropicAdapter",
    "get_adapter",
]
