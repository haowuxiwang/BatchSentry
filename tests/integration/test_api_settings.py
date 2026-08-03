"""Settings API 集成测试。

覆盖：
- GET /api/settings 返回脱敏配置
- POST /api/settings 更新配置 + 内存同步
- 供应商切换 bug 修复验证
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient


@pytest_asyncio.fixture
async def settings_client(test_db):
    from main import app
    from httpx import ASGITransport
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:8000") as client:
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
        assert "env_file" in data

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
            "glm_api_key": "sk-glm-test",
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
        """非白名单字段应被静默忽略（不写入 .env）。"""
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
