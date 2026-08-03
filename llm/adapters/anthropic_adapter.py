"""Anthropic Messages API protocol adapter.

Covers providers that speak Anthropic's `/v1/messages` wire format:
  - Anthropic Claude models (api.anthropic.com)

Differences from OpenAI protocol:
  - Auth header: `x-api-key: <key>` instead of `Authorization: Bearer <key>`
  - Required header: `anthropic-version: 2023-06-01`
  - `system` is a top-level field, not a message in the messages array
  - Response: `content[0].text` instead of `choices[0].message.content`
  - Usage fields: `input_tokens` / `output_tokens` (not prompt/completion)

Uses the official `anthropic` AsyncAnthropic SDK. The dependency is OPTIONAL
— importing this module fails fast only when an anthropic-protocol provider
is actually instantiated, not at app startup.
"""
from __future__ import annotations

import logging

from config import ProviderConfig
from .base import LLMAdapter, ChatResult

logger = logging.getLogger(__name__)


class AnthropicAdapter(LLMAdapter):
    """Adapter for Anthropic Messages API."""

    def __init__(self, provider_cfg: ProviderConfig):
        super().__init__(provider_cfg)
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:
            raise RuntimeError(
                "Provider '%s' is configured with protocol='anthropic' but "
                "the 'anthropic' package is not installed. "
                "Run: pip install anthropic" % provider_cfg.name
            ) from e
        # base_url defaults to https://api.anthropic.com when None
        self.client = AsyncAnthropic(
            api_key=provider_cfg.api_key or "EMPTY",
            base_url=provider_cfg.base_url or None,
        )

    async def chat(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 4000,
        temperature: float = 0.1,
        timeout: float = 180.0,
    ) -> ChatResult:
        # Anthropic uses a top-level `system` field, not a system message.
        resp = await self.client.messages.create(
            model=self.model,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        # content is a list of content blocks; concatenate text blocks
        text_parts = []
        for block in resp.content:
            block_text = getattr(block, "text", None)
            if block_text is not None:
                text_parts.append(block_text)
        content = "".join(text_parts)

        usage = resp.usage
        prompt_tokens = getattr(usage, "input_tokens", None) if usage else None
        completion_tokens = getattr(usage, "output_tokens", None) if usage else None
        total = (prompt_tokens + completion_tokens) if (prompt_tokens is not None and completion_tokens is not None) else None

        return ChatResult(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total,
            model=self.model,
        )

    def client_info(self) -> dict:
        return {
            "provider": self.provider_name,
            "protocol": self.protocol,
            "model": self.model,
            "base_url": self.provider_cfg.base_url or "https://api.anthropic.com",
            "configured": self.is_configured,
        }
