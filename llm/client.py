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
from typing import TYPE_CHECKING

from config import config
from llm.adapters import get_adapter

if TYPE_CHECKING:
    from llm.adapters.base import ChatResult

logger = logging.getLogger(__name__)


# 密钥脱敏，防止 SDK 异常消息把鉴权头/URL 里的密钥带回日志与
# llm_call_audit.error（对抗审查：此前错误串原样落库，密钥可能随异常
# 消息泄露到 error.log / audit 表）。
#
# 覆盖四类凭据模式（对抗审查 cr-14 扩展）：
#   1. sk- 前缀密钥（DeepSeek/SiliconFlow/OpenAI 风格）
#   2. 32 位 hex 访问令牌（PaddleOCR token 格式）
#   3. cli_ 前缀应用 ID（飞书 app_id）
#   4. Bearer 授权头值（Anthropic x-api-key 报错回显场景）
_MASK_PATTERNS = None


def _mask_secrets(text: str) -> str:
    """Redact API keys / tokens appearing in error strings before logging/audit."""
    global _MASK_PATTERNS
    if _MASK_PATTERNS is None:
        import re
        _MASK_PATTERNS = [
            (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "sk-***"),
            (re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])"), "***"),
            (re.compile(r"cli_[A-Za-z0-9_-]{8,}"), "cli_***"),
            # 飞书 app_id 是 cli- 连字符格式（此前只有 cli_ 下划线模式，
            # health 探测异常回显 URL 中的 app_id 未被脱敏）
            (re.compile(r"cli-[A-Za-z0-9]{10,}"), "cli-***"),
            (re.compile(r"Bearer\s+[A-Za-z0-9._-]{8,}", re.IGNORECASE), "Bearer ***"),
        ]
    for regex, repl in _MASK_PATTERNS:
        text = regex.sub(repl, text)
    return text


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
        # Build a short context tag for log correlation (e.g. "[job=abc123 page=5 stage=page_analysis]")
        ctx_tag = ""
        if audit_ctx:
            parts = []
            if audit_ctx.get("job_id"):
                parts.append(f"job={audit_ctx['job_id'][:8]}")
            if audit_ctx.get("page") is not None:
                parts.append(f"page={audit_ctx['page']}")
            if audit_ctx.get("stage"):
                parts.append(f"stage={audit_ctx['stage']}")
            ctx_tag = f" [{', '.join(parts)}]" if parts else ""

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

                # Log token usage with context tag for correlation
                if result.total_tokens is not None:
                    logger.info(
                        f"LLM {self.model}{ctx_tag}: "
                        f"prompt={result.prompt_tokens} "
                        f"completion={result.completion_tokens} "
                        f"total={result.total_tokens} latency={elapsed:.1f}s"
                    )
                else:
                    logger.info(
                        f"LLM {self.model}{ctx_tag}: latency={elapsed:.1f}s (no usage info)"
                    )

                # Phase 7: GMP audit — record this call for traceability
                if audit_ctx is not None:
                    await _record_llm_call(
                        audit_ctx, self, result, last_latency_ms, success=True
                    )

                return result.content
            except Exception as e:
                last_error = e
                # Timeout 类异常（asyncio.TimeoutError / httpx.ReadTimeout /
                # openai.APITimeoutError）跳过完整重试：SDK 内部已有退避重试，
                # 客户端再重试 3 次 × 240s 会让单个页面卡 12+ 分钟，整条
                # pipeline 被拖死。直接失败 → 该页归入 failed_pages 转人工，
                # 符合"快速失败、不阻塞主线"的容错原则。
                if isinstance(e, TimeoutError) or "timeout" in type(e).__name__.lower():
                    # 对抗审查（中文化收尾）：此路径是唯一未脱敏的 LLM 异常
                    # 日志 — 超时异常消息可能携带响应体（偶发回显 key），
                    # 与其他 error 路径对齐 _mask_secrets
                    logger.warning(
                        f"LLM call timed out{ctx_tag}: {type(e).__name__}: "
                        f"{_mask_secrets(str(e))} "
                        f"(skipping client-side retries)"
                    )
                    if audit_ctx is not None:
                        await _record_llm_call(
                            audit_ctx, self, last_result, last_latency_ms,
                            success=False,
                            error=_mask_secrets(str(last_error))[:200],
                        )
                    raise RuntimeError(
                        f"LLM call timed out{ctx_tag}: "
                        f"{_mask_secrets(str(last_error))}"
                    )
                # Distinguish retryable vs non-retryable errors
                err_str = str(e).lower()
                is_non_retryable = any(kw in err_str for kw in [
                    "401", "authentication", "unauthorized",
                    "403", "forbidden",
                    "400", "bad request", "invalid",
                ])
                if is_non_retryable:
                    logger.error(
                        f"LLM call failed (non-retryable){ctx_tag}: "
                        f"{_mask_secrets(f'{type(e).__name__}: {e}')}"
                    )
                    # Record audit and fail immediately
                    if audit_ctx is not None:
                        await _record_llm_call(
                            audit_ctx, self, last_result, last_latency_ms,
                            success=False,
                            error=_mask_secrets(str(last_error))[:200],
                        )
                    raise RuntimeError(
                        f"LLM call failed (non-retryable){ctx_tag}: "
                        f"{_mask_secrets(str(last_error))}"
                    )
                logger.warning(
                    f"LLM call attempt {attempt}/{retries} failed{ctx_tag}: "
                    f"{_mask_secrets(f'{type(e).__name__}: {e}')}"
                )
                if attempt < retries:
                    import random
                    backoff = (2 ** attempt) * random.uniform(0.5, 1.5)
                    logger.info(f"LLM retry{ctx_tag}: backing off {backoff:.1f}s before attempt {attempt + 1}")
                    await asyncio.sleep(backoff)
        # Final failure — record audit if requested
        if audit_ctx is not None:
            await _record_llm_call(
                audit_ctx, self, last_result, last_latency_ms,
                success=False, error=_mask_secrets(str(last_error))[:200],
            )
        raise RuntimeError(
            f"LLM call failed after {retries} attempts{ctx_tag}: "
            f"{_mask_secrets(str(last_error))}"
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
        result = self._parse_json(raw)

        # Context tag for log correlation (reused by retry + final failure logs)
        ctx_tag = ""
        if audit_ctx:
            parts = []
            if audit_ctx.get("job_id"):
                parts.append(f"job={audit_ctx['job_id'][:8]}")
            if audit_ctx.get("page") is not None:
                parts.append(f"page={audit_ctx['page']}")
            if audit_ctx.get("stage"):
                parts.append(f"stage={audit_ctx['stage']}")
            ctx_tag = f" [{', '.join(parts)}]" if parts else ""

        # robustness: JSON 解析失败重试 — LLM 偶发输出带 ```json 围栏、尾随
        # 文本或非法转义字符，API 调用本身成功（audit 已记 success=1）但内容
        # 不可用。附加"修正提示"后最多重试 2 次单发调用（retries=1，不叠加
        # API 级重试与退避，控制成本）。51 页真实回归中第 19 页正是此类失败。
        parse_attempt = 1
        while (
            isinstance(result, dict)
            and result.get("_parse_error")
            and parse_attempt < 3
        ):
            parse_attempt += 1
            logger.warning(
                f"JSON parse failed{ctx_tag} (attempt {parse_attempt - 1}), "
                f"retrying with fix hint: response_length={len(raw)}, "
                f"first_200={raw[:200]!r}"
            )
            # C1: fix-hint 重试的 audit 记录单独标记 stage（stage:fix_hint）—
            # 原样复用 audit_ctx 会在 llm_call_audit 留下两条无法区分的同
            # stage/page 记录，GMP 追溯时看不出哪条是修复重试。
            fix_audit_ctx = dict(audit_ctx) if audit_ctx else None
            if fix_audit_ctx is not None:
                base_stage = fix_audit_ctx.get("stage", "")
                fix_audit_ctx["stage"] = (
                    f"{base_stage}:fix_hint" if base_stage else "fix_hint"
                )
            raw = await self.chat(
                system_prompt,
                user_content
                + (
                    "\n\n[系统提示] 你上一次的输出无法解析为合法 JSON"
                    "（可能包含 Markdown 代码块围栏、尾随文本或非法转义字符）。"
                    "请重新输出：只输出一个合法 JSON 对象，不要 Markdown 代码块、"
                    "不要注释、不要多余说明文字。"
                ),
                max_tokens=max_tokens,
                temperature=temperature,
                retries=1,
                timeout=timeout,
                audit_ctx=fix_audit_ctx,
            )
            result = self._parse_json(raw)

        # Log final parse failures with context for production debugging
        if isinstance(result, dict) and result.get("_parse_error"):
            logger.warning(
                f"JSON parse failed{ctx_tag}: "
                f"response_length={len(raw)}, first_200={raw[:200]!r}"
            )

        return result

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

        # Try to find first { ... } or [ ... ] block (only when the text does
        # not START with a brace — otherwise the block scan can grab an inner
        # fragment like "steps": [] and return an empty array instead of the
        # truncated outer object; brace-starting text goes straight to the
        # truncation recovery below).
        if not text.startswith(("{", "[")):
            for start_char, end_char in [("{", "}"), ("[", "]")]:
                start = text.find(start_char)
                end = text.rfind(end_char)
                if start != -1 and end > start:
                    try:
                        return json.loads(text[start : end + 1])
                    except json.JSONDecodeError:
                        pass

        # Attempt truncated JSON recovery: if text starts with { or [ and has
        # more opening braces than closing, try appending closing braces.
        # Round 3: truncated output may also cut mid-string (e.g. page9 matrix
        # hitting max_tokens). Repair: drop the dangling tail text (any chars
        # after the last complete JSON value), re-close unclosed strings/arrays,
        # then re-balance braces.
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            if text.startswith(start_char):
                open_count = text.count(start_char)
                close_count = text.count(end_char)
                if open_count > close_count:
                    repaired = _repair_truncated_json(text)
                    if repaired is not None:
                        try:
                            result = json.loads(repaired)
                            logger.info(
                                f"Recovered truncated JSON "
                                f"(added {repaired.count(end_char) - close_count} closing braces)"
                            )
                            if isinstance(result, dict):
                                result["_truncated_recovered"] = True
                            return result
                        except json.JSONDecodeError:
                            pass
                    # Legacy path: pure brace padding (covers cut-between-values,
                    # which _repair_truncated_json also handles, but keep as
                    # last resort for exotic shapes)
                    recovered = text + end_char * (open_count - close_count)
                    try:
                        result = json.loads(recovered)
                        logger.info(
                            f"Recovered truncated JSON "
                            f"(added {open_count - close_count} closing braces)"
                        )
                        if isinstance(result, dict):
                            result["_truncated_recovered"] = True
                        return result
                    except json.JSONDecodeError:
                        pass

        logger.warning(f"Failed to parse JSON from LLM response: {raw[:200]}")
        return {"_parse_error": True, "_raw": raw[:500]}


def _repair_truncated_json(text: str) -> str | None:
    """Repair JSON truncated mid-string / mid-value.

    Naive brace padding cannot fix a cut inside a string value ("...16:06,").
    This walker scans char-by-char tracking string/escape state, drops the
    dangling tail (the incomplete value), pads a cut value with null, then
    closes open strings, arrays and objects in the matching order. Returns
    None when the text is not repairable (e.g. not actually truncated).
    """
    out = []
    in_string = False
    escape = False
    stack = []
    i = 0
    n = len(text)
    # value_boundary[i] == True when a complete value ends right before i
    # (we only need to know whether the last emitted char marks a cut point)
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
        elif ch in "{[":
            stack.append(ch)
            out.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            out.append(ch)
        elif ch in ",:":
            out.append(ch)
        elif ch.isspace():
            out.append(ch)
        else:
            out.append(ch)
        i += 1

    if in_string:
        # dangling unterminated string: cut back to its opening quote
        cut = -1
        for j in range(len(out) - 1, -1, -1):
            if out[j] == '"':
                cut = j
                break
        if cut == -1:
            return None
        out = out[:cut]
        # value was cut mid-string: pad with null if we end after a colon
        stripped = "".join(out).rstrip()
        if stripped.endswith(":"):
            out.append("null")

    # Trailing raw token (number/true/false/null): keep it when it reads as a
    # complete primitive (max_tokens cuts happen between tokens, not usually
    # mid-number); drop it otherwise.
    j = len(out) - 1
    while j >= 0 and (out[j].isspace() or out[j] in "0123456789.+-eE"):
        j -= 1
    tail = "".join(out[j + 1:]).strip()
    if tail:
        import re as _re
        if not _re.fullmatch(r"-?\d+(\.\d+)?([eE][+-]?\d+)?|true|false|null", tail):
            out = out[:j + 1]

    # re-balance brace stack on the trimmed prefix
    stack.clear()
    for ch in out:
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    if not stack:
        return None  # not actually truncated
    if out and out[-1] == ",":
        out.pop()  # trailing comma before the cut value
    for ch in reversed(stack):
        out.append("}" if ch == "{" else "]")
    return "".join(out)


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
    llm_client: "LLMClient",
    result: "ChatResult | None",
    latency_ms: int,
    success: bool = True,
    error: str | None = None,
) -> None:
    """Persist a row to llm_call_audit for GMP traceability.

    Best-effort: if the DB write fails, we log but do NOT propagate — the
    user's pipeline should still complete even if audit recording is broken.
    Avoids circular import: db.client imported lazily here, not at module top.

    P-C4 修复：execute + commit 包在 core.pipeline.db_lock 内，与 _analyze_one
    / transition_status / _audit_log 共享同一锁，避免并发写入触发 aiosqlite
    "Recursive use of cursors" 错误。
    """
    try:
        from db.client import get_db
        # 延迟导入避免循环依赖（core.pipeline 不在 module top 引入 llm.client，
        # 但 llm.adapters 可能间接被 core 引入，保险起见在此 lazy import）
        from core.pipeline import db_lock
        db = await get_db()
        client = llm_client
        async with db_lock:
            await db.execute(
                """INSERT INTO llm_call_audit
                   (job_id, page, stage, provider, protocol, model,
                    prompt_version, prompt_tokens, completion_tokens,
                    total_tokens, latency_ms, success, error, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           datetime('now','localtime'))""",
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
