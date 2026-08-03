"""集成测试 — main.py 中的 FastAPI 路由。

覆盖 main.py 直接注册在 app 上的路由（非 api/* 子路由）：
- GET /health              → 健康检查 JSON
- GET /                    → 上传页 HTML（含 job 列表）
- GET /settings            → 设置页 HTML
- GET /jobs                → 等价于 GET /（返回 upload 页）
- GET /jobs/{id}/review    → 审核 UI 页（job 不存在时 404）
- GET /api/jobs/{id}/pdf   → PDF 文件服务（找不到时 404）

使用 tests/conftest.py 中提供的 test_db / test_client fixture。
对于需要 DB 预置数据的测试，使用本文件内的 client_with_job fixture
（依赖 test_db，插入 job 后再构建 ASGITransport 客户端）。
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


@pytest_asyncio.fixture
async def client_with_job(test_db):
    """预置一条 job 后再构建 ASGITransport 客户端。

    用于需要 DB 数据的路由测试（review 页 / PDF 服务）。
    插入的 job pdf_path 指向不存在的文件，便于测试 404 路径。
    """
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        ("route-job", "test.pdf", "/nonexistent/test.pdf", "review", 3),
    )
    await test_db.commit()

    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


class TestHealth:
    """GET /health — 健康检查。"""

    @pytest.mark.asyncio
    async def test_health_returns_ok(self, test_client):
        r = await test_client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "version": "1.0.0"}


class TestIndex:
    """GET / — 上传页 HTML（含 job 列表）。"""

    @pytest.mark.asyncio
    async def test_index_returns_html(self, test_client):
        r = await test_client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        # 上传页应包含 jobs 相关标记（upload.html 模板渲染）
        assert "<html" in r.text.lower()
        assert len(r.text) > 0


class TestSettings:
    """GET /settings — 设置页 HTML。"""

    @pytest.mark.asyncio
    async def test_settings_returns_html(self, test_client):
        r = await test_client.get("/settings")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        assert "<html" in r.text.lower()


class TestJobsList:
    """GET /jobs — 等价于 GET /（同一 upload 页）。"""

    @pytest.mark.asyncio
    async def test_jobs_equals_index(self, test_client):
        r_jobs = await test_client.get("/jobs")
        r_index = await test_client.get("/")
        assert r_jobs.status_code == 200
        assert r_jobs.status_code == r_index.status_code
        # /jobs 内部调用 index()，两者响应体应一致
        assert r_jobs.text == r_index.text
        assert "text/html" in r_jobs.headers.get("content-type", "")


class TestReviewPage:
    """GET /jobs/{job_id}/review — 审核 UI 页。"""

    @pytest.mark.asyncio
    async def test_review_nonexistent_returns_404(self, test_client):
        r = await test_client.get("/jobs/does-not-exist/review")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_review_existing_returns_html(self, client_with_job):
        r = await client_with_job.get("/jobs/route-job/review")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        assert "<html" in r.text.lower()
        # review 模板应体现该 job 的上下文
        assert "route-job" in r.text


class TestServePdf:
    """GET /api/jobs/{job_id}/pdf — PDF 文件服务。"""

    @pytest.mark.asyncio
    async def test_pdf_nonexistent_job_returns_404(self, test_client):
        r = await test_client.get("/api/jobs/no-such-job/pdf")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_pdf_missing_file_returns_404(self, client_with_job):
        """job 存在但 pdf_path 指向不存在的文件 → 404 PDF file missing。"""
        r = await client_with_job.get("/api/jobs/route-job/pdf")
        assert r.status_code == 404
