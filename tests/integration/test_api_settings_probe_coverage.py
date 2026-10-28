"""api/settings/probe.py 边界分支补测。

补全 test_api_settings.py 未触及的错误/回退路径：
- _audit_llm_test 成功与写库失败
- test_provider：非本地 403、掩码 Key 拒绝、SSRF base_url 拒绝、
  model/protocol 覆盖、adapter 成功、各类错误文案映射
- test_feishu：非本地 403、未知模式、app_bot 缺凭据、审计写失败旁路、
  webhook 未配置 URL
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(test_db, tmp_path):
    """Settings API 客户端（重定向 config.json + 本地 Host）。"""
    from main import app
    import api.settings as settings_mod

    test_config = tmp_path / "config.json"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings_mod, "_settings_config_path", lambda: test_config)
        mp.setattr("api.settings._config_path", lambda: test_config)
        mp.setattr("config._config_path", lambda: test_config)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
            headers={"Origin": "http://127.0.0.1:8000"},
        ) as c:
            yield c


class _FakeAdapter:
    def __init__(self, exc: Exception | None = None):
        self.exc = exc

    async def chat(self, **kwargs):
        if self.exc is not None:
            raise self.exc
        return {"ok": True}


async def _set_real_key(client) -> None:
    """写入一个'真实'格式的 deepseek key，使 test_provider 越过未配置分支。"""
    await client.post("/api/settings", json={
        "llm_provider": "deepseek",
        "deepseek_api_key": "sk-real-deepseek-key-123456",
    })


# ─── _audit_llm_test ───────────────────────────────────────────


class TestAuditLlmTest:
    @pytest.mark.asyncio
    async def test_writes_audit_row(self, test_db):
        from api.settings.probe import _audit_llm_test
        await _audit_llm_test("deepseek", "llm_test_ok", "detail-x")
        cur = await test_db.execute(
            "SELECT detail FROM audit_log WHERE action = 'llm_test_ok'"
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == "detail-x"

    @pytest.mark.asyncio
    async def test_swallows_db_failure(self, monkeypatch):
        """写审计失败只告警，不抛异常。"""
        import api.settings.probe as probe

        async def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(probe, "get_db", boom)
        await probe._audit_llm_test("deepseek", "llm_test_failed", "x")  # 不抛异常


# ─── test_provider ─────────────────────────────────────────────


class TestProviderProbeBranches:
    @pytest.mark.asyncio
    async def test_non_local_request_rejected(self, test_db, tmp_path):
        from main import app
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.com:8000"
        ) as c:
            r = await c.post("/api/settings/test_provider", json={"provider": "deepseek"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_masked_key_rejected(self, client):
        await _set_real_key(client)
        from api.settings import _mask
        masked = _mask("sk-real-deepseek-key-123456")
        r = await client.post("/api/settings/test_provider", json={
            "provider": "deepseek", "api_key": masked,
        })
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "掩码" in r.json()["reason"]

    @pytest.mark.asyncio
    async def test_private_base_url_rejected(self, client):
        await _set_real_key(client)
        r = await client.post("/api/settings/test_provider", json={
            "provider": "deepseek",
            "api_key": "sk-another-real-key-999999",
            "base_url": "http://127.0.0.1:9999/v1",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is False

    @pytest.mark.asyncio
    async def test_success_with_model_and_protocol_override(self, client, monkeypatch):
        await _set_real_key(client)
        monkeypatch.setattr("llm.adapters.get_adapter", lambda cfg: _FakeAdapter())
        r = await client.post("/api/settings/test_provider", json={
            "provider": "deepseek",
            "api_key": "sk-another-real-key-999999",
            "model": "override-model",
            "protocol": "openai",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["model"] == "override-model"
        assert "latency_ms" in data

    @pytest.mark.parametrize("exc,kw", [
        (RuntimeError("HTTP 401 unauthorized"), "API Key 无效"),
        (RuntimeError("403 forbidden"), "访问被拒绝"),
        (RuntimeError("request timed out"), "超时"),
        (RuntimeError("dns resolve failure"), "无法连接"),
        (ValueError("weird internal error sk-secret-xyz"), "测试失败"),
    ])
    @pytest.mark.asyncio
    async def test_error_reason_mapping(self, client, monkeypatch, exc, kw):
        await _set_real_key(client)
        monkeypatch.setattr(
            "llm.adapters.get_adapter", lambda cfg: _FakeAdapter(exc=exc)
        )
        r = await client.post("/api/settings/test_provider", json={
            "provider": "deepseek",
            "api_key": "sk-another-real-key-999999",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert kw in data["reason"]


# ─── test_feishu ───────────────────────────────────────────────


class TestFeishuProbeBranches:
    @pytest.mark.asyncio
    async def test_non_local_request_rejected(self, test_db):
        from main import app
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.com:8000"
        ) as c:
            r = await c.post("/api/settings/test_feishu", json={})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_mode(self, client):
        r = await client.post("/api/settings/test_feishu", json={"mode": "carrier-pigeon"})
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "未知模式" in r.json()["reason"]

    @pytest.mark.asyncio
    async def test_app_bot_missing_credentials(self, client):
        r = await client.post("/api/settings/test_feishu", json={"mode": "app_bot"})
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "App ID" in r.json()["reason"]

    @pytest.mark.asyncio
    async def test_app_bot_success_survives_audit_failure(self, client, monkeypatch):
        """app_bot 发送成功但审计写库抛异常 → 仍返回 ok=True（旁路）。"""
        monkeypatch.setattr("core.notify._post_app_bot_sync", lambda *a, **k: (True, ""))
        import api.settings.probe as probe

        async def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(probe, "get_db", boom)
        r = await client.post("/api/settings/test_feishu", json={
            "mode": "app_bot",
            "app_id": "cli_x",
            "app_secret": "sec-real-value-123",
            "open_id": "ou_1",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_app_bot_business_error_hint(self, client, monkeypatch):
        """app_bot 失败带业务码 → 映射为中文提示。"""
        monkeypatch.setattr(
            "core.notify._post_app_bot_sync",
            lambda *a, **k: (False, "code=230006 something"),
        )
        r = await client.post("/api/settings/test_feishu", json={
            "mode": "app_bot",
            "app_id": "cli_x",
            "app_secret": "sec-real-value-123",
            "open_id": "ou_1",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is False

    @pytest.mark.asyncio
    async def test_webhook_missing_url(self, client):
        r = await client.post("/api/settings/test_feishu", json={"mode": "webhook"})
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "webhook" in r.json()["reason"]

    @pytest.mark.asyncio
    async def test_webhook_success_survives_audit_failure(self, client, monkeypatch):
        import api.settings.probe as probe

        async def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(probe, "get_db", boom)
        monkeypatch.setattr("core.notify._post_sync", lambda *a, **k: (True, ""))
        r = await client.post("/api/settings/test_feishu", json={
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/tok-x",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True
