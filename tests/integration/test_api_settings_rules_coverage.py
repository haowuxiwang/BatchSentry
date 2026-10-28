"""api/settings/rules.py 边界分支补测。

补全 TestUserRules 未覆盖的：
- _read_raw_config 损坏 JSON 降级
- _write_raw_config 写盘失败清理 tmp 并上抛
- GET 非本地 403、命中统计 DB 异常降级
- PUT 总字数超限 400
- PUT 审计写库失败时仍成功 / 校验失败仍 400
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
         patch("api.settings.rules._settings_config_path", return_value=test_config), \
         patch("config._config_path", return_value=test_config):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
            headers={"Origin": "http://127.0.0.1:8000"},
        ) as c:
            yield c


class TestRawConfigHelpers:
    def test_read_corrupt_json_falls_back_empty(self, tmp_path):
        import api.settings.rules as rules
        cfg = tmp_path / "config.json"
        cfg.write_text("{ not json !!!", encoding="utf-8")
        with patch.object(rules, "_settings_config_path", return_value=cfg):
            assert rules._read_raw_config() == {}

    def test_write_failure_cleans_tmp_and_raises(self, tmp_path):
        import api.settings.rules as rules
        cfg = tmp_path / "config.json"
        with patch.object(rules, "_settings_config_path", return_value=cfg), patch(
            "json.dump", side_effect=OSError("disk full")
        ):
            with pytest.raises(OSError):
                rules._write_raw_config({"user_rules": []})
        assert not list(tmp_path.glob("config.json.tmp.*"))


class TestRulesEndpointBranches:
    @pytest.mark.asyncio
    async def test_get_non_local_rejected(self, test_db, tmp_path):
        from main import app
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.com:8000"
        ) as c:
            r = await c.get("/api/settings/rules")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_get_stats_db_failure_degrades(self, client, monkeypatch):
        """命中统计查询抛异常 → 仍 200，hits 为空、last_saved_at 为 None。"""
        import db.client as db_mod

        async def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(db_mod, "get_db", boom)
        r = await client.get("/api/settings/rules")
        assert r.status_code == 200
        body = r.json()
        assert body["hits"] == {}
        assert body["last_saved_at"] is None

    @pytest.mark.asyncio
    async def test_put_rejects_total_char_limit(self, client):
        # 9 条 × 950 字 = 8550 > 8000
        rules = [{"text": "规" * 950, "active": True} for _ in range(9)]
        r = await client.put("/api/settings/rules", json={"rules": rules})
        assert r.status_code == 400
        assert "总字数" in json.dumps(r.json(), ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_put_success_survives_audit_failure(self, client, monkeypatch):
        """审计写库失败不影响保存成功（best-effort 留痕）。"""
        import db.client as db_mod
        real_get_db = db_mod.get_db
        state = {"calls": 0}

        async def flaky():
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("audit db down")
            return await real_get_db()

        monkeypatch.setattr(db_mod, "get_db", flaky)
        r = await client.put("/api/settings/rules", json={
            "rules": [{"text": "设备清洁后需双人复核", "active": True}],
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_put_validation_error_survives_audit_failure(self, client, monkeypatch):
        """校验失败写审计也失败 → 仍返回 400。"""
        import db.client as db_mod

        async def boom():
            raise RuntimeError("audit db down")

        monkeypatch.setattr(db_mod, "get_db", boom)
        r = await client.put("/api/settings/rules", json={
            "rules": [{"text": "   ", "active": True}],
        })
        assert r.status_code == 400
