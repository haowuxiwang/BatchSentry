"""api/settings/provider.py 边界分支补测。

补全 test_api_settings.py 未覆盖的：
- _persist_config_field 损坏 JSON 降级 / 写盘失败清理 tmp 并上抛
- set_active_provider 非本地 403、审计写库失败旁路
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(test_db, tmp_path):
    from main import app
    import api.settings as settings_mod

    test_config = tmp_path / "config.json"
    with patch.object(settings_mod, "_settings_config_path", return_value=test_config), \
         patch("api.settings.provider._settings_config_path", return_value=test_config), \
         patch("config._config_path", return_value=test_config):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
            headers={"Origin": "http://127.0.0.1:8000"},
        ) as c:
            yield c


class TestPersistConfigField:
    def test_corrupt_existing_config_replaced(self, tmp_path, monkeypatch):
        import api.settings.provider as provider
        cfg = tmp_path / "config.json"
        cfg.write_text("{ broken json", encoding="utf-8")
        monkeypatch.delenv("PBC_PROV_TEST", raising=False)
        with patch.object(provider, "_settings_config_path", return_value=cfg):
            provider._persist_config_field("PBC_PROV_TEST", "v")
        assert json.loads(cfg.read_text(encoding="utf-8")) == {"PBC_PROV_TEST": "v"}
        monkeypatch.delenv("PBC_PROV_TEST", raising=False)

    def test_write_failure_cleans_tmp_and_raises(self, tmp_path):
        import api.settings.provider as provider
        cfg = tmp_path / "config.json"
        with patch.object(provider, "_settings_config_path", return_value=cfg), patch(
            "json.dump", side_effect=OSError("disk full")
        ):
            with pytest.raises(OSError):
                provider._persist_config_field("PBC_PROV_FAIL", "v")
        assert not list(tmp_path.glob("config.json.tmp.*"))


class TestSetActiveProvider:
    @pytest.mark.asyncio
    async def test_non_local_rejected(self, test_db):
        from main import app
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.com:8000"
        ) as c:
            r = await c.post(
                "/api/settings/set_active_provider", json={"provider": "deepseek"}
            )
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_failure_still_switches(self, client, monkeypatch):
        """审计写库失败不影响切换（best-effort 留痕）。"""
        import api.settings.provider as provider

        async def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(provider, "get_db", boom)
        r = await client.post(
            "/api/settings/set_active_provider", json={"provider": "deepseek"}
        )
        assert r.status_code == 200
        assert r.json()["active_provider"] == "deepseek"

    @pytest.mark.asyncio
    async def test_invalid_name_rejected(self, client):
        # 注意："BAD_NAME" 会被 lower() 成 "bad_name"，仍通过 ^[a-z0-9_-]{2,32}$
        # → 走到 404（未注册）。要命中 400 必须用真正非法字符/长度的名字。
        r = await client.post(
            "/api/settings/set_active_provider", json={"provider": "bad name!"}
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_provider_returns_404(self, client):
        r = await client.post(
            "/api/settings/set_active_provider", json={"provider": "ghost"}
        )
        assert r.status_code == 404
