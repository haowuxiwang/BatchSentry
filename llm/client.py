"""LLM client — provider-agnostic, routes through protocol adapters.

The client owns retry/backoff, JSON parsing, and token-usage logging. The
adapter owns the wire-format translation (OpenAI Chat Completions vs
Anthropic Messages). Adding a new provider requires only a config entry —
no code changes here.

Backward compatibility: existing code that reads `client.client` (the raw
AsyncOpenAI instance) or `client.provider` / `client.model` continues to
work for OpenAI-protocol providers. For Anthropic providers, `client.client`
will be the AsyncAnthropic instance instead — callers that only need to
send chat requests should use `client.chat()` / `client.chat_json()`.
"""
import asyncio
import json
import logging
import time

from config import config
from llm.adapters import get_adapter

logger = logging.getLogger(__name__)


class LLMClient:
    """Unified LLM client that routes through protocol adapters."""

    def __init__(self, provider: str | None = None):
        self.provider = provider or config["app"].llm_provider
        providers = config["providers"]
        if self.provider not in providers:
            logger.warning(
                f"Provider '{self.provider}' not in registry "
                f"(available: {list(providers)}); falling back to 'deepseek'."
            )
            self.provider = "deepseek"
        cfg = providers[self.provider]
        self.adapter = get_adapter(cfg)
        # Expose underlying SDK client + model for backward compat
        # (core/health.py probes `client.client.chat.completions.create`).
        self.client = getattr(self.adapter, "client", None)
        self.model = cfg.model
        logger.info(
            f"LLM client initialized: provider={self.provider}, "
            f"protocol={self.adapter.protocol}, model={self.model}"
        )

    async def chat(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 4000,
        temperature: float = 0.1,
        retries: int = 3,
        timeout: float = 180.0,
        audit_ctx: dict | None = None,
    ) -> str:
        """Send a chat completion request with retry and exponential backoff.

        Args:
            audit_ctx: Optional dict with keys {job_id, page, stage,
                prompt_version}. If provided, the call is recorded in the
                llm_call_audit table for GMP traceability. Set to None for
                ad-hoc calls (health probe, etc.).
        """
        last_error = None
        last_result: "ChatResult | None" = None
        last_latency_ms = 0
        for attempt in range(1, retries + 1):
            try:
                start = time.time()
                result = await self.adapter.chat(
                    system_prompt=system_prompt,
                    user_content=user_content,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout=timeout,
                )
                elapsed = time.time() - start
                last_latency_ms = int(elapsed * 1000)
                last_result = result

                # Log token usage (handles None for providers that don't report)
                if result.total_tokens is not None:
                    logger.info(
                        f"LLM {self.model}: "
                        f"prompt={result.prompt_tokens} "
                        f"completion={result.completion_tokens} "
                        f"total={result.total_tokens} latency={elapsed:.1f}s"
                    )
                else:
                    logger.info(
                        f"LLM {self.model}: latency={elapsed:.1f}s (no usage info)"
                    )

                # Phase 7: GMP audit — record this call for traceability
                if audit_ctx is not None:
                    await _record_llm_call(
                        audit_ctx, result, last_latency_ms, success=True
                    )

                return result.content
            except Exception as e:
                last_error = e
                logger.warning(
                    f"LLM call attempt {attempt}/{retries} failed: {e}"
                )
                if attempt < retries:
                    await asyncio.sleep(2 * attempt)
        # Final failure — record audit if requested
        if audit_ctx is not None:
            await _record_llm_call(
                audit_ctx, last_result, last_latency_ms,
                success=False, error=str(last_error)[:200]
            )
        raise RuntimeError(
            f"LLM call failed after {retries} attempts: {last_error}"
        )

    async def chat_json(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 4000,
        temperature: float = 0.1,
        retries: int = 3,
        timeout: float = 180.0,
        audit_ctx: dict | None = None,
    ) -> dict | list:
        """Send chat completion and parse JSON from response. Returns dict or list."""
        raw = await self.chat(
            system_prompt, user_content,
            max_tokens=max_tokens,
            temperature=temperature,
            retries=retries,
            timeout=timeout,
            audit_ctx=audit_ctx,
        )
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> dict | list:
        """Extract JSON from LLM response. Handles markdown fence, leading text,
        truncated JSON, and both {..} and [..]."""
        text = raw.strip()

        # Strip markdown code fence
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[-1].strip() == "```":
                lines = lines[:-1]
            if lines[0].strip().startswith("```"):
                lines = lines[1:]
            text = "\n".join(lines).strip()

        # Try direct parse (handles both {...} and [...])
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to find first { ... } or [ ... ] block
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            start = text.find(start_char)
            end = text.rfind(end_char)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    pass

        # Attempt truncated JSON recovery: if text starts with { or [ and has
        # more opening braces than closing, try appending closing braces
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            if text.startswith(start_char):
                open_count = text.count(start_char)
                close_count = text.count(end_char)
                if open_count > close_count:
                    recovered = text + end_char * (open_count - close_count)
                    try:
                        result = json.loads(recovered)
                        logger.info(
                            f"Recovered truncated JSON "
                            f"(added {open_count - close_count} closing braces)"
                        )
                        return result
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


def reset_llm_client() -> None:
    """Drop the cached singleton so the next get_llm_client() rebuilds it.

    Used by the Settings API after a provider switch — ensures subsequent
    pipeline runs pick up the new adapter / API key without a process restart.
    """
    global _default_client
    _default_client = None


async def _record_llm_call(
    ctx: dict,
    result: "ChatResult | None",
    latency_ms: int,
    success: bool = True,
    error: str | None = None,
) -> None:
    """Persist a row to llm_call_audit for GMP traceability.

    Best-effort: if the DB write fails, we log but do NOT propagate — the
    user's pipeline should still complete even if audit recording is broken.
    Avoids circular import: db.client imported lazily here, not at module top.
    """
    try:
        from db.client import get_db
        db = await get_db()
        client = get_llm_client()
        await db.execute(
            """INSERT INTO llm_call_audit
               (job_id, page, stage, provider, protocol, model,
                prompt_version, prompt_tokens, completion_tokens,
                total_tokens, latency_ms, success, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ctx.get("job_id", ""),
                ctx.get("page"),
                ctx.get("stage", ""),
                client.provider,
                client.adapter.protocol,
                client.model,
                ctx.get("prompt_version"),
                result.prompt_tokens if result else None,
                result.completion_tokens if result else None,
                result.total_tokens if result else None,
                latency_ms,
                1 if success else 0,
                error,
            ),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"LLM audit record failed (non-fatal): {e}")
