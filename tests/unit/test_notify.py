"""Feishu notification unit tests (Phase 12).

Covers the Feishu-specific signature algorithm (golden value locks the
implementation against regressions), message building with the 20KB guard,
event whitelist gating, transport retries, and body-code verification
(HTTP 200 != success).

All network calls are mocked — no real webhook is ever contacted.
"""

import asyncio
import json
import unittest.mock as mock


from core.notify import (
    build_text_message,
    notify_job,
    sign_payload,
    _post_sync,
    _should_notify,
)


# ===========================================================================
# Signature
# ===========================================================================


class TestSignature:
    """Feishu custom-bot signature (key=ts\\nsecret over an EMPTY message).

    官方文档与钉钉签名方向相反（钉钉: key=secret, msg=ts\\nsecret）。
    黄金值锁定算法，防止被"优化"改坏。
    """

    def test_golden_value_locks_algorithm(self):
        assert (
            sign_payload("test_secret", 1599360473)
            == "FW06sV98dJlmB07TC2kBBUSpkGrDmWP+mE2IRa7SQhA="
        )

    def test_output_is_base64_sha256_digest(self):
        sig = sign_payload("s", 1234567890)
        import base64
        raw = base64.b64decode(sig)
        assert len(raw) == 32  # SHA-256 digest length
        assert sig.endswith("=") or "=" not in sig  # base64 padding validity


# ===========================================================================
# Message building
# ===========================================================================


class TestBuildTextMessage:
    def test_review_message_contains_keyword_and_stats(self):
        payload = build_text_message(
            "FS-001.pdf", "review", 51,
            {"critical": 3, "warning": 5, "info": 2}, "",
        )
        text = payload["content"]["text"]
        assert "BatchSentry" in text
        assert "FS-001.pdf" in text
        assert "51 页" in text
        assert "3 严重 / 5 警告 / 2 信息" in text

    def test_error_message_includes_error_detail(self):
        payload = build_text_message("FS-002.pdf", "error", 0, None, "OCR timeout after 600s")
        text = payload["content"]["text"]
        assert "OCR timeout after 600s" in text
        assert "发现" not in text

    def test_partial_review_status(self):
        payload = build_text_message("FS-003.pdf", "partial_review", 10, None, "")
        assert "部分完成" in payload["content"]["text"]

    def test_oversized_error_message_truncated_to_500(self):
        payload = build_text_message("F.pdf", "error", 0, None, "E" * 2000)
        assert "E" * 500 in payload["content"]["text"]
        assert "E" * 501 not in payload["content"]["text"]

    def test_payload_stays_under_20kb(self):
        findings = {"critical": 1, "warning": 1, "info": 1}
        payload = build_text_message("长" * 100000, "error", 99999, findings, "错误" * 2000)
        assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= 20 * 1024


# ===========================================================================
# Event gating
# ===========================================================================


class TestShouldNotify:
    def test_disabled_never_notifies(self):
        assert not _should_notify("review", {"enabled": False, "webhook_url": "u", "events": ["review"]})

    def test_missing_url_never_notifies(self):
        assert not _should_notify("review", {"enabled": True, "webhook_url": "", "events": ["review"]})

    def test_event_not_in_whitelist_skips(self):
        cfg = {"enabled": True, "webhook_url": "u", "events": ["review", "error"]}
        assert _should_notify("review", cfg)
        assert not _should_notify("cancelled", cfg)

    def test_empty_events_skips_all(self):
        assert not _should_notify("review", {"enabled": True, "webhook_url": "u", "events": []})


# ===========================================================================
# Transport (mocked requests)
# ===========================================================================


class TestPostSync:
    def test_success_checks_body_code(self):
        """HTTP 200 但 body code != 0 → 视为失败（飞书 API 业务码）。"""
        with mock.patch("core.notify.requests.post") as m:
            m.return_value.status_code = 200
            m.return_value.json.return_value = {"code": 0, "msg": "success"}
            ok, detail = _post_sync("https://open.feishu.cn/x", {"msg_type": "text"}, "")
        assert ok and detail == "ok"

    def test_http_200_with_nonzero_code_is_failure(self):
        with mock.patch("core.notify.requests.post") as m:
            m.return_value.status_code = 200
            m.return_value.json.return_value = {"code": 19024, "msg": "keyword missing"}
            ok, detail = _post_sync("u", {"msg_type": "text"}, "")
        assert not ok
        assert "19024" in detail

    def test_transport_error_retries_three_times(self):
        """网络类异常：指数退避重试，最多 3 次（共 4 次尝试）。"""
        import requests as _requests
        with mock.patch("core.notify.requests.post",
                        side_effect=_requests.exceptions.ConnectionError("conn reset")) as m, \
                mock.patch("core.notify.time.sleep") as sleep:
            ok, detail = _post_sync("u", {"msg_type": "text"}, "")
        assert not ok
        assert m.call_count == 4  # 1 + 3 retries
        assert "retries exhausted" in detail
        assert sleep.call_count == 3

    def test_business_code_not_retried(self):
        """业务错误（19024 关键词缺失）不重试。"""
        with mock.patch("core.notify.requests.post") as m:
            m.return_value.status_code = 200
            m.return_value.json.return_value = {"code": 19024, "msg": "kw"}
            ok, _ = _post_sync("u", {"msg_type": "text"}, "")
        assert not ok
        assert m.call_count == 1

    def test_429_retried_once_then_succeeds(self):
        resp_429 = mock.MagicMock(status_code=429)
        resp_ok = mock.MagicMock(status_code=200)
        resp_ok.json.return_value = {"code": 0, "msg": "success"}
        with mock.patch("core.notify.requests.post", side_effect=[resp_429, resp_ok]), \
                mock.patch("core.notify.time.sleep"):
            ok, detail = _post_sync("u", {"msg_type": "text"}, "")
        assert ok

    def test_429_honors_ratelimit_reset_header(self):
        """429 响应头 x-ogw-ratelimit-reset → 以它为等待时长（官方推荐）。"""
        resp_429 = mock.MagicMock(status_code=429)
        resp_429.headers = {"x-ogw-ratelimit-reset": "30"}
        resp_ok = mock.MagicMock(status_code=200)
        resp_ok.json.return_value = {"code": 0, "msg": "success"}
        with mock.patch("core.notify.requests.post", side_effect=[resp_429, resp_ok]), \
                mock.patch("core.notify.time.sleep") as sleep:
            ok, _ = _post_sync("u", {"msg_type": "text"}, "")
        assert ok
        assert sleep.call_args[0][0] == 30  # reset header wins over backoff

    def test_429_without_reset_header_uses_backoff(self):
        resp_429 = mock.MagicMock(status_code=429)
        resp_429.headers = {}
        resp_ok = mock.MagicMock(status_code=200)
        resp_ok.json.return_value = {"code": 0, "msg": "success"}
        with mock.patch("core.notify.requests.post", side_effect=[resp_429, resp_ok]), \
                mock.patch("core.notify.time.sleep") as sleep:
            ok, _ = _post_sync("u", {"msg_type": "text"}, "")
        assert ok
        assert sleep.call_args[0][0] == 1.0  # fallback to first backoff step

    def test_signature_attached_when_secret_set(self):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            resp = mock.MagicMock(status_code=200)
            resp.json.return_value = {"code": 0, "msg": "success"}
            return resp

        with mock.patch("core.notify.requests.post", side_effect=fake_post):
            ok, _ = _post_sync("https://open.feishu.cn/hook/abc", {"msg_type": "text"}, "sec123")
        assert ok
        payload = captured["payload"]
        assert "timestamp" in payload and "sign" in payload
        assert payload["msg_type"] == "text"
        assert payload["sign"] == sign_payload("sec123", int(payload["timestamp"]))


# ===========================================================================
# notify_job end-to-end (mocked webhook + real DB)
# ===========================================================================


class TestNotifyJob:
    def test_disabled_never_calls_requests(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"feishu_enabled": False}), encoding="utf-8")
        with mock.patch("config._config_path", return_value=cfg), \
                mock.patch("core.notify.requests.post") as m:
            asyncio.run(notify_job("job-x", "review"))
        m.assert_not_called()

    def test_event_not_whitelisted_skips(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "feishu_enabled": True,
            "feishu_webhook_url": "https://open.feishu.cn/hook/abc",
            "feishu_events": "review",
        }), encoding="utf-8")
        with mock.patch("config._config_path", return_value=cfg), \
                mock.patch("core.notify.requests.post") as m:
            asyncio.run(notify_job("job-x", "error"))
        m.assert_not_called()

    def test_success_flow_writes_audit_log(self, tmp_path):
        """配置正确 + 发送成功 → audit_log 出现 feishu_notify（GMP 留痕）。"""
        import aiosqlite
        from db.client import init_schema, migrate

        db_path = tmp_path / "t.db"
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "feishu_enabled": True,
            "feishu_webhook_url": "https://open.feishu.cn/hook/abc",
            "feishu_events": "review,error",
        }), encoding="utf-8")

        async def run():
            async with aiosqlite.connect(db_path) as conn:
                conn.row_factory = aiosqlite.Row
                await init_schema(conn)
                await migrate(conn)
                await conn.execute(
                    "INSERT INTO jobs (id, filename, status, total_pages) "
                    "VALUES ('job-x', 'f.pdf', 'review', 2)"
                )
                await conn.execute(
                    "INSERT INTO findings (job_id, page, type, severity, description, source) "
                    "VALUES ('job-x', 1, 'param', 'critical', 'd', 'rule')"
                )
                await conn.commit()

                import db.client as db_mod
                orig = db_mod._db
                db_mod._db = conn

                resp = mock.MagicMock(status_code=200)
                resp.json.return_value = {"code": 0, "msg": "success"}
                try:
                    with mock.patch("config._config_path", return_value=cfg), \
                            mock.patch("core.notify.requests.post", return_value=resp):
                        await notify_job("job-x", "review")
                    cur = await conn.execute(
                        "SELECT job_id, action, detail FROM audit_log WHERE action = 'feishu_notify'"
                    )
                    rows = await cur.fetchall()
                    assert len(rows) == 1
                    assert rows[0]["job_id"] == "job-x"
                    assert "ok=True" in rows[0]["detail"]
                    assert "status=review" in rows[0]["detail"]
                finally:
                    db_mod._db = orig

        asyncio.run(run())

    def test_database_error_never_raises(self, tmp_path):
        """DB 故障时 notify_job 只记日志不抛出（旁路原则）。"""
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "feishu_enabled": True,
            "feishu_webhook_url": "https://open.feishu.cn/hook/abc",
            "feishu_events": "review",
        }), encoding="utf-8")
        with mock.patch("config._config_path", return_value=cfg), \
                mock.patch("db.client.get_db", side_effect=RuntimeError("db down")):
            # 不应抛出异常
            asyncio.run(notify_job("job-x", "review"))

    def test_dedup_skips_second_notification(self, tmp_path):
        """同 (job_id, status) 已有成功记录 → 第二次不发（webhook 无幂等键）。"""
        import aiosqlite
        from db.client import init_schema, migrate
        import db.client as db_mod

        db_path = tmp_path / "t_dedup.db"
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "feishu_enabled": True,
            "feishu_webhook_url": "https://open.feishu.cn/hook/abc",
            "feishu_events": "review",
        }), encoding="utf-8")

        async def run():
            async with aiosqlite.connect(db_path) as conn:
                conn.row_factory = aiosqlite.Row
                await init_schema(conn)
                await migrate(conn)
                await conn.execute(
                    "INSERT INTO jobs (id, filename, status, total_pages) "
                    "VALUES ('job-x', 'f.pdf', 'review', 2)"
                )
                await conn.commit()
                orig = db_mod._db
                db_mod._db = conn
                resp = mock.MagicMock(status_code=200)
                resp.json.return_value = {"code": 0, "msg": "success"}
                try:
                    with mock.patch("config._config_path", return_value=cfg), \
                            mock.patch("core.notify.requests.post", return_value=resp) as m:
                        await notify_job("job-x", "review")
                        await notify_job("job-x", "review")
                    assert m.call_count == 1  # 第二次被 audit_log 去重拦截
                    cur = await conn.execute(
                        "SELECT COUNT(*) AS c FROM audit_log WHERE action = 'feishu_notify'"
                    )
                    row = await cur.fetchone()
                    assert row["c"] == 1
                finally:
                    db_mod._db = orig

        asyncio.run(run())


# ===========================================================================
# App-bot (self-built application DM) — Phase 12.1
# ===========================================================================


class TestShouldNotifyAppBot:
    def test_app_bot_requires_app_credentials(self):
        cfg = {"enabled": True, "mode": "app_bot", "app_id": "", "app_secret": "", "open_id": "ou_x", "events": ["review"]}
        assert not _should_notify("review", cfg)

    def test_app_bot_requires_receiver(self):
        cfg = {"enabled": True, "mode": "app_bot", "app_id": "cli_x", "app_secret": "s", "open_id": "", "mobile": "", "events": ["review"]}
        assert not _should_notify("review", cfg)

    def test_app_bot_ok_with_open_id(self):
        cfg = {"enabled": True, "mode": "app_bot", "app_id": "cli_x", "app_secret": "s", "open_id": "ou_1", "mobile": "", "events": ["review"]}
        assert _should_notify("review", cfg)

    def test_app_bot_ok_with_mobile_only(self):
        cfg = {"enabled": True, "mode": "app_bot", "app_id": "cli_x", "app_secret": "s", "open_id": "", "mobile": "13800000000", "events": ["review"]}
        assert _should_notify("review", cfg)

    def test_webhook_mode_still_requires_url(self):
        cfg = {"enabled": True, "mode": "webhook", "webhook_url": "", "events": ["review"]}
        assert not _should_notify("review", cfg)


class TestTenantAccessToken:
    def test_fetches_token_and_caches(self):
        import core.notify as notify_mod
        resp = mock.MagicMock(status_code=200)
        resp.json.return_value = {"code": 0, "tenant_access_token": "t-abc", "expire": 7200}
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        with mock.patch("core.notify.requests.post", return_value=resp) as m:
            ok, tok = notify_mod._get_tenant_access_token_sync("cli_x", "sec")
            ok2, tok2 = notify_mod._get_tenant_access_token_sync("cli_x", "sec")
        assert ok and tok == "t-abc"
        assert tok2 == tok
        assert m.call_count == 1  # cached

    def test_token_error_code_returns_failure(self):
        import core.notify as notify_mod
        resp = mock.MagicMock(status_code=200)
        resp.json.return_value = {"code": 10003, "msg": "bad secret"}
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        with mock.patch("core.notify.requests.post", return_value=resp):
            ok, detail = notify_mod._get_tenant_access_token_sync("cli_x", "wrong")
        assert not ok
        assert "10003" in detail

    def test_transport_error_returns_failure(self):
        import requests as _requests
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        with mock.patch("core.notify.requests.post",
                        side_effect=_requests.exceptions.ConnectionError("boom")):
            ok, detail = notify_mod._get_tenant_access_token_sync("cli_x", "sec")
        assert not ok
        assert "transport" in detail

    def test_token_swap_resets_cache(self):
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        resp1 = mock.MagicMock(status_code=200)
        resp1.json.return_value = {"code": 0, "tenant_access_token": "t-a", "expire": 7200}
        resp2 = mock.MagicMock(status_code=200)
        resp2.json.return_value = {"code": 0, "tenant_access_token": "t-b", "expire": 7200}
        with mock.patch("core.notify.requests.post", side_effect=[resp1, resp2]) as m:
            ok1, t1 = notify_mod._get_tenant_access_token_sync("cli_x", "sec")
            ok2, t2 = notify_mod._get_tenant_access_token_sync("cli_y", "sec2")
        assert t1 == "t-a" and t2 == "t-b"
        assert m.call_count == 2


class TestResolveOpenId:
    def test_resolves_mobile_to_open_id_and_caches(self):
        import core.notify as notify_mod
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t-abc", "expire": 7200}
        user_resp = mock.MagicMock(status_code=200)
        user_resp.json.return_value = {"code": 0, "data": {"user_list": [{"user_id": "ou_123", "mobile": "13800000000"}]}}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        with mock.patch("core.notify.requests.post", side_effect=[tok_resp, user_resp]) as m:
            ok, oid = notify_mod.resolve_open_id_sync("cli_x", "sec", "13800000000")
            ok2, oid2 = notify_mod.resolve_open_id_sync("cli_x", "sec", "13800000000")
        assert ok and oid == "ou_123"
        assert oid2 == oid
        assert m.call_count == 2  # token + resolve, cached after

    def test_empty_user_list_is_failure(self):
        import core.notify as notify_mod
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t-abc", "expire": 7200}
        user_resp = mock.MagicMock(status_code=200)
        user_resp.json.return_value = {"code": 0, "data": {"user_list": []}}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        with mock.patch("core.notify.requests.post", side_effect=[tok_resp, user_resp]):
            ok, detail = notify_mod.resolve_open_id_sync("cli_x", "sec", "13800000000")
        assert not ok
        assert "empty" in detail


class TestPostAppBot:
    def test_success_dm_send(self):
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t-abc", "expire": 7200}
        msg_resp = mock.MagicMock(status_code=200)
        msg_resp.json.return_value = {"code": 0, "msg": "success", "data": {"message_id": "om_1"}}
        with mock.patch("core.notify.requests.post", side_effect=[tok_resp, msg_resp]) as m:
            ok, detail = notify_mod._post_app_bot_sync("cli_x", "sec", "ou_1", "", "hello")
        assert ok and detail == "ok"
        url = m.call_args_list[1].args[0]
        payload = m.call_args_list[1].kwargs["json"]
        assert "/im/v1/messages" in url
        assert payload["receive_id"] == "ou_1"
        assert payload["msg_type"] == "text"
        assert "hello" in payload["content"]
        # auth header with Bearer token
        headers = m.call_args_list[1].kwargs["headers"]
        assert headers["Authorization"] == "Bearer t-abc"

    def test_content_json_escaped(self):
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
        msg_resp = mock.MagicMock(status_code=200)
        msg_resp.json.return_value = {"code": 0, "msg": "ok"}
        with mock.patch("core.notify.requests.post", side_effect=[tok_resp, msg_resp]) as m:
            ok, _ = notify_mod._post_app_bot_sync("cli_x", "s", "ou_1", "", '含"引号"和\\反斜杠')
        assert ok
        payload = m.call_args_list[1].kwargs["json"]
        parsed = json.loads(payload["content"])
        assert parsed["text"] == '含"引号"和\\反斜杠'

    def test_fatal_code_mapped_to_zh_no_retry(self):
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
        msg_resp = mock.MagicMock(status_code=200)
        msg_resp.json.return_value = {"code": 230013, "msg": "no availability"}
        with mock.patch("core.notify.requests.post", side_effect=[tok_resp, msg_resp]) as m:
            ok, detail = notify_mod._post_app_bot_sync("cli_x", "s", "ou_1", "", "hi")
        assert not ok
        assert "230013" in detail
        assert "可用范围" in detail
        assert m.call_count == 2  # token + 1 message attempt, no retry

    def test_retries_on_429_then_succeeds(self):
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
        resp_429 = mock.MagicMock(status_code=429)
        msg_resp = mock.MagicMock(status_code=200)
        msg_resp.json.return_value = {"code": 0, "msg": "ok"}
        with mock.patch("core.notify.requests.post", side_effect=[tok_resp, resp_429, msg_resp]) as m, \
                mock.patch("core.notify.time.sleep"):
            ok, _ = notify_mod._post_app_bot_sync("cli_x", "s", "ou_1", "", "hi")
        assert ok
        assert m.call_count == 3

    def test_missing_receiver_returns_failure(self):
        import core.notify as notify_mod
        ok, detail = notify_mod._post_app_bot_sync("cli_x", "s", "", "", "hi")
        assert not ok
        assert "receiver" in detail

    def test_resolves_open_id_from_mobile(self):
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
        user_resp = mock.MagicMock(status_code=200)
        user_resp.json.return_value = {"code": 0, "data": {"user_list": [{"user_id": "ou_999"}]}}
        msg_resp = mock.MagicMock(status_code=200)
        msg_resp.json.return_value = {"code": 0, "msg": "ok"}
        with mock.patch("core.notify.requests.post",
                        side_effect=[tok_resp, user_resp, msg_resp]) as m:
            ok, _ = notify_mod._post_app_bot_sync("cli_x", "s", "", "13800000000", "hi")
        assert ok
        payload = m.call_args_list[2].kwargs["json"]
        assert payload["receive_id"] == "ou_999"

    def test_uuid_deterministic_same_text(self):
        """同一文本 → 相同 uuid（防超时重试双发）；不同文本 → 不同 uuid。"""
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}

        def resp_ok():
            r = mock.MagicMock(status_code=200)
            r.json.return_value = {"code": 0, "msg": "ok"}
            return r

        with mock.patch("core.notify.requests.post",
                        side_effect=[resp_ok(), resp_ok()]):
            tok_resp = mock.MagicMock(status_code=200)
            tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
            # 第一次：token + msg
            notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
            with mock.patch("core.notify.requests.post",
                            side_effect=[tok_resp, resp_ok()]) as m1:
                notify_mod._post_app_bot_sync("cli_x", "s", "ou_1", "", "hello")
            uuid1 = m1.call_args_list[1].kwargs["json"]["uuid"]
            # 第二次（同文本，token 缓存已就绪）：仅 msg
            with mock.patch("core.notify.requests.post", side_effect=[resp_ok()]) as m2:
                notify_mod._post_app_bot_sync("cli_x", "s", "ou_1", "", "hello")
            uuid2 = m2.call_args_list[0].kwargs["json"]["uuid"]
            # 第三次（不同文本）：仅 msg
            with mock.patch("core.notify.requests.post", side_effect=[resp_ok()]) as m3:
                notify_mod._post_app_bot_sync("cli_x", "s", "ou_1", "", "hello world")
            uuid3 = m3.call_args_list[0].kwargs["json"]["uuid"]
        assert uuid1 == uuid2
        assert uuid1 != uuid3
        assert len(uuid1) <= 48

    def test_content_uses_json_dumps(self):
        """content 由 json.dumps 生成（不再手写转义）。"""
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
        msg_resp = mock.MagicMock(status_code=200)
        msg_resp.json.return_value = {"code": 0, "msg": "ok"}
        with mock.patch("core.notify.requests.post", side_effect=[tok_resp, msg_resp]) as m:
            ok, _ = notify_mod._post_app_bot_sync("cli_x", "s", "ou_1", "", '含"引号"和\\反斜杠\n换行')
        assert ok
        payload = m.call_args_list[1].kwargs["json"]
        assert payload["content"].startswith('{"text":')  # json.dumps 签名
        parsed = json.loads(payload["content"])
        assert parsed["text"] == '含"引号"和\\反斜杠\n换行'

    def test_99991663_self_heals_token(self):
        """99991663（token 失效窗口）→ 清缓存重拉 token → 单次重试成功。"""
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        tok1 = mock.MagicMock(status_code=200)
        tok1.json.return_value = {"code": 0, "tenant_access_token": "t-old", "expire": 7200}
        invalid = mock.MagicMock(status_code=200)
        invalid.json.return_value = {"code": 99991663, "msg": "token invalid"}
        tok2 = mock.MagicMock(status_code=200)
        tok2.json.return_value = {"code": 0, "tenant_access_token": "t-new", "expire": 7200}
        ok_resp = mock.MagicMock(status_code=200)
        ok_resp.json.return_value = {"code": 0, "msg": "ok"}
        with mock.patch("core.notify.requests.post",
                        side_effect=[tok1, invalid, tok2, ok_resp]) as m, \
                mock.patch("core.notify.time.sleep"):
            ok, detail = notify_mod._post_app_bot_sync("cli_x", "s", "ou_1", "", "hi")
        assert ok, detail
        assert m.call_count == 4  # token + msg(99991663) + token-refetch + msg(ok)
        # 第二次发送使用新 token
        headers2 = m.call_args_list[3].kwargs["headers"]
        assert headers2["Authorization"] == "Bearer t-new"

    def test_99991663_self_heal_fails_gracefully(self):
        """自愈时 token 重拉失败 → 返回失败而非死循环。"""
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        tok1 = mock.MagicMock(status_code=200)
        tok1.json.return_value = {"code": 0, "tenant_access_token": "t-old", "expire": 7200}
        invalid = mock.MagicMock(status_code=200)
        invalid.json.return_value = {"code": 99991663, "msg": "token invalid"}
        tok_fail = mock.MagicMock(status_code=500)
        with mock.patch("core.notify.requests.post",
                        side_effect=[tok1, invalid, tok_fail]) as m, \
                mock.patch("core.notify.time.sleep"):
            ok, detail = notify_mod._post_app_bot_sync("cli_x", "s", "ou_1", "", "hi")
        assert not ok
        assert "http 500" in detail
        assert m.call_count == 3

    def test_99991663_not_retried_twice(self):
        """99991663 只自愈一次——二次出现仍失败（防无限循环）。"""
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        tok1 = mock.MagicMock(status_code=200)
        tok1.json.return_value = {"code": 0, "tenant_access_token": "t-old", "expire": 7200}
        invalid1 = mock.MagicMock(status_code=200)
        invalid1.json.return_value = {"code": 99991663, "msg": "token invalid"}
        tok2 = mock.MagicMock(status_code=200)
        tok2.json.return_value = {"code": 0, "tenant_access_token": "t-new", "expire": 7200}
        invalid2 = mock.MagicMock(status_code=200)
        invalid2.json.return_value = {"code": 99991663, "msg": "token invalid"}
        with mock.patch("core.notify.requests.post",
                        side_effect=[tok1, invalid1, tok2, invalid2]) as m, \
                mock.patch("core.notify.time.sleep"):
            ok, detail = notify_mod._post_app_bot_sync("cli_x", "s", "ou_1", "", "hi")
        assert not ok
        assert "99991663" in detail
        assert m.call_count == 4  # 不再有第三次 token 重拉


class TestNotifyJobAppBot:
    def test_app_bot_mode_sends_dm_and_audits(self, tmp_path):
        """app_bot 模式：notify_job 走 DM 通道并写 audit_log。"""
        import aiosqlite
        from db.client import init_schema, migrate
        import db.client as db_mod
        import core.notify as notify_mod

        db_path = tmp_path / "t2.db"
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "feishu_enabled": True,
            "feishu_mode": "app_bot",
            "feishu_app_id": "cli_x",
            "feishu_app_secret": "sec",
            "feishu_open_id": "ou_1",
            "feishu_events": "review",
        }), encoding="utf-8")

        async def run():
            async with aiosqlite.connect(db_path) as conn:
                conn.row_factory = aiosqlite.Row
                await init_schema(conn)
                await migrate(conn)
                await conn.execute(
                    "INSERT INTO jobs (id, filename, status, total_pages) "
                    "VALUES ('job-x', 'f.pdf', 'review', 2)"
                )
                await conn.commit()
                orig = db_mod._db
                db_mod._db = conn
                notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
                notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
                tok_resp = mock.MagicMock(status_code=200)
                tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
                msg_resp = mock.MagicMock(status_code=200)
                msg_resp.json.return_value = {"code": 0, "msg": "ok"}
                try:
                    with mock.patch("config._config_path", return_value=cfg), \
                            mock.patch("core.notify.requests.post",
                                       side_effect=[tok_resp, msg_resp]):
                        await notify_mod.notify_job("job-x", "review")
                    cur = await conn.execute(
                        "SELECT detail FROM audit_log WHERE action = 'feishu_notify'"
                    )
                    rows = await cur.fetchall()
                    assert len(rows) == 1
                    assert "ok=True" in rows[0]["detail"]
                finally:
                    db_mod._db = orig

        asyncio.run(run())

    def test_app_bot_incomplete_config_skips(self, tmp_path):
        """app_bot 缺 app_secret → 不发请求。"""
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "feishu_enabled": True,
            "feishu_mode": "app_bot",
            "feishu_app_id": "cli_x",
            "feishu_open_id": "ou_1",
            "feishu_events": "review",
        }), encoding="utf-8")
        with mock.patch("config._config_path", return_value=cfg), \
                mock.patch("core.notify.requests.post") as m:
            asyncio.run(notify_job("job-x", "review"))
        m.assert_not_called()


# ===========================================================================
# Edge / failure branches (coverage)
# ===========================================================================


class TestEdgeBranches:
    """非 JSON 响应、异常状态码、空 token、resolve/token 链式失败等分支。"""

    def test_post_sync_non_json_body(self):
        resp = mock.MagicMock(status_code=200)
        resp.json.side_effect = ValueError("not json")
        with mock.patch("core.notify.requests.post", return_value=resp):
            ok, detail = _post_sync("u", {"msg_type": "text"}, "")
        assert not ok
        assert "non-JSON" in detail

    def test_post_app_bot_non_json_body(self):
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
        msg_resp = mock.MagicMock(status_code=200)
        msg_resp.json.side_effect = ValueError("not json")
        with mock.patch("core.notify.requests.post", side_effect=[tok_resp, msg_resp]):
            ok, detail = notify_mod._post_app_bot_sync("cli_x", "s", "ou_1", "", "hi")
        assert not ok
        assert "non-JSON" in detail

    def test_post_app_bot_unknown_business_code(self):
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
        msg_resp = mock.MagicMock(status_code=200)
        msg_resp.json.return_value = {"code": 99999, "msg": "mystery"}
        with mock.patch("core.notify.requests.post", side_effect=[tok_resp, msg_resp]):
            ok, detail = notify_mod._post_app_bot_sync("cli_x", "s", "ou_1", "", "hi")
        assert not ok
        assert "99999" in detail and "mystery" in detail

    def test_post_app_bot_transport_error_exhausts_retries(self):
        import requests as _requests
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
        with mock.patch("core.notify.requests.post", side_effect=[tok_resp] + [
            _requests.exceptions.ConnectionError("boom")] * 4) as m, \
                mock.patch("core.notify.time.sleep"):
            ok, detail = notify_mod._post_app_bot_sync("cli_x", "s", "ou_1", "", "hi")
        assert not ok
        assert "retries exhausted" in detail
        assert m.call_count == 5  # token + 4 msg attempts

    def test_token_http_error_non_json_empty(self):
        import core.notify as notify_mod
        # http != 200
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        resp = mock.MagicMock(status_code=500)
        with mock.patch("core.notify.requests.post", return_value=resp):
            ok, detail = notify_mod._get_tenant_access_token_sync("cli_x", "s")
        assert not ok and "http 500" in detail
        # non-JSON body
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        resp = mock.MagicMock(status_code=200)
        resp.json.side_effect = ValueError()
        with mock.patch("core.notify.requests.post", return_value=resp):
            ok, detail = notify_mod._get_tenant_access_token_sync("cli_x", "s")
        assert not ok and "non-JSON" in detail
        # business error
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        resp = mock.MagicMock(status_code=200)
        resp.json.return_value = {"code": 99991661, "msg": "no auth"}
        with mock.patch("core.notify.requests.post", return_value=resp):
            ok, detail = notify_mod._get_tenant_access_token_sync("cli_x", "s")
        assert not ok and "99991661" in detail
        # empty token
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        resp = mock.MagicMock(status_code=200)
        resp.json.return_value = {"code": 0, "tenant_access_token": ""}
        with mock.patch("core.notify.requests.post", return_value=resp):
            ok, detail = notify_mod._get_tenant_access_token_sync("cli_x", "s")
        assert not ok and "empty" in detail

    def _reset_caches(self):
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}

    def test_resolve_failure_branches(self):
        import core.notify as notify_mod
        # token 失败 → 直接返回
        self._reset_caches()
        resp = mock.MagicMock(status_code=500)
        with mock.patch("core.notify.requests.post", return_value=resp):
            ok, detail = notify_mod.resolve_open_id_sync("cli_x", "s", "13800000000")
        assert not ok
        # resolve transport error
        import requests as _requests
        self._reset_caches()
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
        with mock.patch("core.notify.requests.post", side_effect=[tok_resp] + [
            _requests.exceptions.ConnectionError()]):
            ok, detail = notify_mod.resolve_open_id_sync("cli_x", "s", "13800000000")
        assert not ok and "transport" in detail
        # resolve http error
        self._reset_caches()
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
        resp500 = mock.MagicMock(status_code=500)
        with mock.patch("core.notify.requests.post", side_effect=[tok_resp, resp500]):
            ok, detail = notify_mod.resolve_open_id_sync("cli_x", "s", "13800000000")
        assert not ok and "http 500" in detail
        # resolve non-JSON
        self._reset_caches()
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
        bad = mock.MagicMock(status_code=200)
        bad.json.side_effect = ValueError()
        with mock.patch("core.notify.requests.post", side_effect=[tok_resp, bad]):
            ok, detail = notify_mod.resolve_open_id_sync("cli_x", "s", "13800000000")
        assert not ok and "non-JSON" in detail
        # resolve business error
        self._reset_caches()
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
        biz = mock.MagicMock(status_code=200)
        biz.json.return_value = {"code": 99991663, "msg": "token invalid"}
        with mock.patch("core.notify.requests.post", side_effect=[tok_resp, biz]):
            ok, detail = notify_mod.resolve_open_id_sync("cli_x", "s", "13800000000")
        assert not ok and "99991663" in detail

    def test_app_bot_resolve_failure_propagates(self):
        """open_id 为空且手机号解析失败 → 消息体不带 receiver 即失败。"""
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        tok_resp = mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
        biz = mock.MagicMock(status_code=200)
        biz.json.return_value = {"code": 99991663, "msg": "token invalid"}
        with mock.patch("core.notify.requests.post", side_effect=[tok_resp, biz]) as m:
            ok, detail = notify_mod._post_app_bot_sync("cli_x", "s", "", "13800000000", "hi")
        assert not ok
        assert "99991663" in detail
        assert m.call_count == 2  # 未到发消息

    def test_app_bot_token_failure_propagates(self):
        """token 获取失败 → 不发消息直接失败（token 单次尝试，无消息流量）。"""
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        resp = mock.MagicMock(status_code=500)
        with mock.patch("core.notify.requests.post", return_value=resp) as m:
            ok, detail = notify_mod._post_app_bot_sync("cli_x", "s", "ou_1", "", "hi")
        assert not ok and "token" in detail
        assert m.call_count == 1  # 仅 token 尝试，未到消息阶段

    def test_notify_job_failure_logs_warning(self, tmp_path):
        """发送失败 → audit_log 记 ok=False，不抛异常。"""
        import aiosqlite
        from db.client import init_schema, migrate
        import db.client as db_mod

        db_path = tmp_path / "t3.db"
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "feishu_enabled": True,
            "feishu_webhook_url": "https://open.feishu.cn/hook/abc",
            "feishu_events": "review",
        }), encoding="utf-8")

        async def run():
            async with aiosqlite.connect(db_path) as conn:
                conn.row_factory = aiosqlite.Row
                await init_schema(conn)
                await migrate(conn)
                await conn.execute(
                    "INSERT INTO jobs (id, filename, status, total_pages) "
                    "VALUES ('job-x', 'f.pdf', 'review', 2)"
                )
                await conn.commit()
                orig = db_mod._db
                db_mod._db = conn
                resp = mock.MagicMock(status_code=200)
                resp.json.return_value = {"code": 19024, "msg": "kw"}
                try:
                    with mock.patch("config._config_path", return_value=cfg), \
                            mock.patch("core.notify.requests.post", return_value=resp):
                        await notify_job("job-x", "review")
                    cur = await conn.execute(
                        "SELECT detail FROM audit_log WHERE action = 'feishu_notify'"
                    )
                    rows = await cur.fetchall()
                    assert len(rows) == 1
                    assert "ok=False" in rows[0]["detail"]
                finally:
                    db_mod._db = orig

        asyncio.run(run())