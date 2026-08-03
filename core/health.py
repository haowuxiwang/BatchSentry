"""Downstream service health probes.

Checks reachability of configured OCR and LLM endpoints BEFORE submitting
real work, so misconfiguration fails fast instead of after a 10-minute OCR
timeout.

Used by:
- /api/health/downstream endpoint (manual check from Settings page)
- pipeline pre-flight check (optional)
"""
import asyncio
import logging
import time

import requests

from config import config
from llm.client import get_llm_client

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 8  # seconds — short, fail fast


def probe_paddle_ocr() -> dict:
    """Probe PaddleOCR service: GET the base URL with auth header."""
    cfg = config["paddle_ocr"]
    if not cfg.api_url or not cfg.token:
        return {"ok": False, "reason": "PADDLE_OCR_API_URL or TOKEN not configured"}
    try:
        start = time.time()
        # Use OPTIONS or HEAD to minimize side effects; fall back to GET.
        headers = {"Authorization": f"bearer {cfg.token}"}
        resp = requests.request("OPTIONS", cfg.api_url, headers=headers, timeout=_PROBE_TIMEOUT)
        elapsed = (time.time() - start) * 1000
        # Many APIs return 405 for OPTIONS but that still proves reachability
        reachable = resp.status_code < 500
        return {
            "ok": reachable,
            "status": resp.status_code,
            "latency_ms": round(elapsed, 0),
            "reason": "" if reachable else f"HTTP {resp.status_code}",
        }
    except requests.exceptions.Timeout:
        return {"ok": False, "reason": f"Timeout after {_PROBE_TIMEOUT}s"}
    except requests.exceptions.ConnectionError as e:
        return {"ok": False, "reason": f"Connection failed: {e.__class__.__name__}"}
    except Exception as e:
        return {"ok": False, "reason": f"{e.__class__.__name__}: {e}"}


def probe_mineru() -> dict:
    """Probe MinerU service: just verify token is configured."""
    cfg = config["mineru"]
    if not cfg.token:
        return {"ok": False, "reason": "MINERU_TOKEN not configured"}
    # MinerU has no simple health endpoint; configured is the best we can check
    # without submitting a real job.
    return {"ok": True, "reason": "Token configured (reachability checked on first job)"}


async def probe_llm() -> dict:
    """Probe LLM service: send a minimal chat completion via the adapter.

    Uses the adapter layer (not the raw SDK client) so this works for both
    OpenAI and Anthropic protocols. Catches the case where the configured
    provider has no API key (adapter construction may still succeed but
    the actual request will fail with an auth error, which is what we want
    to surface to the user).
    """
    try:
        client = get_llm_client()
        info = client.adapter.client_info()
        start = time.time()
        # Tiny prompt to verify auth + connectivity. Goes through adapter.chat
        # which routes to OpenAI chat.completions.create or Anthropic
        # messages.create as appropriate. max_tokens=1 to minimize cost.
        await client.adapter.chat(
            system_prompt="",
            user_content="ping",
            max_tokens=1,
            temperature=0,
            timeout=_PROBE_TIMEOUT,
        )
        elapsed = (time.time() - start) * 1000
        return {
            "ok": True,
            "model": client.model,
            "provider": client.provider,
            "protocol": info.get("protocol", "?"),
            "latency_ms": round(elapsed, 0),
            "reason": "",
        }
    except Exception as e:
        # Best-effort model/provider reporting — adapter may not be built
        try:
            client = get_llm_client()
            model = client.model
            provider = client.provider
        except Exception:
            model, provider = "?", "?"
        return {
            "ok": False,
            "model": model,
            "provider": provider,
            "reason": f"{e.__class__.__name__}: {str(e)[:200]}",
        }


async def probe_all() -> dict:
    """Probe all configured downstream services in parallel."""
    backend = config["app"].ocr_backend
    ocr_probe = probe_mineru if backend == "mineru" else probe_paddle_ocr

    ocr_result, llm_result = await asyncio.gather(
        asyncio.to_thread(ocr_probe),
        probe_llm(),
    )
    return {
        "ocr_backend": backend,
        "ocr": ocr_result,
        "llm": llm_result,
        "all_ok": ocr_result["ok"] and llm_result["ok"],
    }
