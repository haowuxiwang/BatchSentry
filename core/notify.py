"""Feishu notification for job lifecycle events.

Phase 12: push job completion / error summaries to Feishu. Two channels:

  * "webhook" — custom group bot. POST to the group webhook URL (with
    optional HMAC signature + keyword). Config: feishu_webhook_url,
    feishu_secret.
  * "app_bot" — enterprise self-built app bot, DM to a personal user.
    Chain: tenant_access_token (cached, 2h TTL, re-fetch <30min) →
    optional open_id resolution via mobile (contact/v3/users/batch_get_id)
    → POST /open-apis/im/v1/messages (receive_id_type=open_id). Config:
    feishu_app_id, feishu_app_secret, feishu_open_id (or feishu_mobile).

Config comes from config.json top-level keys (feishu_enabled / feishu_mode /
...), read through config.load_feishu_config() and editable from the
Settings page.

Design rules:
  * Notification is a side-channel — failures must NEVER affect the pipeline.
    Every send is wrapped in try/except and only logged + audited.
  * HTTP 200 is NOT success: the Feishu API returns {"code": 0} in the body;
    business error codes (19021 signature expiry, 19024 keyword missing,
    9499 payload too large, 230006 bot disabled, 230013 out of availability,
    ...) are NOT retried.
  * Retries (exponential backoff, max 3) apply only to transport errors and
    HTTP 5xx / 429; 429 honors the `x-ogw-ratelimit-reset` response header
    when present. A token-bucket (5/s) guards against burst limits.
  * app_bot 99991663 (cached token invalidated) self-heals: drop the token
    cache, refetch, retry once — no exponential backoff.
  * Dedup: app_bot uses a deterministic `uuid` (same content → same key, so
    retries after timeouts cannot double-deliver); webhook dedups via
    audit_log (a prior successful record for the same job_id+status skips).
  * Every send attempt is recorded in audit_log (action=feishu_notify) for
    GMP traceability.

Signature algorithm (Feishu-specific — differs from DingTalk!):
  key = f"{timestamp}\\n{secret}", msg = b"", HMAC-SHA256, base64.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time

import requests

from core.zh_map import zh_severity

logger = logging.getLogger(__name__)

_API_BASE = "https://open.feishu.cn/open-apis"
_MAX_PAYLOAD_BYTES = 20 * 1024          # Feishu hard limit: 20 KB
_KEYWORD = "BatchSentry"                 # also serves as webhook keyword
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)
_MAX_RETRIES = 3
_BACKOFF_SECONDS = (1.0, 2.0, 4.0)
_MIN_INTERVAL_SECONDS = 0.2              # token bucket: 5 req/s ceiling
_TOKEN_MIN_TTL_SECONDS = 30 * 60         # re-fetch only when < 30min left

# App-bot business error codes that are NOT transient (no retry, mapped to zh)
_APP_BOT_FATAL_CODES = {
    230006: "机器人能力未启用（开发者后台开启机器人并重新发版）",
    230013: "接收者不在应用可用范围内（重新发版或调整可用范围）",
    230027: "缺少发送消息权限（开通 im:message 并重新发版）",
    230028: "消息被数据防泄漏审查拦截（勿裸发手机号/邮箱）",
    230029: "接收者已离职",
    230034: "receive_id 无效（open_id 与应用不匹配？）",
    230035: "无发送消息权限（禁言/被屏蔽）",
    230049: "消息正在发送中",
    230053: "用户已设置不再接收机器人消息",
    230101: "应用身份受限（个人账号应用可能只能回复不能主动发）",
    99991661: "缺少 Authorization 头",
    99991663: "tenant_access_token 无效（重新获取）",
}

_token_cache: dict = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
_open_id_cache: dict = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}

_last_send_ts = 0.0
_send_lock = asyncio.Lock()


def _ratelimit_sleep(resp, attempt: int) -> float:
    """Sleep honoring the Feishu `x-ogw-ratelimit-reset` header if present.

    Official guidance: 429 responses carry the reset time in seconds; when
    absent, fall back to the fixed exponential backoff schedule.
    """
    reset = resp.headers.get("x-ogw-ratelimit-reset", "")
    try:
        reset_seconds = float(reset)
    except (TypeError, ValueError):
        reset_seconds = 0.0
    # 对抗审查 P2-E：reset 头可能被代理/异常响应填充超大值或 epoch 秒，
    # 直接 time.sleep 会让调用线程睡数小时 — 该调用在 pipeline 末段
    # asyncio.to_thread 线程池里，阻塞通知收尾且非 daemon 线程阻止
    # 解释器退出（应用关不掉）。上限 30s，超限按指数退避处理。
    delay = min(max(reset_seconds, _BACKOFF_SECONDS[attempt]), 30.0)
    time.sleep(delay)
    return delay


def _idempotency_uuid(text: str) -> str:
    """Deterministic idempotency key for im/v1/messages `uuid`.

    Same logical message (same text) always maps to the same uuid, so retries
    after a timeout cannot double-deliver — Feishu dedups identical uuids
    within 1 hour (content change → hash change → new uuid).
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return base64.urlsafe_b64encode(digest.encode("utf-8")).decode()[:48]


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
                "发现：{} {} / {} {} / {} {}".format(
                    findings.get("critical", 0),
                    zh_severity("critical"),
                    findings.get("warning", 0),
                    zh_severity("warning"),
                    findings.get("info", 0),
                    zh_severity("info"),
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


async def _already_notified(job_id: str, status: str, db):
    """Async dedup guard for the webhook channel (has no idempotency key).

    Terminal events fire at most once per (job_id, status); a prior successful
    audit record means we already delivered this notification.

    对抗审查 P2-D：原实现是非 async 函数直接 return 未 await 的
    db.execute()，靠调用端 await 返回值（aiosqlite 0.21 的可 await
    cursor 兼容行为）生效；requirements 无上界，未来版本移除该兼容后
    去重检查静默失败 → 通知功能整体失效且无测试暴露。改为 async def
    显式 await 拿 cursor，语义不变。
    """
    # 对抗审查 P2-D：显式 await db.execute 拿 cursor
    cur = await db.execute(
        "SELECT 1 FROM audit_log WHERE job_id = ? AND action = 'feishu_notify' "
        "AND detail LIKE ? LIMIT 1",
        (job_id, f"%status={status} ok=True%"),
    )
    return cur


def _should_notify(status: str, feishu_cfg: dict) -> bool:
    """Notify only when enabled, channel configured, and status whitelisted."""
    if not feishu_cfg.get("enabled"):
        return False
    mode = feishu_cfg.get("mode", "webhook")
    if mode == "app_bot":
        if not feishu_cfg.get("app_id") or not feishu_cfg.get("app_secret"):
            return False
        if not feishu_cfg.get("open_id") and not feishu_cfg.get("mobile"):
            return False
    else:
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
                time.sleep(_ratelimit_sleep(resp, attempt))
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


def _get_tenant_access_token_sync(app_id: str, app_secret: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Blocking fetch of tenant_access_token with process-wide cache.

    Feishu: token TTL 7200s; calls with >= 30min remaining return the SAME
    token, below that a NEW one (both valid). We cache and only re-fetch
    when the cached token is older than 90 min. Returns (ok, token).
    """
    global _token_cache
    now = time.monotonic()
    if (
        _token_cache["token"]
        and _token_cache["app_id"] == app_id
        and _token_cache["app_secret"] == app_secret
        and now - _token_cache["fetched_at"] < 7200 - _TOKEN_MIN_TTL_SECONDS
    ):
        return True, _token_cache["token"]
    try:
        resp = requests.post(
            _API_BASE + "/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        return False, f"token transport: {type(e).__name__}"
    if resp.status_code != 200:
        return False, f"token http {resp.status_code}"
    try:
        body = resp.json()
    except ValueError:
        return False, "token non-JSON body"
    if body.get("code", -1) != 0:
        return False, f"token code={body.get('code')} msg={body.get('msg')}"
    token = body.get("tenant_access_token", "")
    if not token:
        return False, "token empty in response"
    _token_cache = {
        "token": token,
        "fetched_at": now,
        "app_id": app_id,
        "app_secret": app_secret,
    }
    return True, token


def resolve_open_id_sync(app_id: str, app_secret: str, mobile: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Blocking open_id resolution from a mobile number (contact/v3/users/batch_get_id).

    Requires the `contact:user.id:readonly` permission on the app.
    Returns (ok, open_id or error detail).
    """
    global _open_id_cache
    now = time.monotonic()
    if (
        _open_id_cache["open_id"]
        and _open_id_cache["app_id"] == app_id
        and _open_id_cache["mobile"] == mobile
        and now - _open_id_cache["resolved_at"] < 3600
    ):
        return True, _open_id_cache["open_id"]
    ok, token = _get_tenant_access_token_sync(app_id, app_secret, timeout)
    if not ok:
        return False, token
    try:
        resp = requests.post(
            _API_BASE + "/contact/v3/users/batch_get_id?user_id_type=open_id",
            json={"mobiles": [mobile]},
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=timeout,
        )
    except requests.RequestException as e:
        return False, f"resolve transport: {type(e).__name__}"
    if resp.status_code != 200:
        return False, f"resolve http {resp.status_code}"
    try:
        body = resp.json()
    except ValueError:
        return False, "resolve non-JSON body"
    if body.get("code", -1) != 0:
        return False, f"resolve code={body.get('code')} msg={body.get('msg')}"
    user_list = (body.get("data") or {}).get("user_list") or []
    for u in user_list:
        if u.get("user_id"):
            _open_id_cache = {
                "open_id": u["user_id"],
                "mobile": mobile,
                "resolved_at": now,
                "app_id": app_id,
            }
            return True, u["user_id"]
    return False, "resolve empty (手机号不存在或不在应用通讯录权限范围内)"


def app_bot_error_zh(code: int) -> str:
    """Map an app-bot business error code to a Chinese hint (or raw msg)."""
    return _APP_BOT_FATAL_CODES.get(code, "")


def _post_app_bot_sync(
    app_id: str, app_secret: str, open_id: str, mobile: str, text: str, timeout: float = 5.0
) -> tuple[bool, str]:
    """Blocking DM send via the self-built app bot. Returns (ok, detail)."""
    if not open_id:
        if not mobile:
            return False, "no receiver (open_id or mobile required)"
        ok, resolved = resolve_open_id_sync(app_id, app_secret, mobile, timeout)
        if not ok:
            return False, resolved
        open_id = resolved
    ok, token = _get_tenant_access_token_sync(app_id, app_secret, timeout)
    if not ok:
        return False, token
    # content must be an escaped JSON *string*; json.dumps is safer than hand-rolled escaping
    content = json.dumps({"text": text}, ensure_ascii=False)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    payload = {
        "receive_id": open_id,
        "msg_type": "text",
        "content": content,
        "uuid": _idempotency_uuid(text),
    }
    url = _API_BASE + "/im/v1/messages?receive_id_type=open_id"
    last_err = ""
    refreshed_token = False
    for attempt in range(_MAX_RETRIES + 1):
        try:
            headers["Authorization"] = f"Bearer {token}"
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last_err = f"transport: {type(e).__name__}"
            if attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_SECONDS[attempt])
            continue
        if resp.status_code in _RETRYABLE_STATUS:
            last_err = f"http {resp.status_code}"
            if attempt < _MAX_RETRIES:
                _ratelimit_sleep(resp, attempt)
            continue
        try:
            body = resp.json()
            code = body.get("code", -1)
            msg = body.get("msg", "")
        except ValueError:
            return False, f"http {resp.status_code}, non-JSON body"
        if resp.status_code == 200 and code == 0:
            return True, "ok"
        # 99991663 = cached tenant_access_token invalidated (expiry window).
        # Self-heal: drop the cache, refetch, retry once — no exponential backoff.
        if code == 99991663 and not refreshed_token:
            global _token_cache
            _token_cache["token"] = ""
            refreshed_token = True
            ok, token = _get_tenant_access_token_sync(app_id, app_secret, timeout)
            if not ok:
                return False, token
            last_err = f"code={code} token refreshed, retried"
            continue
        hint = app_bot_error_zh(code)
        if hint:
            return False, f"code={code} {hint}"
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
        # Dedup: a prior successful notification for this (job_id, status)
        # means we already delivered it (webhook has no idempotency key).
        dup_cursor = await _already_notified(job_id, status, db)
        if await dup_cursor.fetchone():
            logger.info(f"[{job_id}] Feishu notify skipped (already notified, status={status})")
            return
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
        mode = cfg.get("mode", "webhook")
        if mode == "app_bot":
            text = payload["content"]["text"]
            ok, detail = await asyncio.to_thread(
                _post_app_bot_sync,
                cfg["app_id"],
                cfg["app_secret"],
                cfg["open_id"],
                cfg["mobile"],
                text,
            )
        else:
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
