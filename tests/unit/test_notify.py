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
from pathlib import Path

import pytest

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
        assert "3 critical / 5 warning / 2 info" in text

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
        with mock.patch("core.notify.requests.post", side_effect=[resp_429, resp_ok]) as m, \
                mock.patch("core.notify.time.sleep"):
            ok, detail = _post_sync("u", {"msg_type": "text"}, "")
        assert ok

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
        from db.client import SCHEMA_VERSION, init_schema, migrate
        from config import config as _cfg

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