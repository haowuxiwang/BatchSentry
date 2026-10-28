"""POST /api/settings 校验与落盘分支补测（api/settings/write.py）。

补全 test_api_settings.py 未覆盖的：
- 非本地 403、ocr_slices 边界、mineru_language 白名单、llm_provider 未注册
- per-provider 非法名 / clear_key 语义 / __CLEAR__ / 掩码回写跳过
- Paddle URL、provider base_url、feishu webhook 的 SSRF 拒绝
- 既有 config.json 损坏降级、原子写失败清理、审计写库失败旁路
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


import contextlib


@contextlib.asynccontextmanager
async def _settings_client(tmp_path, *, raise_app_exceptions: bool = True):
    """构造 Settings 客户端并重定向所有配置路径 + 隔离 provider 注册表。"""
    from main import app
    import api.settings as settings_mod
    from config import config as _cfg

    test_config = tmp_path / "config.json"
    saved_providers = dict(_cfg["providers"])
    with patch.object(settings_mod, "_settings_config_path", return_value=test_config), \
         patch("api.settings.rules._settings_config_path", return_value=test_config), \
         patch("api.settings.provider._settings_config_path", return_value=test_config), \
         patch("api.settings.write._settings_config_path", return_value=test_config), \
         patch("api.settings.read._settings_config_path", return_value=test_config), \
         patch("config._config_path", return_value=test_config):
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
            base_url="http://127.0.0.1:8000",
            headers={"Origin": "http://127.0.0.1:8000"},
        ) as c:
            try:
                yield c
            finally:
                _cfg["providers"].clear()
                _cfg["providers"].update(saved_providers)


@pytest_asyncio.fixture
async def client(test_db, tmp_path):
    async with _settings_client(tmp_path) as c:
        yield c


@pytest_asyncio.fixture
async def client_no_raise(test_db, tmp_path):
    async with _settings_client(tmp_path, raise_app_exceptions=False) as c:
        yield c


class TestUpdateSettingsValidation:
    @pytest.mark.asyncio
    async def test_non_local_rejected(self, test_db, tmp_path):
        from main import app
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.com:8000"
        ) as c:
            r = await c.post("/api/settings", json={"llm_provider": "deepseek"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_ocr_slices_valid(self, client):
        from config import config as _cfg
        orig = _cfg["app"].ocr_slices
        r = await client.post("/api/settings", json={"ocr_slices": 5})
        assert r.status_code == 200
        assert _cfg["app"].ocr_slices == 5
        _cfg["app"].ocr_slices = orig

    @pytest.mark.asyncio
    async def test_ocr_slices_below_one_rejected(self, client):
        r = await client.post("/api/settings", json={"ocr_slices": 0})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_mineru_language_rejected(self, client):
        r = await client.post("/api/settings", json={"mineru_language": "klingon"})
        assert r.status_code == 400
        assert "mineru_language" in json.dumps(r.json(), ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_unregistered_llm_provider_rejected(self, client):
        r = await client.post("/api/settings", json={"llm_provider": "ghostprov"})
        assert r.status_code == 400
        assert "not a registered provider" in json.dumps(r.json(), ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_paddle_url_private_network_rejected(self, client):
        r = await client.post(
            "/api/settings", json={"paddle_ocr_api_url": "http://127.0.0.1:9/ocr"}
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_provider_base_url_private_network_rejected(self, client):
        r = await client.post(
            "/api/settings", json={"glm_base_url": "http://10.0.0.5/v1"}
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_feishu_webhook_private_network_rejected(self, client):
        r = await client.post(
            "/api/settings", json={"feishu_webhook_url": "http://127.0.0.1:9/hook"}
        )
        assert r.status_code == 400


class TestPerProviderFields:
    @pytest.mark.asyncio
    async def test_invalid_provider_name_in_field_rejected(self, client):
        r = await client.post("/api/settings", json={"BAD_api_key": "sk-x"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_clear_key_name_rejected(self, client):
        r = await client.post("/api/settings", json={"BAD_clear_key": True})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_clear_key_false_is_ignored(self, client):
        r = await client.post("/api/settings", json={"siliconflow_clear_key": False})
        assert r.status_code == 200
        assert r.json()["updated"] == 0

    @pytest.mark.asyncio
    async def test_provider_api_key_clear_literal(self, client):
        from config import config as _cfg
        r = await client.post(
            "/api/settings", json={"llm_providers_add": "glm", "glm_api_key": "__CLEAR__"}
        )
        assert r.status_code == 200
        assert _cfg["providers"]["glm"].api_key == ""

    @pytest.mark.asyncio
    async def test_mask_roundtrip_is_skipped(self, client):
        from api.settings import _mask
        await client.post("/api/settings", json={"llm_providers_add": "glm"})
        real = "glm-realkey-1234567890"
        await client.post("/api/settings", json={"glm_api_key": real})
        r = await client.post("/api/settings", json={"glm_api_key": _mask(real)})
        assert r.status_code == 200
        body = r.json()
        assert body["updated"] == 0
        assert "glm_api_key" in body.get("skipped", [])


class TestPersistBranches:
    @pytest.mark.asyncio
    async def test_corrupt_existing_config_recovers(self, client, tmp_path):
        (tmp_path / "config.json").write_text("{ broken", encoding="utf-8")
        r = await client.post("/api/settings", json={"llm_provider": "deepseek"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_write_failure_cleans_tmp(self, client_no_raise, tmp_path):
        import api.settings.write as write_mod
        with patch.object(write_mod.json, "dump", side_effect=OSError("disk full")):
            r = await client_no_raise.post(
                "/api/settings", json={"llm_provider": "deepseek"}
            )
        assert r.status_code == 500
        assert not list(tmp_path.glob("config.json.tmp.*"))

    @pytest.mark.asyncio
    async def test_audit_failure_still_saves(self, client, monkeypatch):
        import api.settings.write as write_mod

        async def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(write_mod, "get_db", boom)
        r = await client.post("/api/settings", json={"llm_provider": "deepseek"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
