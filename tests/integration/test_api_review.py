"""Review API 集成测试 — findings CRUD。

覆盖：
- GET /api/jobs/{id}/findings（含 page 和 status 过滤）
- GET /api/jobs/{id}/findings/{fid}
- POST /api/jobs/{id}/findings/{fid}（confirm/reject/correct）
- GET /api/jobs/{id}/audit
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient


@pytest_asyncio.fixture
async def review_client(test_db):
    """提供带 findings 数据的客户端。"""
    # 插入 job
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        ("review-job", "test.pdf", "/tmp/test.pdf", "review", 3),
    )
    # 插入 findings
    await test_db.executemany(
        "INSERT INTO findings (job_id, page, type, severity, source, description, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("review-job", 1, "参数越界", "critical", "rule", "温度超出", "pending"),
            ("review-job", 1, "时间逻辑", "warning", "llm_page", "工序倒序", "pending"),
            ("review-job", 2, "完整性", "info", "rule", "缺少签名", "pending"),
            ("review-job", 2, "参数越界", "critical", "rule", "pH偏低", "confirmed"),
        ],
    )
    await test_db.commit()

    from main import app
    from httpx import ASGITransport
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:8000") as client:
        yield client


class TestListFindings:
    """GET /api/jobs/{id}/findings。"""

    @pytest.mark.asyncio
    async def test_list_all_findings(self, review_client):
        r = await review_client.get("/api/jobs/review-job/findings")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 4

    @pytest.mark.asyncio
    async def test_list_findings_by_page(self, review_client):
        r = await review_client.get("/api/jobs/review-job/findings?page=1")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 2
        assert all(f["page"] == 1 for f in data["findings"])

    @pytest.mark.asyncio
    async def test_list_findings_by_status(self, review_client):
        r = await review_client.get("/api/jobs/review-job/findings?status=confirmed")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 1
        assert data["findings"][0]["status"] == "confirmed"

    @pytest.mark.asyncio
    async def test_findings_sorted_by_severity(self, review_client):
        """findings 应按严重度排序（critical 优先）。"""
        r = await review_client.get("/api/jobs/review-job/findings?page=1")
        data = r.json()
        if len(data["findings"]) >= 2:
            # 第一个应是 critical
            assert data["findings"][0]["severity"] == "critical"


class TestGetFinding:
    """GET /api/jobs/{id}/findings/{fid}。"""

    @pytest.mark.asyncio
    async def test_get_finding_by_id(self, review_client):
        r = await review_client.get("/api/jobs/review-job/findings/1")
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "参数越界"

    @pytest.mark.asyncio
    async def test_get_nonexistent_finding_404(self, review_client):
        r = await review_client.get("/api/jobs/review-job/findings/999")
        assert r.status_code == 404


class TestUpdateFinding:
    """POST /api/jobs/{id}/findings/{fid}。"""

    @pytest.mark.asyncio
    async def test_confirm_finding(self, review_client):
        r = await review_client.post(
            "/api/jobs/review-job/findings/1",
            data={"status": "confirmed"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_reject_finding(self, review_client):
        r = await review_client.post(
            "/api/jobs/review-job/findings/2",
            data={"status": "rejected"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_correct_finding(self, review_client):
        r = await review_client.post(
            "/api/jobs/review-job/findings/3",
            data={"status": "corrected", "corrected_text": "新值"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_invalid_action_returns_400(self, review_client):
        r = await review_client.post(
            "/api/jobs/review-job/findings/1",
            data={"status": "invalid"},
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_update_rejected_from_non_local_origin(self, test_db):
        """对抗审查（cr-18）：恶意 Origin 的 finding 更新必须 403 —— Form 编码是
        CORS 简单请求，此前无守卫，跨站可篡改 GMP 审计数据。"""
        from main import app
        from httpx import AsyncClient, ASGITransport
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("evil-job", "test.pdf", "/tmp/test.pdf", "review", 1),
        )
        await test_db.execute(
            "INSERT INTO findings (job_id, page, type, severity, source, description, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("evil-job", 1, "参数越界", "critical", "rule", "温度超出", "pending"),
        )
        await test_db.commit()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.com:8000"
        ) as c:
            r = await c.post("/api/jobs/evil-job/findings/1", data={"status": "confirmed"})
        assert r.status_code == 403
        assert "non-local" in r.text


class TestLlmAuditLog:
    """GET /api/jobs/{id}/llm_audit — Phase 7 GMP 追溯 + P2-1 守卫。"""

    @pytest.mark.asyncio
    async def test_llm_audit_log_returns_entries(self, review_client, test_db):
        """插入 audit 行后应返回全部记录（含 provider/model/tokens）。"""
        await test_db.execute(
            "INSERT INTO llm_call_audit (job_id, page, stage, provider, protocol, model, "
            "prompt_version, prompt_tokens, completion_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("review-job", 1, "page_analysis", "deepseek", "openai", "deepseek-chat",
             "v3", 120, 80),
        )
        await test_db.commit()
        r = await review_client.get("/api/jobs/review-job/llm_audit")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] >= 1
        assert data["entries"][0]["model"] == "deepseek-chat"

    @pytest.mark.asyncio
    async def test_llm_audit_log_empty(self, review_client):
        """无 audit 行 → 200 + 空列表（不 500）。"""
        r = await review_client.get("/api/jobs/review-job/llm_audit")
        assert r.status_code == 200
        assert r.json()["count"] == 0

    @pytest.mark.asyncio
    async def test_llm_audit_log_guard(self, test_db):
        """P2-1：非本地 Host 读端点 → 403。"""
        from main import app
        from httpx import AsyncClient, ASGITransport
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("audit-job", "test.pdf", "/tmp/test.pdf", "review", 1),
        )
        await test_db.commit()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.com:8000"
        ) as c:
            r = await c.get("/api/jobs/audit-job/llm_audit")
        assert r.status_code == 403
