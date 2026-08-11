"""测试 create_job 的 LLM 配置拦截 — 未配置 provider 时拒绝上传。

这覆盖了新增的 needs_setup 拦截逻辑（api/jobs.py create_job 内）。

注意：production 代码读取 config["providers"]（不是 "llm_providers"）。
早期测试 fixture 误用 "llm_providers" 键，与 production 路径脱节，
掩盖了真实的拦截 bug（见对抗性审查 B-C1）。
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


@pytest_asyncio.fixture
async def no_llm_client(test_db):
    """无 LLM provider 配置的客户端（清空所有 provider 的 api_key）。

    production 代码遍历 config["providers"].values() 检查是否有非空
    api_key，所以这里清空每个 provider 的 api_key（而不是覆盖整个 dict
    或写到不存在的 "llm_providers" 键）。
    """
    from config import config as _cfg
    saved = {name: p.api_key for name, p in _cfg["providers"].items()}
    for p in _cfg["providers"].values():
        p.api_key = ""
    try:
        async with AsyncClient(
            transport=ASGITransport(app=__import__("main").app),
            base_url="http://test",
        ) as c:
            yield c
    finally:
        for name, key in saved.items():
            if name in _cfg["providers"]:
                _cfg["providers"][name].api_key = key


class TestUploadLLMGuard:
    """未配置 LLM 服务商时上传应被拦截。"""

    @pytest.mark.asyncio
    async def test_upload_rejected_without_llm_config(self, no_llm_client):
        """未配置任何 LLM provider 时，上传应返回 400 + 引导文案。"""
        files = {"file": ("test.pdf", b"%PDF-1.4 test", "application/pdf")}
        r = await no_llm_client.post("/api/jobs", files=files)
        assert r.status_code == 400
        detail = r.json().get("detail", "")
        assert "LLM" in detail or "配置" in detail

    @pytest.mark.asyncio
    async def test_upload_rejected_when_provider_has_no_key(self, test_db):
        """所有 provider 的 api_key 均为空时，仍应被拦截。"""
        from config import config as _cfg
        # 清空所有 provider 的 api_key（test_db fixture 注入了 deepseek 的
        # test key，这里全部清空以测试"provider 存在但无 key"的场景）。
        # 注意：不能只清 deepseek —— 如果 siliconflow 等其他 provider 仍有
        # 非空 key（从 .env 加载），has_any_key 会为 True，上传会通过。
        saved = {name: p.api_key for name, p in _cfg["providers"].items()}
        for p in _cfg["providers"].values():
            p.api_key = ""
        try:
            async with AsyncClient(
                transport=ASGITransport(app=__import__("main").app),
                base_url="http://test",
            ) as c:
                files = {"file": ("test.pdf", b"%PDF-1.4 test", "application/pdf")}
                r = await c.post("/api/jobs", files=files)
                assert r.status_code == 400
        finally:
            for name, key in saved.items():
                if name in _cfg["providers"]:
                    _cfg["providers"][name].api_key = key

    @pytest.mark.asyncio
    async def test_upload_allowed_when_llm_configured(self, test_client):
        """已配置 LLM provider（test_db fixture 注入）时，上传应正常进行。"""
        import fitz
        doc = fitz.open()
        doc.new_page().insert_text((50, 50), "Test")
        import io
        buf = io.BytesIO()
        doc.save(buf)
        doc.close()
        buf.seek(0)
        files = {"file": ("valid.pdf", buf, "application/pdf")}
        r = await test_client.post("/api/jobs", files=files)
        # 应返回 200 + job_id（不是 400 拦截）
        assert r.status_code == 200
        assert "job_id" in r.json()
