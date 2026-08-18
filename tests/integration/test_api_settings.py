"""Settings API 集成测试。

覆盖：
- GET /api/settings 返回脱敏配置
- POST /api/settings 更新配置 + 内存同步
- 供应商切换 bug 修复验证
"""
import unittest.mock
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient


@pytest_asyncio.fixture
async def settings_client(test_db, tmp_path):
    """提供 Settings API 测试客户端，隔离 config.json 文件避免 Windows 文件锁。

    Phase 9: 配置系统从 .env 迁移到 JSON 后，Settings API 写入 config.json。
    将 _settings_config_path 重定向到 tmp_path/config.json，避免污染项目根。
    """
    from main import app
    from httpx import ASGITransport
    import api.settings as settings_mod

    # 重定向 _settings_config_path 到临时目录（无需备份 — 测试用的 JSON
    # 内容由 POST /api/settings 在测试中按需生成，不需要预置）
    test_config = tmp_path / "config.json"

    with patch.object(settings_mod, "_settings_config_path", return_value=test_config), \
         patch("api.settings._config_path", return_value=test_config), \
         patch("config._config_path", return_value=test_config):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
            headers={"Origin": "http://127.0.0.1:8000"},
        ) as client:
            yield client


class TestGetSettings:
    """GET /api/settings。"""

    @pytest.mark.asyncio
    async def test_returns_all_sections(self, settings_client):
        r = await settings_client.get("/api/settings")
        assert r.status_code == 200
        data = r.json()
        assert "llm" in data
        assert "ocr" in data
        assert "app" in data
        assert "config_file" in data

    @pytest.mark.asyncio
    async def test_llm_has_provider_and_both_providers(self, settings_client):
        r = await settings_client.get("/api/settings")
        data = r.json()
        assert "provider" in data["llm"]
        assert "deepseek" in data["llm"]
        assert "siliconflow" in data["llm"]

    @pytest.mark.asyncio
    async def test_ocr_has_backend_and_both_backends(self, settings_client):
        r = await settings_client.get("/api/settings")
        data = r.json()
        assert "backend" in data["ocr"]
        assert "paddle" in data["ocr"]
        assert "mineru" in data["ocr"]

    @pytest.mark.asyncio
    async def test_api_keys_are_masked(self, settings_client):
        """API key 应脱敏显示。"""
        r = await settings_client.get("/api/settings")
        data = r.json()
        # 如果有 key，应包含 ****
        deepseek_key = data["llm"]["deepseek"]["api_key"]
        if deepseek_key and len(deepseek_key) > 12:
            assert "****" in deepseek_key

    @pytest.mark.asyncio
    async def test_configured_flag_present(self, settings_client):
        r = await settings_client.get("/api/settings")
        data = r.json()
        assert "configured" in data["llm"]["deepseek"]
        assert "configured" in data["llm"]["siliconflow"]
        assert "configured" in data["ocr"]["paddle"]
        assert "configured" in data["ocr"]["mineru"]


class TestUpdateSettings:
    """POST /api/settings — 供应商切换 bug 的核心验证。"""

    @pytest.mark.asyncio
    async def test_switch_llm_provider_takes_effect_immediately(self, settings_client):
        """切换 LLM 供应商应立即生效（修复 bug 的关键测试）。"""
        # 切换到 siliconflow
        r = await settings_client.post("/api/settings", json={"llm_provider": "siliconflow"})
        assert r.status_code == 200
        assert "立即生效" in r.json()["message"]

        # 验证 GET 返回新值
        r2 = await settings_client.get("/api/settings")
        assert r2.json()["llm"]["provider"] == "siliconflow"

        # 恢复
        await settings_client.post("/api/settings", json={"llm_provider": "deepseek"})

    @pytest.mark.asyncio
    async def test_switch_ocr_backend_takes_effect_immediately(self, settings_client):
        """切换 OCR 后端应立即生效。"""
        r = await settings_client.post("/api/settings", json={"ocr_backend": "mineru"})
        assert r.status_code == 200

        r2 = await settings_client.get("/api/settings")
        assert r2.json()["ocr"]["backend"] == "mineru"

        # 恢复
        await settings_client.post("/api/settings", json={"ocr_backend": "paddle"})

    @pytest.mark.asyncio
    async def test_update_api_key(self, settings_client):
        r = await settings_client.post("/api/settings", json={"deepseek_api_key": "sk-test-new"})
        assert r.status_code == 200
        assert r.json()["updated"] == 1

    @pytest.mark.asyncio
    async def test_empty_update_returns_message(self, settings_client):
        """无字段更新应返回提示。"""
        r = await settings_client.post("/api/settings", json={})
        assert r.status_code == 200
        assert "无更新" in r.json()["message"]

    @pytest.mark.asyncio
    async def test_update_mineru_bool_fields(self, settings_client):
        r = await settings_client.post("/api/settings", json={
            "mineru_enable_formula": False,
            "mineru_enable_table": True,
        })
        assert r.status_code == 200

        # 验证生效
        r2 = await settings_client.get("/api/settings")
        assert r2.json()["ocr"]["mineru"]["enable_formula"] is False
        assert r2.json()["ocr"]["mineru"]["enable_table"] is True

    @pytest.mark.asyncio
    async def test_empty_base_url_allowed_restores_default(self, settings_client):
        """对抗审查(cr-10): 空 base_url = 恢复 SDK 默认地址，应放行而非
        被 SSRF 校验拒绝（此前会 400，用户只能手改文件）。"""
        r = await settings_client.post("/api/settings", json={
            "deepseek_base_url": "",
        })
        assert r.status_code == 200
        assert r.json()["updated"] == 1
        # 确认内存配置也同步为空（恢复默认）
        from config import config as _cfg
        assert _cfg["providers"]["deepseek"].base_url == ""


class TestDynamicProviders:
    """Phase 7: 动态 provider 注册表测试。

    覆盖：
    - GET /api/settings 返回 providers list（不只是 deepseek/siliconflow）
    - llm_providers_add 添加新 provider
    - <provider>_<field> 动态字段更新
    - protocol 白名单校验
    - provider 名校验（拒绝非法字符）
    - 切换到新 provider 立即生效
    """

    @pytest.mark.asyncio
    async def test_get_returns_providers_list(self, settings_client):
        """GET 应返回 llm.providers list，至少包含 deepseek 和 siliconflow。"""
        r = await settings_client.get("/api/settings")
        data = r.json()
        assert "providers" in data["llm"]
        providers = data["llm"]["providers"]
        assert isinstance(providers, list)
        names = [p["name"] for p in providers]
        assert "deepseek" in names
        assert "siliconflow" in names
        # 每个 provider 应有完整字段
        for p in providers:
            assert "protocol" in p
            assert "api_key" in p
            assert "base_url" in p
            assert "model" in p
            assert "configured" in p

    @pytest.mark.asyncio
    async def test_add_new_provider(self, settings_client):
        """通过 llm_providers_add 添加 glm provider。"""
        r = await settings_client.post("/api/settings", json={
            "llm_providers_add": "glm",
            "glm_protocol": "openai",
            "glm_api_key": "sk-glm-1234567890abcdef1234567890",
            "glm_base_url": "https://open.bigmodel.cn/api/paas/v4",
            "glm_model": "glm-4-plus",
        })
        assert r.status_code == 200, r.text
        # 验证 glm 出现在 providers list
        r2 = await settings_client.get("/api/settings")
        names = [p["name"] for p in r2.json()["llm"]["providers"]]
        assert "glm" in names
        # 验证字段已写入
        glm = next(p for p in r2.json()["llm"]["providers"] if p["name"] == "glm")
        assert glm["protocol"] == "openai"
        assert glm["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
        assert glm["model"] == "glm-4-plus"
        assert glm["configured"] is True

    @pytest.mark.asyncio
    async def test_add_multiple_providers(self, settings_client):
        """一次添加多个 provider。"""
        r = await settings_client.post("/api/settings", json={
            "llm_providers_add": "kimi,qwen",
        })
        assert r.status_code == 200
        r2 = await settings_client.get("/api/settings")
        names = [p["name"] for p in r2.json()["llm"]["providers"]]
        assert "kimi" in names
        assert "qwen" in names

    @pytest.mark.asyncio
    async def test_remove_provider_persisted(self, settings_client):
        """通过 llm_providers_remove 移除自定义 provider 应持久化。"""
        # 先添加 kimi
        r = await settings_client.post("/api/settings", json={
            "llm_providers_add": "kimi",
        })
        assert r.status_code == 200
        # 移除它
        r = await settings_client.post("/api/settings", json={
            "llm_providers_remove": "kimi",
        })
        assert r.status_code == 200, r.text
        # GET 确认不在 providers list
        r2 = await settings_client.get("/api/settings")
        names = [p["name"] for p in r2.json()["llm"]["providers"]]
        assert "kimi" not in names

    @pytest.mark.asyncio
    async def test_remove_builtin_provider_idempotent(self, settings_client):
        """移除内置 provider（deepseek）应幂等无副作用。"""
        r = await settings_client.post("/api/settings", json={
            "llm_providers_remove": "deepseek",
        })
        assert r.status_code == 200, r.text
        r2 = await settings_client.get("/api/settings")
        names = [p["name"] for p in r2.json()["llm"]["providers"]]
        assert "deepseek" in names

    @pytest.mark.asyncio
    async def test_remove_unknown_provider_no_error(self, settings_client):
        """移除不存在的 provider 不应报错。"""
        r = await settings_client.post("/api/settings", json={
            "llm_providers_remove": "not-a-real-provider",
        })
        assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_switch_to_new_provider(self, settings_client):
        """添加 provider 后切换到它应立即生效。"""
        # 添加 + 配置 anthropic（不实际安装 SDK，只测配置写入）
        r = await settings_client.post("/api/settings", json={
            "llm_providers_add": "anthropictest",
            "anthropictest_protocol": "anthropic",
            "anthropictest_api_key": "sk-ant-test",
            "anthropictest_base_url": "https://api.anthropic.com",
            "anthropictest_model": "claude-3-5-sonnet",
            "llm_provider": "anthropictest",
        })
        assert r.status_code == 200, r.text
        # 验证 GET 返回新 provider 为激活
        r2 = await settings_client.get("/api/settings")
        assert r2.json()["llm"]["provider"] == "anthropictest"
        # 恢复到 deepseek 避免污染其他测试
        await settings_client.post("/api/settings", json={"llm_provider": "deepseek"})

    @pytest.mark.asyncio
    async def test_invalid_protocol_rejected(self, settings_client):
        """非法 protocol 应返回 400 + errors。"""
        r = await settings_client.post("/api/settings", json={
            "llm_providers_add": "badproto",
            "badproto_protocol": "weird-protocol",
        })
        assert r.status_code == 400
        detail = r.json()["detail"]
        # detail 可能是 {"errors": [...]} 或字符串
        if isinstance(detail, dict):
            errs = detail.get("errors", [])
        else:
            errs = [str(detail)]
        assert any("protocol" in str(e).lower() for e in errs)

    @pytest.mark.asyncio
    async def test_invalid_provider_name_rejected(self, settings_client):
        """非法 provider 名（含特殊字符）应返回 400。"""
        r = await settings_client.post("/api/settings", json={
            "llm_providers_add": "../etc/passwd",
        })
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_llm_provider_name_rejected(self, settings_client):
        """非法 llm_provider 名应返回 400。"""
        r = await settings_client.post("/api/settings", json={
            "llm_provider": "INVALID NAME WITH SPACES",
        })
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_per_provider_field_update(self, settings_client):
        """更新已有 provider 的字段。"""
        # 先确保 deepseek 存在
        r = await settings_client.post("/api/settings", json={
            "deepseek_model": "deepseek-new-model",
        })
        assert r.status_code == 200
        r2 = await settings_client.get("/api/settings")
        deepseek = next(
            p for p in r2.json()["llm"]["providers"] if p["name"] == "deepseek"
        )
        assert deepseek["model"] == "deepseek-new-model"
        # 恢复
        await settings_client.post("/api/settings", json={
            "deepseek_model": "deepseek-chat",
        })

    @pytest.mark.asyncio
    async def test_unknown_field_ignored(self, settings_client):
        """非白名单字段应被静默忽略（不写入 config.json）。"""
        r = await settings_client.post("/api/settings", json={
            "random_unknown_field": "should_be_ignored",
            "deepseek_model": "deepseek-chat",  # 加一个合法字段避免 "无更新"
        })
        assert r.status_code == 200
        # random_unknown_field 不应出现在更新字段列表
        assert "RANDOM_UNKNOWN_FIELD" not in r.json()["fields"]

    @pytest.mark.asyncio
    async def test_response_includes_providers_list(self, settings_client):
        """POST 响应应包含最新的 providers list（避免前端二次 GET）。"""
        r = await settings_client.post("/api/settings", json={
            "deepseek_model": "deepseek-chat",
        })
        assert r.status_code == 200
        data = r.json()
        assert "providers" in data
        assert isinstance(data["providers"], list)


class TestSetActiveProviderEndpoint:
    """S1: POST /api/settings/set_active_provider 端点测试。"""

    @pytest.mark.asyncio
    async def test_switch_active_provider_persists(self, settings_client):
        """切换 active provider 应持久化到 config.json + 内存热更新。"""
        # 先确保 siliconflow 已注册并有 key
        await settings_client.post("/api/settings", json={
            "llm_provider": "deepseek",
            "siliconflow_api_key": "sk-realkey1234567890abcdef",
        })
        # 切换到 siliconflow
        r = await settings_client.post("/api/settings/set_active_provider", json={
            "provider": "siliconflow",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["active_provider"] == "siliconflow"

        # 验证已持久化（再次 GET 应返回新的 active_provider）
        r2 = await settings_client.get("/api/settings")
        data2 = r2.json()
        assert data2["llm"]["active_provider"] == "siliconflow"
        assert data2["llm"]["provider"] == "siliconflow"  # 向后兼容字段

    @pytest.mark.asyncio
    async def test_switch_to_unknown_provider_returns_404(self, settings_client):
        """切换到不存在的 provider 应返回 404。"""
        r = await settings_client.post("/api/settings/set_active_provider", json={
            "provider": "nonexistent_provider",
        })
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_switch_to_invalid_name_returns_400(self, settings_client):
        """非法 provider 名应返回 400。"""
        r = await settings_client.post("/api/settings/set_active_provider", json={
            "provider": "Invalid Name With Spaces",
        })
        assert r.status_code == 400


class TestTestProviderEndpoint:
    """S4: POST /api/settings/test_provider 端点测试。"""

    @pytest.mark.asyncio
    async def test_test_provider_without_key_returns_not_configured(self, settings_client):
        """测试未配置 Key 的 provider 应返回 ok=False + 中文 reason。"""
        # 确保 deepseek 没配置 key
        await settings_client.post("/api/settings", json={
            "llm_provider": "deepseek",
            "deepseek_clear_key": True,
        })
        r = await settings_client.post("/api/settings/test_provider", json={
            "provider": "deepseek",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert "未配置" in data["reason"] or "密钥" in data["reason"]

    @pytest.mark.asyncio
    async def test_test_unknown_provider_returns_404(self, settings_client):
        """测试不存在的 provider 应返回 404。"""
        r = await settings_client.post("/api/settings/test_provider", json={
            "provider": "nonexistent_xyz",
        })
        assert r.status_code == 404


class TestOcrTokenClear:
    """S6: OCR token 清除（__CLEAR__ 标记）测试。"""

    @pytest.mark.asyncio
    async def test_clear_paddle_ocr_token(self, settings_client):
        """发送 __CLEAR__ 应将 paddle_ocr_token 清空。"""
        # 先设置一个 token
        await settings_client.post("/api/settings", json={
            "llm_provider": "deepseek",
            "paddle_ocr_token": "some_token_value",
        })
        # 验证已设置
        r1 = await settings_client.get("/api/settings")
        assert r1.json()["ocr"]["paddle"]["configured"] is True

        # 清除
        r2 = await settings_client.post("/api/settings", json={
            "llm_provider": "deepseek",
            "paddle_ocr_token": "__CLEAR__",
        })
        assert r2.status_code == 200

        # 验证已清空
        r3 = await settings_client.get("/api/settings")
        assert r3.json()["ocr"]["paddle"]["configured"] is False

    @pytest.mark.asyncio
    async def test_clear_mineru_token(self, settings_client):
        """发送 __CLEAR__ 应将 mineru_token 清空。"""
        await settings_client.post("/api/settings", json={
            "llm_provider": "deepseek",
            "mineru_token": "some_mineru_token",
        })
        r1 = await settings_client.get("/api/settings")
        assert r1.json()["ocr"]["mineru"]["configured"] is True

        r2 = await settings_client.post("/api/settings", json={
            "llm_provider": "deepseek",
            "mineru_token": "__CLEAR__",
        })
        assert r2.status_code == 200

        r3 = await settings_client.get("/api/settings")
        assert r3.json()["ocr"]["mineru"]["configured"] is False


class TestUserRules:
    """S0: 用户自定义合规规则 API（GET/PUT /api/settings/rules）。"""

    @pytest.mark.asyncio
    async def test_get_rules_empty_by_default(self, settings_client):
        """未配置规则时返回空列表。"""
        r = await settings_client.get("/api/settings/rules")
        assert r.status_code == 200
        assert r.json()["rules"] == []

    @pytest.mark.asyncio
    async def test_put_and_get_roundtrip(self, settings_client):
        """PUT 保存规则后 GET 能完整回读（含 id/active/created_at）。"""
        payload = {"rules": [
            {"text": "产品 A 的中间体储存温度必须为 15-25°C", "active": True},
            {"text": "关键工序必须双人复核签名", "active": False},
        ]}
        r = await settings_client.put("/api/settings/rules", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert len(data["rules"]) == 2
        assert data["rules"][0]["id"]  # 自动生成 id
        assert data["rules"][0]["created_at"]

        r2 = await settings_client.get("/api/settings/rules")
        rules = r2.json()["rules"]
        assert len(rules) == 2
        assert rules[0]["text"] == "产品 A 的中间体储存温度必须为 15-25°C"
        assert rules[0]["active"] is True
        assert rules[1]["active"] is False

    @pytest.mark.asyncio
    async def test_put_rejects_empty_text(self, settings_client):
        """空规则内容应返回 400 且不写入。"""
        r = await settings_client.put("/api/settings/rules", json={
            "rules": [{"text": "  ", "active": True}],
        })
        assert r.status_code == 400
        r2 = await settings_client.get("/api/settings/rules")
        assert r2.json()["rules"] == []

    @pytest.mark.asyncio
    async def test_put_rejects_oversized_text(self, settings_client):
        """超过 1000 字符的规则应返回 400。"""
        r = await settings_client.put("/api/settings/rules", json={
            "rules": [{"text": "长" * 1001, "active": True}],
        })
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_put_rejects_over_limit_count(self, settings_client):
        """超过 100 条上限应返回 400。"""
        r = await settings_client.put("/api/settings/rules", json={
            "rules": [{"text": f"规则 {i}", "active": True} for i in range(101)],
        })
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_put_persists_to_config_json(self, settings_client, tmp_path):
        """规则应写入 config.json 文件（load_user_rules 可读取）。"""
        await settings_client.put("/api/settings/rules", json={
            "rules": [{"text": "批号必须全页一致", "active": True}],
        })
        import config
        rules = config.load_user_rules()
        assert len(rules) == 1
        assert rules[0]["text"] == "批号必须全页一致"

    @pytest.mark.asyncio
    async def test_put_writes_audit_log(self, settings_client):
        """规则变更应写入 audit_log（GMP 追溯）。"""
        from db.client import get_db
        await settings_client.put("/api/settings/rules", json={
            "rules": [{"text": "规则 A", "active": True}],
        })
        db = await get_db()
        cur = await db.execute(
            "SELECT action, detail FROM audit_log WHERE action = 'user_rules_update'"
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert "1 rules" in rows[0][1]

    @pytest.mark.asyncio
    async def test_put_rejected_writes_failure_audit(self, settings_client):
        """校验失败（空文本）也应写 audit_log — 用户保存失败无留痕会让

        「规则没起作用」无法诊断（真实故障：用户以为已保存但 config 从未
        更新，audit_log 无任何记录）。"""
        from db.client import get_db
        r = await settings_client.put("/api/settings/rules", json={
            "rules": [{"text": "   ", "active": True}],
        })
        assert r.status_code == 400
        db = await get_db()
        cur = await db.execute(
            "SELECT action, detail FROM audit_log "
            "WHERE action = 'user_rules_update_failed'"
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert "内容不能为空" in rows[0][1]
        # 失败不应污染 config 中的规则
        import config
        assert config.load_user_rules() == []

    @pytest.mark.asyncio
    async def test_get_rules_last_saved_at(self, settings_client):
        """GET /rules 返回最近保存时间；从未成功保存时为 None（前端防呆提示）。"""
        r = await settings_client.get("/api/settings/rules")
        assert r.json()["last_saved_at"] is None

        await settings_client.put("/api/settings/rules", json={
            "rules": [{"text": "批号必须一致", "active": True}],
        })
        r2 = await settings_client.get("/api/settings/rules")
        assert r2.json()["last_saved_at"] is not None
        assert r2.json()["rules"][0]["text"] == "批号必须一致"

    @pytest.mark.asyncio
    async def test_get_rules_reports_hits(self, settings_client):
        """GET /api/settings/rules 应返回各规则历史命中数（source=user_rule 按 id 分组）。"""
        from db.client import get_db
        db = await get_db()
        await db.execute(
            "INSERT INTO jobs (id, filename, status) VALUES ('job-hit-1', 'hit.pdf', 'review')"
        )
        await db.execute(
            "INSERT INTO findings (job_id, page, type, severity, description, source, user_rule_id) "
            "VALUES ('job-hit-1', 1, 'user_rule', 'warning', '温度超标', 'user_rule', 'rule-a')"
        )
        await db.execute(
            "INSERT INTO findings (job_id, page, type, severity, description, source, user_rule_id) "
            "VALUES ('job-hit-1', 2, 'user_rule', 'warning', '复核缺失', 'user_rule', 'rule-a')"
        )
        await db.execute(
            "INSERT INTO findings (job_id, page, type, severity, description, source, user_rule_id) "
            "VALUES ('job-hit-1', 3, 'user_rule', 'info', '混批', 'user_rule', 'rule-b')"
        )
        # 未回填 id 的历史命中不计入（NULL 被 GROUP BY 排除）
        await db.execute(
            "INSERT INTO findings (job_id, page, type, severity, description, source, user_rule_id) "
            "VALUES ('job-hit-1', 4, 'user_rule', 'info', '无 id', 'user_rule', NULL)"
        )
        # 非 user_rule 来源不计数
        await db.execute(
            "INSERT INTO findings (job_id, page, type, severity, description, source) "
            "VALUES ('job-hit-1', 5, 'signature_mismatch', 'critical', '签名', 'llm_cross')"
        )
        await db.commit()

        r = await settings_client.get("/api/settings/rules")
        assert r.status_code == 200
        hits = r.json()["hits"]
        assert hits == {"rule-a": 2, "rule-b": 1}

    @pytest.mark.asyncio
    async def test_non_local_origin_rejected(self, tmp_path):
        """非本地 Origin 的 PUT 应被拒绝（CSRF 防护）。"""
        from main import app
        from httpx import ASGITransport
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
            headers={"Origin": "http://evil.example.com"},
        ) as client:
            r = await client.put("/api/settings/rules", json={
                "rules": [{"text": "恶意规则", "active": True}],
            })
            assert r.status_code == 403


# ===========================================================================
# 飞书通知（Phase 12）
# ===========================================================================


class TestFeishuNotifySettings:
    """飞书通知配置的读写、掩码保护、事件白名单与测试消息端点。"""

    @pytest.mark.asyncio
    async def test_feishu_config_roundtrip(self, settings_client):
        """保存飞书配置 → config.json 落盘 → load_feishu_config 读真值 → GET 掩码。"""
        import config
        r = await settings_client.post("/api/settings", json={
            "feishu_enabled": True,
            "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/tok123",
            "feishu_secret": "sec456",
            "feishu_events": "review,error",
        })
        assert r.status_code == 200

        saved = config.load_feishu_config()
        assert saved["enabled"] is True
        assert saved["webhook_url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/tok123"
        assert saved["secret"] == "sec456"
        assert saved["events"] == ["review", "error"]

        r2 = await settings_client.get("/api/settings")
        feishu = r2.json()["feishu"]
        assert feishu["enabled"] is True
        assert "tok123" not in feishu["webhook_url"]      # 掩码
        assert "sec456" not in feishu["secret"]           # 掩码
        assert "…" in feishu["webhook_url"] or "****" in feishu["webhook_url"] or feishu["webhook_url"]

    @pytest.mark.asyncio
    async def test_feishu_bool_string_false_parsed(self, settings_client):
        """config.json 存的 "false" 字符串应解析为 False（bool() 陷阱防护）。"""
        import config
        await settings_client.post("/api/settings", json={
            "feishu_enabled": False,
        })
        r = await settings_client.get("/api/settings")
        assert r.json()["feishu"]["enabled"] is False
        assert config.load_feishu_config()["enabled"] is False

    @pytest.mark.asyncio
    async def test_feishu_events_validation(self, settings_client):
        """非法事件名应被拒绝（400），合法写入不受影响。"""
        r = await settings_client.post("/api/settings", json={
            "feishu_events": "review,not_a_real_status",
        })
        assert r.status_code == 400
        r2 = await settings_client.post("/api/settings", json={
            "feishu_events": "review,partial_review,error,cancelled",
        })
        assert r2.status_code == 200

    @pytest.mark.asyncio
    async def test_masked_value_not_overwritten(self, settings_client):
        """回传与掩码相同的 URL → 忽略写入（保护已保存的真值）。"""
        import config
        await settings_client.post("/api/settings", json={
            "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/realtoken",
        })
        assert config.load_feishu_config()["webhook_url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/realtoken"
        masked = (await settings_client.get("/api/settings")).json()["feishu"]["webhook_url"]
        r = await settings_client.post("/api/settings", json={
            "feishu_webhook_url": masked,
        })
        assert r.status_code == 200
        # 真值未被掩码覆盖
        assert config.load_feishu_config()["webhook_url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/realtoken"

    @pytest.mark.asyncio
    async def test_masked_value_reports_skipped_in_response(self, settings_client):
        """T2.3：掩码值回传被跳过时，响应须显式列出 skipped 字段（此前静默跳过）。"""
        import config
        await settings_client.post("/api/settings", json={
            "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/realtoken2",
        })
        masked = (await settings_client.get("/api/settings")).json()["feishu"]["webhook_url"]
        r = await settings_client.post("/api/settings", json={
            "feishu_webhook_url": masked,
        })
        data = r.json()
        assert data.get("skipped") == ["feishu_webhook_url"]
        assert "feishu_webhook_url" in data.get("message", "")
        # updated=0 且 skipped 非空的路径同样有提示（全量掩码跳过分支）
        assert data.get("updated") == 0
        assert config.load_feishu_config()["webhook_url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/realtoken2"

    @pytest.mark.asyncio
    async def test_partial_masked_skip_still_updates_others(self, settings_client):
        """T2.3：一份请求里掩码字段被跳过，其他字段仍正常写入并计入 updated。"""
        import config
        await settings_client.post("/api/settings", json={
            "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/keepme",
        })
        masked = (await settings_client.get("/api/settings")).json()["feishu"]["webhook_url"]
        r = await settings_client.post("/api/settings", json={
            "feishu_webhook_url": masked,
            "feishu_mode": "app_bot",
        })
        data = r.json()
        assert data.get("skipped") == ["feishu_webhook_url"]
        assert data.get("updated") == 1
        assert "feishu_mode" in data.get("fields", [])
        assert config.load_feishu_config()["mode"] == "app_bot"
        assert config.load_feishu_config()["webhook_url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/keepme"

    @pytest.mark.asyncio
    async def test_test_feishu_success(self, settings_client):
        """测试消息端点：mock webhook 返回 code=0 → ok=True + audit_log 留痕。"""
        with unittest.mock.patch("core.notify.requests.post") as m:
            resp = unittest.mock.MagicMock(status_code=200)
            resp.json.return_value = {"code": 0, "msg": "success"}
            m.return_value = resp
            r = await settings_client.post("/api/settings/test_feishu", json={
                "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/tok123",
            })
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # 真实 Webhook URL 被调用（非假端点）
        assert "https://open.feishu.cn/open-apis/bot/v2/hook/tok123" in [
            c.args[0] for c in m.call_args_list
        ]
        from db.client import get_db
        db = await get_db()
        cur = await db.execute("SELECT detail FROM audit_log WHERE action = 'feishu_test'")
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert "ok=True" in rows[0][0]

    @pytest.mark.asyncio
    async def test_test_feishu_masked_url_rejected(self, settings_client):
        """传掩码值作 URL → 拒绝（防止重复发测试消息到错误地址）。"""
        await settings_client.post("/api/settings", json={
            "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/realtoken",
        })
        masked = (await settings_client.get("/api/settings")).json()["feishu"]["webhook_url"]
        r = await settings_client.post("/api/settings/test_feishu", json={
            "webhook_url": masked,
        })
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "掩码" in r.json()["reason"]

    @pytest.mark.asyncio
    async def test_test_feishu_masked_secret_rejected(self, settings_client):
        """传掩码值作 secret → 拒绝（避免误导性签名校验失败提示）。"""
        await settings_client.post("/api/settings", json={
            "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/realtoken",
            "feishu_secret": "sec-realsecret1234567890",
        })
        masked_secret = (await settings_client.get("/api/settings")).json()["feishu"]["secret"]
        r = await settings_client.post("/api/settings/test_feishu", json={
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/realtoken",
            "secret": masked_secret,
        })
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "掩码" in r.json()["reason"]

    @pytest.mark.asyncio
    async def test_test_feishu_private_url_blocked(self, settings_client):
        """SSRF：私网 webhook URL 应被拦截。"""
        r = await settings_client.post("/api/settings/test_feishu", json={
            "webhook_url": "http://127.0.0.1:9999/hook",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is False

    @pytest.mark.asyncio
    async def test_test_feishu_business_error_hint(self, settings_client):
        """业务错误（19024 关键词缺失）→ 中文提示。"""
        with unittest.mock.patch("core.notify.requests.post") as m:
            resp = unittest.mock.MagicMock(status_code=200)
            resp.json.return_value = {"code": 19024, "msg": "keyword missing"}
            m.return_value = resp
            r = await settings_client.post("/api/settings/test_feishu", json={
                "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/tok123",
            })
        assert r.json()["ok"] is False
        assert "19024" in r.json()["reason"] or "关键词" in r.json()["reason"]

    # ---- App-bot 模式（Phase 12.1）----

    @pytest.mark.asyncio
    async def test_app_bot_config_roundtrip(self, settings_client):
        """保存 app_bot 配置 → 落盘真值 → GET 返回（secret 掩码）。"""
        import config
        r = await settings_client.post("/api/settings", json={
            "feishu_enabled": True,
            "feishu_mode": "app_bot",
            "feishu_app_id": "cli_test_app",
            "feishu_app_secret": "appsecret123",
            "feishu_open_id": "ou_test_receiver",
            "feishu_mobile": "13800000000",
        })
        assert r.status_code == 200

        saved = config.load_feishu_config()
        assert saved["mode"] == "app_bot"
        assert saved["app_id"] == "cli_test_app"
        assert saved["app_secret"] == "appsecret123"
        assert saved["open_id"] == "ou_test_receiver"
        assert saved["mobile"] == "13800000000"

        r2 = await settings_client.get("/api/settings")
        feishu = r2.json()["feishu"]
        assert feishu["mode"] == "app_bot"
        assert "appsecret123" not in feishu["app_secret"]  # 掩码

    @pytest.mark.asyncio
    async def test_app_secret_masked_not_overwritten(self, settings_client):
        """回传掩码 app_secret → 不覆盖真值。"""
        import config
        await settings_client.post("/api/settings", json={
            "feishu_app_secret": "realsecret",
        })
        assert config.load_feishu_config()["app_secret"] == "realsecret"
        masked = (await settings_client.get("/api/settings")).json()["feishu"]["app_secret"]
        r = await settings_client.post("/api/settings", json={
            "feishu_app_secret": masked,
        })
        assert r.status_code == 200
        assert config.load_feishu_config()["app_secret"] == "realsecret"

    @pytest.mark.asyncio
    async def test_feishu_mode_validation(self, settings_client):
        """非法 mode → 400。"""
        r = await settings_client.post("/api/settings", json={
            "feishu_mode": "slack",
        })
        assert r.status_code == 400
        r2 = await settings_client.post("/api/settings", json={
            "feishu_mode": "app_bot",
        })
        assert r2.status_code == 200

    @pytest.mark.asyncio
    async def test_test_feishu_app_bot_success(self, settings_client):
        """app_bot 测试消息：mock token + 发消息 → ok=True + audit_log。"""
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        tok_resp = unittest.mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t-test", "expire": 7200}
        msg_resp = unittest.mock.MagicMock(status_code=200)
        msg_resp.json.return_value = {"code": 0, "msg": "success", "data": {"message_id": "om_1"}}
        with unittest.mock.patch("core.notify.requests.post",
                                 side_effect=[tok_resp, msg_resp]) as m:
            r = await settings_client.post("/api/settings/test_feishu", json={
                "mode": "app_bot",
                "app_id": "cli_x",
                "app_secret": "sec",
                "open_id": "ou_1",
            })
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # 真的调用了 im/v1/messages
        urls = [c.args[0] for c in m.call_args_list]
        assert any("/im/v1/messages" in u for u in urls)
        from db.client import get_db
        db = await get_db()
        cur = await db.execute("SELECT detail FROM audit_log WHERE action = 'feishu_test'")
        rows = await cur.fetchall()
        assert any("mode=app_bot ok=True" in row[0] for row in rows)

    @pytest.mark.asyncio
    async def test_test_feishu_app_bot_masked_secret_rejected(self, settings_client):
        """app_secret 传掩码 → 明确提示拒绝。"""
        await settings_client.post("/api/settings", json={
            "feishu_app_secret": "realsecret123",
        })
        masked = (await settings_client.get("/api/settings")).json()["feishu"]["app_secret"]
        r = await settings_client.post("/api/settings/test_feishu", json={
            "mode": "app_bot",
            "app_id": "cli_x",
            "app_secret": masked,
            "open_id": "ou_1",
        })
        assert r.json()["ok"] is False
        assert "掩码" in r.json()["reason"]

    @pytest.mark.asyncio
    async def test_test_feishu_app_bot_fatal_code_hint(self, settings_client):
        """230013 不可用错误 → 中文提示。"""
        import core.notify as notify_mod
        notify_mod._token_cache = {"token": "", "fetched_at": 0.0, "app_id": "", "app_secret": ""}
        notify_mod._open_id_cache = {"open_id": "", "mobile": "", "resolved_at": 0.0, "app_id": ""}
        tok_resp = unittest.mock.MagicMock(status_code=200)
        tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t", "expire": 7200}
        msg_resp = unittest.mock.MagicMock(status_code=200)
        msg_resp.json.return_value = {"code": 230013, "msg": "no availability"}
        with unittest.mock.patch("core.notify.requests.post",
                                 side_effect=[tok_resp, msg_resp]):
            r = await settings_client.post("/api/settings/test_feishu", json={
                "mode": "app_bot",
                "app_id": "cli_x",
                "app_secret": "sec",
                "open_id": "ou_1",
            })
        assert r.json()["ok"] is False
        assert "230013" in r.json()["reason"] or "可用范围" in r.json()["reason"]

    @pytest.mark.asyncio
    async def test_test_feishu_app_bot_missing_receiver(self, settings_client):
        """app_bot 缺接收者 → 拒绝。"""
        r = await settings_client.post("/api/settings/test_feishu", json={
            "mode": "app_bot",
            "app_id": "cli_x",
            "app_secret": "sec",
        })
        assert r.json()["ok"] is False
        assert "接收者" in r.json()["reason"]


class TestSettingsAudit:
    """设置保存审计（Settings API 写入 audit_log，GMP 追溯）。"""

    @pytest.mark.asyncio
    async def test_post_settings_writes_audit(self, settings_client):
        """POST /api/settings 成功后必须写 settings_update 审计（不含敏感值）。"""
        from db.client import get_db
        await settings_client.post("/api/settings", json={
            "llm_provider": "siliconflow",
            "deepseek_api_key": "sk-secret-abc",
        })
        db = await get_db()
        cur = await db.execute(
            "SELECT action, detail FROM audit_log WHERE action='settings_update'"
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        action, detail = rows[0]
        assert "LLM_PROVIDER" in detail
        assert "sk-secret-abc" not in detail

    @pytest.mark.asyncio
    async def test_set_active_provider_writes_audit(self, settings_client):
        """切换 active provider 写 provider_switch 审计。"""
        from db.client import get_db
        r = await settings_client.post(
            "/api/settings/set_active_provider", json={"provider": "deepseek"}
        )
        assert r.status_code == 200
        db = await get_db()
        cur = await db.execute(
            "SELECT action, detail FROM audit_log WHERE action='provider_switch'"
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert "deepseek" in rows[0][1]

    @pytest.mark.asyncio
    async def test_clear_feishu_secret_by___CLEAR__(self, settings_client, tmp_path):
        """对抗审查：飞书 secret 也支持 __CLEAR__ 清空（注册到清除列表）。"""
        await settings_client.post("/api/settings", json={
            "feishu_secret": "feishu-secret-zzz",
        })
        await settings_client.post("/api/settings", json={
            "feishu_secret": "__CLEAR__",
        })
        import json
        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert data.get("feishu_secret", "") == ""

    @pytest.mark.asyncio
    async def test_clear_feishu_app_secret_by___CLEAR__(self, settings_client, tmp_path):
        """对抗审查：飞书 app_secret 支持 __CLEAR__ 清空。"""
        await settings_client.post("/api/settings", json={
            "feishu_app_secret": "feishu-app-secret-abc",
        })
        await settings_client.post("/api/settings", json={
            "feishu_app_secret": "__CLEAR__",
        })
        import json
        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert data.get("feishu_app_secret", "") == ""

    @pytest.mark.asyncio
    async def test_bom_config_survives_rules_update(self, settings_client, tmp_path):
        """对抗审查：PowerShell BOM 写的 config.json 也能被安全读取。
        带 BOM 文件上 PUT rules 后，其他键不得丢失（utf-8-sig 读取）。"""
        import json
        cfg_path = tmp_path / "config.json"
        cfg_path.write_bytes(
            b"\xef\xbb\xbf" + json.dumps({
                "deepseek_api_key": "sk-keep-me",
                "llm_provider": "deepseek",
                "user_rules": [],
            }, ensure_ascii=False).encode("utf-8")
        )
        r = await settings_client.put("/api/settings/rules", json={
            "rules": [{"text": "新规则", "active": True}],
        })
        assert r.status_code == 200
        raw = cfg_path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf") is False  # 重写时去 BOM
        data = json.loads(raw.decode("utf-8"))
        assert data["deepseek_api_key"] == "sk-keep-me"
        assert data["user_rules"][0]["text"] == "新规则"
