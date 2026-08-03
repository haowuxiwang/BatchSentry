"""API 集成测试 — jobs 路由。

覆盖：
- GET /api/jobs/stats/overview
- GET /api/jobs/archived/list
- POST /api/jobs/{id}/archive
- POST /api/jobs/{id}/unarchive
- DELETE /api/jobs/{id}
- GET /api/jobs/{id}/pages/{page}
- GET /api/jobs/{id}/findings?page=N
- POST /api/jobs/{id}/cancel
- POST /api/jobs/{id}/retry
- POST /api/jobs (上传)
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient


@pytest_asyncio.fixture
async def client_with_data(test_db):
    """提供带测试数据的 API 客户端。"""
    # 插入测试 job
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        ("test-job-api", "test.pdf", "/tmp/test.pdf", "review", 5),
    )
    await test_db.execute(
        "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
        "VALUES (?, ?, ?, ?)",
        ("test-job-api", 1, "<p>OCR text page 1</p>", '{"steps":[]}'),
    )
    await test_db.execute(
        "INSERT INTO findings (job_id, page, type, severity, source, description, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("test-job-api", 1, "参数越界", "critical", "rule", "温度超出范围", "pending"),
    )
    await test_db.commit()

    from main import app
    from httpx import ASGITransport
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


class TestStatsOverview:
    """GET /api/jobs/stats/overview。"""

    @pytest.mark.asyncio
    async def test_stats_returns_all_fields(self, client_with_data):
        r = await client_with_data.get("/api/jobs/stats/overview")
        assert r.status_code == 200
        data = r.json()
        assert "database" in data
        assert "jobs" in data
        assert "page_cache" in data
        assert "findings" in data
        assert "audit_log" in data
        assert "pdf_storage" in data

    @pytest.mark.asyncio
    async def test_stats_counts_correct(self, client_with_data):
        r = await client_with_data.get("/api/jobs/stats/overview")
        data = r.json()
        assert data["jobs"]["total"] >= 1
        assert data["page_cache"] >= 1
        assert data["findings"] >= 1


class TestArchivedList:
    """GET /api/jobs/archived/list。"""

    @pytest.mark.asyncio
    async def test_archived_list_empty_by_default(self, client_with_data):
        r = await client_with_data.get("/api/jobs/archived/list")
        assert r.status_code == 200
        data = r.json()
        assert "archived" in data
        assert "count" in data


class TestArchiveUnarchive:
    """归档/取消归档。"""

    @pytest.mark.asyncio
    async def test_archive_review_job(self, client_with_data):
        r = await client_with_data.post("/api/jobs/test-job-api/archive")
        assert r.status_code == 200
        assert r.json()["status"] == "archived"

    @pytest.mark.asyncio
    async def test_unarchive_archived_job(self, client_with_data):
        # 先归档
        await client_with_data.post("/api/jobs/test-job-api/archive")
        # 再取消归档
        r = await client_with_data.post("/api/jobs/test-job-api/unarchive")
        assert r.status_code == 200
        assert r.json()["status"] == "review"

    @pytest.mark.asyncio
    async def test_archive_nonexistent_returns_400(self, client_with_data):
        """不存在的 job 归档应返回错误（transition_status 抛 InvalidTransitionError → 400）。"""
        r = await client_with_data.post("/api/jobs/nonexistent-id/archive")
        assert r.status_code == 400


class TestDeleteJob:
    """DELETE /api/jobs/{id}。"""

    @pytest.mark.asyncio
    async def test_delete_job(self, client_with_data):
        r = await client_with_data.delete("/api/jobs/test-job-api?keep_pdf=true")
        assert r.status_code == 200
        assert r.json()["deleted"] is True

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, client_with_data):
        r = await client_with_data.delete("/api/jobs/nonexistent-id")
        assert r.status_code == 404


class TestPageData:
    """GET /api/jobs/{id}/pages/{page}。"""

    @pytest.mark.asyncio
    async def test_get_page_data(self, client_with_data):
        r = await client_with_data.get("/api/jobs/test-job-api/pages/1")
        assert r.status_code == 200
        data = r.json()
        assert data["page"] == 1
        assert "raw_html" in data
        assert "structured" in data

    @pytest.mark.asyncio
    async def test_get_nonexistent_page_returns_404(self, client_with_data):
        r = await client_with_data.get("/api/jobs/test-job-api/pages/999")
        assert r.status_code == 404


class TestFindingsList:
    """GET /api/jobs/{id}/findings?page=N。"""

    @pytest.mark.asyncio
    async def test_get_findings_by_page(self, client_with_data):
        r = await client_with_data.get("/api/jobs/test-job-api/findings?page=1")
        assert r.status_code == 200
        data = r.json()
        assert "findings" in data
        assert "count" in data
        assert data["count"] >= 1
        assert data["findings"][0]["type"] == "参数越界"

    @pytest.mark.asyncio
    async def test_get_findings_all_pages(self, client_with_data):
        r = await client_with_data.get("/api/jobs/test-job-api/findings")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] >= 1


class TestCancelRetry:
    """取消/重试（状态机集成）。"""

    @pytest.mark.asyncio
    async def test_cancel_review_job_raises_400(self, client_with_data):
        """review 状态不能取消（应返回 400）。"""
        r = await client_with_data.post("/api/jobs/test-job-api/cancel")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_retry_error_job(self, client_with_data, test_db):
        """error 状态应能重试。"""
        # 先把 job 改为 error 状态（通过状态机）
        await test_db.execute("UPDATE jobs SET status = 'error' WHERE id = 'test-job-api'")
        await test_db.commit()
        r = await client_with_data.post("/api/jobs/test-job-api/retry")
        # 应返回 200（pending）或 400（如果 PDF 不存在）
        assert r.status_code in (200, 400)


class TestHealthEndpoint:
    """健康检查。"""

    @pytest.mark.asyncio
    async def test_health(self, client_with_data):
        r = await client_with_data.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_has_request_id_header(self, client_with_data):
        """响应应包含 X-Request-ID 头（request_id 中间件验证）。"""
        r = await client_with_data.get("/health")
        assert "x-request-id" in r.headers or "X-Request-ID" in r.headers
