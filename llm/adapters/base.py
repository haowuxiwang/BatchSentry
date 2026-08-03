"""Abstract LLM adapter — defines the uniform interface every protocol adapter
must implement. Keeping the surface tiny (just `chat`) makes adding a new
protocol a localized change.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass

from config import ProviderConfig


@dataclass
class ChatResult:
    """Normalized chat-completion result returned by every adapter.

    Attributes:
        content: The assistant message text.
        prompt_tokens / completion_tokens / total_tokens: Token usage if the
            upstream API reported it. None when the API doesn't return usage.
        model: The model identifier actually used (useful for audit logs and
            the health probe, which needs to report which model served the
            request).
    """
    content: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    model: str = ""


class LLMAdapter(abc.ABC):
    """Protocol-agnostic LLM adapter.

    Subclasses wrap a specific SDK (openai, anthropic, ...) and translate
    our uniform `chat()` call into the provider's wire format.
    """

    def __init__(self, provider_cfg: ProviderConfig):
        self.provider_cfg = provider_cfg
        self.provider_name = provider_cfg.name
        self.protocol = (provider_cfg.protocol or "openai").lower()
        self.model = provider_cfg.model

    @abc.abstractmethod
    async def chat(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 4000,
        temperature: float = 0.1,
        timeout: float = 180.0,
    ) -> ChatResult:
        """Send a single chat completion request and return a ChatResult.

        Implementations should NOT retry — the LLMClient owns the retry loop.
        Implementations SHOULD raise on transport/auth errors so the retry
        loop can decide whether to back off.
        """
        ...

    @abc.abstractmethod
    def client_info(self) -> dict:
        """Return a small dict describing this adapter's underlying client.

        Used by the /api/health/downstream probe to report which provider /
        model / base_url is configured without exposing the API key.
        """
        ...

    @property
    def is_configured(self) -> bool:
        """True if this provider has an API key set (i.e. can be used)."""
        return bool(self.provider_cfg.api_key)
