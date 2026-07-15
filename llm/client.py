"""LLM client — DeepSeek + SiliconFlow dual backend via OpenAI-compatible SDK."""
import json
import logging
import time
from openai import AsyncOpenAI

from config import config

logger = logging.getLogger(__name__)


class LLMClient:
    """Unified LLM client that routes to DeepSeek or SiliconFlow."""

    def __init__(self, provider: str | None = None):
        self.provider = provider or config["app"].llm_provider
        cfg = config[self.provider]
        self.client = AsyncOpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
        )
        self.model = cfg.model
        logger.info(f"LLM client initialized: provider={self.provider}, model={self.model}")

    async def chat(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 2000,
        temperature: float = 0.1,
        retries: int = 3,
        timeout: float = 120.0,
    ) -> str:
        """Send a chat completion request with retry and JSON fallback."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout=timeout,
                )
                return resp.choices[0].message.content or ""
            except Exception as e:
                last_error = e
                logger.warning(f"LLM call attempt {attempt}/{retries} failed: {e}")
                if attempt < retries:
                    time.sleep(2 * attempt)
        raise RuntimeError(f"LLM call failed after {retries} attempts: {last_error}")

    async def chat_json(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 2000,
        temperature: float = 0.1,
        retries: int = 3,
        timeout: float = 120.0,
    ) -> dict:
        """Send chat completion and parse JSON from response."""
        raw = await self.chat(
            system_prompt, user_content,
            max_tokens=max_tokens,
            temperature=temperature,
            retries=retries,
            timeout=timeout,
        )
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """Extract JSON from LLM response (handles markdown fence, leading text)."""
        text = raw.strip()
        # Strip markdown code fence
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (``` or ```json) and last line (```)
            if lines[-1].strip() == "```":
                lines = lines[:-1]
            if lines[0].strip().startswith("```"):
                lines = lines[1:]
            text = "\n".join(lines).strip()
        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Try to find first { ... } block
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
        logger.warning(f"Failed to parse JSON from LLM response: {raw[:200]}")
        return {"_parse_error": True, "_raw": raw[:500]}


# Singleton
_default_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client
