"""Feishu group-bot notification for job lifecycle events.

Phase 12: push job completion / error summaries to a Feishu group via the
custom bot webhook (zero new dependencies, `requests` only — same pattern as
core/ocr_client.py). Config comes from config.json top-level keys
(feishu_enabled / feishu_webhook_url / feishu_secret / feishu_events), read
through config.load_feishu_config() and editable from the Settings page.

Design rules:
  * Notification is a side-channel — failures must NEVER affect the pipeline.
    Every send is wrapped in try/except and only logged + audited.
  * HTTP 200 is NOT success: the Feishu API returns {"code": 0} in the body;
    business error codes (19021 signature expiry, 19024 keyword missing,
    9499 payload too large) are NOT retried.
  * Retries (exponential backoff, max 3) apply only to transport errors and
    HTTP 5xx / 429; a token-bucket (5/s) guards against burst limits.
  * Every send attempt is recorded in audit_log (action=feishu_notify) for
    GMP traceability.

Signature algorithm (Feishu-specific — differs from DingTalk!):
  key = f"{timestamp}\\n{secret}", msg = b"", HMAC-SHA256, base64.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import time

import requests

logger = logging.getLogger(__name__)

_MAX_PAYLOAD_BYTES = 20 * 1024          # Feishu hard limit: 20 KB
_KEYWORD = "BatchSentry"                 # also serves as webhook keyword
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)
_MAX_RETRIES = 3
_BACKOFF_SECONDS = (1.0, 2.0, 4.0)
_MIN_INTERVAL_SECONDS = 0.2              # token bucket: 5 req/s ceiling

_last_send_ts = 0.0
_send_lock = asyncio.Lock()


def sign_payload(secret: str, timestamp: int) -> str:
    """Feishu custom-bot signature.

    key = "timestamp\\nsecret"; HMAC-SHA256 over an EMPTY message; base64.
    Golden value for (timestamp=1599360473, secret="test_secret") is
    asserted in tests to lock the algorithm against "optimizations".
    """
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def build_text_message(
    filename: str,
    status: str,
    total_pages: int,
    findings: dict | None = None,
    error_message: str = "",
) -> dict:
    """Build a Feishu text message body (always <= 20 KB after encoding).

    findings: {"critical": n, "warning": n, "info": n} severity counts.
    The message header always contains the keyword so group keyword rules
    (if configured by the user) never block delivery.
    """
    status_zh = {
        "review": "分析完成，已生成报告",
        "partial_review": "部分完成（有失败页）",
        "error": "处理失败",
    }.get(status, status)
    lines = [
        f"【{_KEYWORD}】批生产记录{status_zh}",
        f"文件：{filename or '(未知)'}",
    ]
    if status != "error":
        lines.append(f"页数：{total_pages} 页")
        if findings:
            lines.append(
                "发现：{} critical / {} warning / {} info".format(
                    findings.get("critical", 0),
                    findings.get("warning", 0),
                    findings.get("info", 0),
                )
            )
    else:
        lines.append(f"错误：{error_message[:500] or '(无详细信息)'}")
    text = "\n".join(lines)
    payload = {"msg_type": "text", "content": {"text": text}}
    # 20KB 是"请求体"硬限制（含 JSON 结构开销），截断余量留 256B
    if len(text.encode("utf-8")) > _MAX_PAYLOAD_BYTES - 256:
        text = text.encode("utf-8")[:_MAX_PAYLOAD_BYTES - 512].decode(
            "utf-8", errors="ignore"
        ) + "…(截断)"
        payload["content"]["text"] = text
    return payload


def _should_notify(status: str, feishu_cfg: dict) -> bool:
    """Notify only when enabled, URL set, and status is in the event whitelist."""
    if not feishu_cfg.get("enabled"):
        return False
    if not feishu_cfg.get("webhook_url"):
        return False
    events = feishu_cfg.get("events") or []
    return status in events


async def _throttle() -> None:
    """Process-wide token bucket: at least _MIN_INTERVAL_SECONDS apart."""
    global _last_send_ts
    async with _send_lock:
        elapsed = time.monotonic() - _last_send_ts
        if elapsed < _MIN_INTERVAL_SECONDS:
            await asyncio.sleep(_MIN_INTERVAL_SECONDS - elapsed)
        _last_send_ts = time.monotonic()


def _post_sync(url: str, payload: dict, secret: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Blocking send (called via asyncio.to_thread). Returns (ok, detail)."""
    if secret:
        ts = int(time.time())
        payload = {"timestamp": str(ts), "sign": sign_payload(secret, ts), **payload}
    headers = {"Content-Type": "application/json"}
    last_err = ""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last_err = f"transport: {type(e).__name__}"
            if attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_SECONDS[attempt])
            continue
        if resp.status_code in _RETRYABLE_STATUS:
            last_err = f"http {resp.status_code}"
            if attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_SECONDS[attempt])
            continue
        try:
            body = resp.json()
            code = body.get("code", -1)
            msg = body.get("msg", "")
        except ValueError:
            return False, f"http {resp.status_code}, non-JSON body"
        if resp.status_code == 200 and code == 0:
            return True, "ok"
        return False, f"code={code} msg={msg} http={resp.status_code}"
    return False, f"retries exhausted: {last_err}"


async def notify_job(job_id: str, status: str) -> None:
    """Send a job lifecycle notification if configured.

    Pure side-effect guard: any exception is logged, never raised — the
    pipeline must not fail because notifications failed.
    """
    try:
        from config import load_feishu_config
        cfg = load_feishu_config()
        if not _should_notify(status, cfg):
            return
        await _throttle()
        # Job stats snapshot for the message
        from db.client import get_db
        db = await get_db()
        cursor = await db.execute(
            "SELECT filename, total_pages, error_message FROM jobs WHERE id = ?",
            (job_id,),
        )
        row = await cursor.fetchone()
        filename = ""
        total_pages = 0
        error_message = ""
        if row:
            filename = row["filename"] or ""
            total_pages = row["total_pages"] or 0
            error_message = row["error_message"] or ""
        findings = None
        if status != "error" and row:
            cursor = await db.execute(
                "SELECT severity, COUNT(*) AS cnt FROM findings "
                "WHERE job_id = ? GROUP BY severity",
                (job_id,),
            )
            findings = {}
            for f in await cursor.fetchall():
                findings[f["severity"]] = f["cnt"]
        payload = build_text_message(filename, status, total_pages, findings, error_message)
        ok, detail = await asyncio.to_thread(
            _post_sync, cfg["webhook_url"], payload, cfg["secret"]
        )
        from db.client import get_db as _db
        audit_db = await _db()
        await audit_db.execute(
            "INSERT INTO audit_log (job_id, action, detail) VALUES (?, ?, ?)",
            (job_id, "feishu_notify", f"status={status} ok={ok} {detail}"),
        )
        await audit_db.commit()
        if ok:
            logger.info(f"[{job_id}] Feishu notify sent (status={status})")
        else:
            logger.warning(f"[{job_id}] Feishu notify failed: {detail}")
    except Exception as e:
        logger.error(f"[{job_id}] Feishu notify error: {e}", exc_info=True)
