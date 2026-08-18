"""API 集成测试 — AJAX 分页 + 归档列表 + DELETE 状态守卫。

覆盖最近新增的端点：
- GET /api/jobs?page=&page_size=        (分页 + 边界校验 + archived 过滤)
- GET /api/jobs/archived/list           (归档列表)
- DELETE /api/jobs/{id} 运行中拒绝      (409 守卫，防孤儿 pipeline task)

这些是上一轮对抗性审查定位的 High 风险：新增端点完全无测试。
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


@pytest_asyncio.fixture
async def client(test_db):
    """基于 ASGITransport 的 API 客户端（依赖 test_db 隔离数据库）。"""
    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as c:
        yield c


async def _insert_job(test_db, job_id, status, filename="t.pdf",
                      total_pages=1, created_at=None):
    """插入一条 job（避免重复 SQL）。"""
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, filename, f"/tmp/{job_id}.pdf", status, total_pages,
         created_at or "2026-01-01 10:00:00"),
    )
    await test_db.commit()


class TestListJobsPagination:
    """GET /api/jobs — AJAX 分页端点。"""

    @pytest.mark.asyncio
    async def test_empty_db_returns_zero_jobs(self, client):
        """空数据库应返回空列表 + total_pages=0（无任何页可显示）。"""
        r = await client.get("/api/jobs")
        assert r.status_code == 200
        data = r.json()
        assert data["jobs"] == []
        assert data["total_jobs"] == 0
        # (0 + 20 - 1) // 20 = 0 — 前端 renderPagination 在 totalPages<=1 时隐藏分页
        assert data["total_pages"] == 0
        assert data["page"] == 1

    @pytest.mark.asyncio
    async def test_returns_jobs_ordered_by_created_desc(self, client, test_db):
        """应按 created_at DESC 排序（最新在前）。"""
        await _insert_job(test_db, "old-job", "review", created_at="2026-01-01 10:00:00")
        await _insert_job(test_db, "new-job", "review", created_at="2026-02-01 10:00:00")
        r = await client.get("/api/jobs")
        data = r.json()
        assert data["total_jobs"] == 2
        assert data["jobs"][0]["id"] == "new-job"
        assert data["jobs"][1]["id"] == "old-job"

    @pytest.mark.asyncio
    async def test_archived_jobs_excluded(self, client, test_db):
        """archived 状态的 job 不应出现在列表中。"""
        await _insert_job(test_db, "active-job", "review")
        await _insert_job(test_db, "archived-job", "archived")
        r = await client.get("/api/jobs")
        data = r.json()
        assert data["total_jobs"] == 1
        assert data["jobs"][0]["id"] == "active-job"

    @pytest.mark.asyncio
    async def test_pagination_respects_page_size(self, client, test_db):
        """page_size=2 应只返回 2 条，total_pages=ceil(3/2)=2。"""
        for i in range(3):
            await _insert_job(test_db, f"job-{i}", "review",
                              created_at=f"2026-01-0{i+1} 10:00:00")
        r = await client.get("/api/jobs?page=1&page_size=2")
        data = r.json()
        assert len(data["jobs"]) == 2
        assert data["page_size"] == 2
        assert data["total_jobs"] == 3
        assert data["total_pages"] == 2
        assert data["page"] == 1

    @pytest.mark.asyncio
    async def test_page_2_returns_remaining_jobs(self, client, test_db):
        """第 2 页应返回剩余 1 条。"""
        for i in range(3):
            await _insert_job(test_db, f"job-{i}", "review",
                              created_at=f"2026-01-0{i+1} 10:00:00")
        r = await client.get("/api/jobs?page=2&page_size=2")
        data = r.json()
        assert data["page"] == 2
        assert len(data["jobs"]) == 1

    @pytest.mark.asyncio
    async def test_page_beyond_range_returns_empty(self, client, test_db):
        """超出范围的页应返回空 jobs（不是 404）。"""
        await _insert_job(test_db, "only-job", "review")
        r = await client.get("/api/jobs?page=999")
        data = r.json()
        assert data["jobs"] == []
        assert data["total_jobs"] == 1  # 总数仍正确

    @pytest.mark.asyncio
    async def test_negative_page_clamped_to_1(self, client, test_db):
        """page < 1 应被 max(1, page) 夹紧为 1。"""
        await _insert_job(test_db, "job-1", "review")
        r = await client.get("/api/jobs?page=-5")
        assert r.status_code == 200
        assert r.json()["page"] == 1

    @pytest.mark.asyncio
    async def test_page_size_capped_at_100(self, client, test_db):
        """page_size > 100 应被夹紧为 100（防滥用大查询）。"""
        await _insert_job(test_db, "job-1", "review")
        r = await client.get("/api/jobs?page=1&page_size=9999")
        assert r.status_code == 200
        assert r.json()["page_size"] == 100

    @pytest.mark.asyncio
    async def test_page_size_zero_clamped_to_1(self, client, test_db):
        """page_size < 1 应被 max(1, ...) 夹紧为 1。"""
        await _insert_job(test_db, "job-1", "review")
        r = await client.get("/api/jobs?page=1&page_size=0")
        assert r.status_code == 200
        assert r.json()["page_size"] == 1

    @pytest.mark.asyncio
    async def test_pdf_path_not_exposed_in_response(self, client, test_db):
        """pdf_path 不应泄露到 JSON 响应中（路径信息敏感）。"""
        await _insert_job(test_db, "job-1", "review")
        r = await client.get("/api/jobs")
        data = r.json()
        assert "pdf_path" not in data["jobs"][0]


class TestArchivedList:
    """GET /api/jobs/archived/list — 归档列表端点。"""

    @pytest.mark.asyncio
    async def test_empty_returns_zero(self, client):
        r = await client.get("/api/jobs/archived/list")
        assert r.status_code == 200
        data = r.json()
        assert data["archived"] == []
        assert data["count"] == 0

    @pytest.mark.asyncio
    async def test_returns_only_archived_jobs(self, client, test_db):
        """只应返回 status=archived 的 job。"""
        await _insert_job(test_db, "active-job", "review")
        await _insert_job(test_db, "archived-1", "archived",
                          created_at="2026-01-01 10:00:00")
        await _insert_job(test_db, "archived-2", "archived",
                          created_at="2026-02-01 10:00:00")
        r = await client.get("/api/jobs/archived/list")
        data = r.json()
        assert data["count"] == 2
        ids = [j["id"] for j in data["archived"]]
        assert "archived-1" in ids
        assert "archived-2" in ids
        assert "active-job" not in ids

    @pytest.mark.asyncio
    async def test_archived_ordered_desc(self, client, test_db):
        """归档列表也应按 created_at DESC。"""
        await _insert_job(test_db, "old-arch", "archived",
                          created_at="2026-01-01 10:00:00")
        await _insert_job(test_db, "new-arch", "archived",
                          created_at="2026-02-01 10:00:00")
        r = await client.get("/api/jobs/archived/list")
        data = r.json()
        assert data["archived"][0]["id"] == "new-arch"

    @pytest.mark.asyncio
    async def test_archived_does_not_expose_pdf_path(self, client, test_db):
        """归档列表也不应泄露 pdf_path（虽然当前 SQL 没查，但显式断言）。"""
        await _insert_job(test_db, "arch-1", "archived")
        r = await client.get("/api/jobs/archived/list")
        data = r.json()
        assert "pdf_path" not in data["archived"][0]


class TestDeleteActiveJobGuard:
    """DELETE /api/jobs/{id} — 运行中 job 删除守卫（防孤儿 pipeline task）。"""

    @pytest.mark.asyncio
    async def test_delete_pending_job_returns_409(self, client, test_db):
        """pending 状态的 job 不允许删除（pipeline 仍在运行）。"""
        await _insert_job(test_db, "pending-job", "pending")
        r = await client.delete("/api/jobs/pending-job?keep_pdf=false")
        assert r.status_code == 409
        assert "处理中" in r.text or "active" in r.text.lower()

    @pytest.mark.asyncio
    async def test_delete_ocr_running_job_returns_409(self, client, test_db):
        await _insert_job(test_db, "ocr-job", "ocr_running")
        r = await client.delete("/api/jobs/ocr-job?keep_pdf=false")
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_delete_analyzing_job_returns_409(self, client, test_db):
        await _insert_job(test_db, "analyzing-job", "analyzing")
        r = await client.delete("/api/jobs/analyzing-job?keep_pdf=false")
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_delete_cancelling_job_returns_409(self, client, test_db):
        """cancelling 中间态也不允许删除（需等待真正终态）。"""
        await _insert_job(test_db, "cancelling-job", "cancelling")
        r = await client.delete("/api/jobs/cancelling-job?keep_pdf=false")
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_delete_review_job_succeeds(self, client, test_db):
        """review 终态允许删除。"""
        from pathlib import Path
        from config import config as _cfg
        output_dir = Path(_cfg["app"].output_dir)
        job_dir = output_dir / "review-del"
        job_dir.mkdir(parents=True, exist_ok=True)
        pdf = job_dir / "t.pdf"
        pdf.write_bytes(b"%PDF-1.4 test")
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) VALUES (?, ?, ?, ?)",
            ("review-del", "t.pdf", str(pdf), "review"),
        )
        await test_db.commit()
        r = await client.delete("/api/jobs/review-del?keep_pdf=false")
        assert r.status_code == 200
        assert r.json()["deleted"] is True

    @pytest.mark.asyncio
    async def test_delete_archived_job_succeeds(self, client, test_db):
        """archived 终态允许删除。"""
        from pathlib import Path
        from config import config as _cfg
        output_dir = Path(_cfg["app"].output_dir)
        job_dir = output_dir / "arch-del"
        job_dir.mkdir(parents=True, exist_ok=True)
        pdf = job_dir / "t.pdf"
        pdf.write_bytes(b"%PDF-1.4 test")
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) VALUES (?, ?, ?, ?)",
            ("arch-del", "t.pdf", str(pdf), "archived"),
        )
        await test_db.commit()
        r = await client.delete("/api/jobs/arch-del?keep_pdf=false")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_delete_error_job_succeeds(self, client, test_db):
        """error 终态允许删除（用户清理失败任务）。"""
        from pathlib import Path
        from config import config as _cfg
        output_dir = Path(_cfg["app"].output_dir)
        job_dir = output_dir / "err-del"
        job_dir.mkdir(parents=True, exist_ok=True)
        pdf = job_dir / "t.pdf"
        pdf.write_bytes(b"%PDF-1.4 test")
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) VALUES (?, ?, ?, ?)",
            ("err-del", "t.pdf", str(pdf), "error"),
        )
        await test_db.commit()
        r = await client.delete("/api/jobs/err-del?keep_pdf=false")
        assert r.status_code == 200


class TestRouteOrdering:
    """FastAPI 路由顺序 — /{job_id} 不应吞掉 /archived/list 和 /stats/overview。"""

    @pytest.mark.asyncio
    async def test_archived_list_not_captured_by_job_id(self, client):
        """GET /api/jobs/archived/list 应命中 list_archived，而非 get_job_status。

        若路由顺序错误，会被 /{job_id} 捕获 job_id='archived'，返回 404
        'Job not found'。list_archived 返回的 JSON 含 'archived' 数组字段。
        """
        r = await client.get("/api/jobs/archived/list")
        assert r.status_code == 200
        data = r.json()
        assert "archived" in data  # list_archived 的特征字段
        assert "count" in data

    @pytest.mark.asyncio
    async def test_stats_overview_not_captured_by_job_id(self, client):
        """GET /api/jobs/stats/overview 应命中 stats_overview。"""
        r = await client.get("/api/jobs/stats/overview")
        assert r.status_code == 200
        data = r.json()
        assert "database" in data
        assert "jobs" in data
