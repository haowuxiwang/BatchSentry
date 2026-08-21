"""OpenAI Chat Completions protocol adapter.

Covers any provider that speaks OpenAI's `/v1/chat/completions` wire format:
  - DeepSeek (api.deepseek.com/v1)
  - SiliconFlow (api.siliconflow.cn/v1)
  - GLM / Zhipu (open.bigmodel.cn/api/paas/v4)
  - Kimi / Moonshot (api.moonshot.cn/v1)
  - Qwen / DashScope compatible mode (dashscope.aliyuncs.com/compatible-mode/v1)
  - MiMo / Xiaomi (api.mimo.xiaomi.com/v1) — when available
  - OpenAI itself (api.openai.com/v1)

Uses the official `openai` AsyncOpenAI SDK pointed at the provider's base_url.
"""
from __future__ import annotations

import logging

from openai import AsyncOpenAI

from config import ProviderConfig
from .base import LLMAdapter, ChatResult

logger = logging.getLogger(__name__)


class OpenAIAdapter(LLMAdapter):
    """Adapter for OpenAI-compatible Chat Completions API."""

    def __init__(self, provider_cfg: ProviderConfig):
        super().__init__(provider_cfg)
        # base_url may be empty for genuine OpenAI, but we always pass it
        # explicitly to avoid surprises when the env points elsewhere.
        self.client = AsyncOpenAI(
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
        response_format: dict | None = None,
    ) -> ChatResult:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        kwargs = {}
        if response_format:
            # 结构化输出（LLM_JSON_MODE 开启时由 client 传入）：
            # OpenAI 兼容层的 json_object 模式在服务端约束输出为合法
            # JSON，从源头减少截断/围栏/前导文本三类解析失败。
            # 部分兼容网关不支持该参数（400）— client 捕获后降级重试。
            kwargs["response_format"] = response_format
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            **kwargs,
        )
        content = resp.choices[0].message.content or ""
        usage = resp.usage
        return ChatResult(
            content=content,
            prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            total_tokens=getattr(usage, "total_tokens", None) if usage else None,
            model=self.model,
        )

    def client_info(self) -> dict:
        return {
            "provider": self.provider_name,
            "protocol": self.protocol,
            "model": self.model,
            "base_url": self.provider_cfg.base_url,
            "configured": self.is_configured,
        }
