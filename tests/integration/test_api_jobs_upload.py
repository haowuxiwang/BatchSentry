"""API 集成测试 — jobs 上传与生命周期端点（test_api_jobs.py 未覆盖的补集）。

覆盖：
- POST /api/jobs          （上传 PDF + 启动 pipeline）
- GET  /api/jobs/stats/overview
- GET  /api/jobs/archived/list
- POST /api/jobs/{id}/cancel
- POST /api/jobs/{id}/retry

关键约束：
- Mock `api.jobs.launch_pipeline` 避免 real OCR/LLM 执行
- 上传测试使用 PyMuPDF (fitz) 生成真实临时 PDF
- 复用 conftest.test_db fixture（隔离的 SQLite + 临时 output_dir）
"""
import pytest
import pytest_asyncio
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport


@pytest_asyncio.fixture
async def client(test_db):
    """提供基于 ASGITransport 的 API 客户端（依赖 test_db 隔离数据库）。"""
    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


class TestUploadPdf:
    """POST /api/jobs — 上传 PDF 并创建 job。"""

    @pytest.mark.asyncio
    async def test_upload_pdf_creates_job(self, client, tmp_path, test_db):
        """上传真实 PDF 应返回 job_id，并将 job 写入 DB（status=pending）。"""
        import fitz

        # 生成真实 PDF
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Test batch record")
        pdf_path = tmp_path / "batch.pdf"
        doc.save(str(pdf_path))
        doc.close()

        # Mock pipeline 避免真实 OCR/LLM
        with patch("api.jobs.launch_pipeline") as mock_pipe:
            with open(pdf_path, "rb") as f:
                r = await client.post(
                    "/api/jobs",
                    files={"file": ("batch.pdf", f, "application/pdf")},
                )

        assert r.status_code == 200
        data = r.json()
        assert "job_id" in data
        assert data["filename"] == "batch.pdf"
        assert data["status"] == "pending"

        # pipeline 应被调度一次
        assert mock_pipe.call_count == 1
        job_id = mock_pipe.call_args.args[0]
        assert job_id == data["job_id"]

        # DB 应有对应 job 记录，status=pending
        cursor = await test_db.execute(
            "SELECT id, filename, status, pdf_path FROM jobs WHERE id = ?",
            (data["job_id"],),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["filename"] == "batch.pdf"
        assert row["status"] == "pending"
        assert row["pdf_path"] is not None

        # audit_log 应记录 pipeline_start
        cursor = await test_db.execute(
            "SELECT action FROM audit_log WHERE job_id = ?",
            (data["job_id"],),
        )
        logs = [r["action"] for r in await cursor.fetchall()]
        assert "pipeline_start" in logs

    @pytest.mark.asyncio
    async def test_upload_rejects_non_pdf_filename(self, client, tmp_path):
        """非 .pdf 后缀应返回 400。"""
        fake = tmp_path / "notes.txt"
        fake.write_bytes(b"not a pdf")

        with patch("api.jobs.launch_pipeline"):
            with open(fake, "rb") as f:
                r = await client.post(
                    "/api/jobs",
                    files={"file": ("notes.txt", f, "text/plain")},
                )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_upload_rejects_empty_file(self, client):
        """空文件应返回 400。"""
        with patch("api.jobs.launch_pipeline"):
            r = await client.post(
                "/api/jobs",
                files={"file": ("empty.pdf", b"", "application/pdf")},
            )
        assert r.status_code == 400


class TestStatsOverview:
    """GET /api/jobs/stats/overview。"""

    @pytest.mark.asyncio
    async def test_stats_returns_all_fields(self, client):
        r = await client.get("/api/jobs/stats/overview")
        assert r.status_code == 200
        data = r.json()
        # 必须包含全部 6 个顶层字段
        for key in ("database", "jobs", "page_cache", "findings", "audit_log", "pdf_storage"):
            assert key in data, f"missing field: {key}"

    @pytest.mark.asyncio
    async def test_stats_database_field_structure(self, client):
        data = (await client.get("/api/jobs/stats/overview")).json()
        assert "path" in data["database"]
        assert "size_mb" in data["database"]

    @pytest.mark.asyncio
    async def test_stats_jobs_field_structure(self, client):
        data = (await client.get("/api/jobs/stats/overview")).json()
        assert {"total", "active", "archived"} <= set(data["jobs"])

    @pytest.mark.asyncio
    async def test_stats_pdf_storage_field_structure(self, client):
        data = (await client.get("/api/jobs/stats/overview")).json()
        assert {"dir", "count", "size_mb"} <= set(data["pdf_storage"])

    @pytest.mark.asyncio
    async def test_stats_reflects_inserted_job(self, client, test_db):
        """插入 job + finding 后，stats 计数应反映。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("stats-job", "s.pdf", "/tmp/s.pdf", "review", 3),
        )
        await test_db.execute(
            "INSERT INTO findings (job_id, page, type, severity, description, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("stats-job", 1, "参数越界", "critical", "test", "pending"),
        )
        await test_db.commit()

        data = (await client.get("/api/jobs/stats/overview")).json()
        assert data["jobs"]["total"] >= 1
        assert data["findings"] >= 1


class TestArchivedList:
    """GET /api/jobs/archived/list。"""

    @pytest.mark.asyncio
    async def test_archived_list_empty_by_default(self, client):
        r = await client.get("/api/jobs/archived/list")
        assert r.status_code == 200
        data = r.json()
        assert "archived" in data
        assert "count" in data
        assert data["count"] == 0
        assert data["archived"] == []

    @pytest.mark.asyncio
    async def test_archived_list_returns_archived_jobs(self, client, test_db):
        """已归档 job 应出现在列表中。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("archived-1", "a.pdf", "/tmp/a.pdf", "archived", 2),
        )
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("active-1", "b.pdf", "/tmp/b.pdf", "review", 2),
        )
        await test_db.commit()

        r = await client.get("/api/jobs/archived/list")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 1
        ids = [j["id"] for j in data["archived"]]
        assert "archived-1" in ids
        assert "active-1" not in ids


class TestCancelJob:
    """POST /api/jobs/{id}/cancel。"""

    @pytest.mark.asyncio
    async def test_cancel_pending_job_returns_200(self, client, test_db):
        """pending → cancelling 合法，应返回 200。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) "
            "VALUES (?, ?, ?, ?)",
            ("cancel-pending", "p.pdf", "/tmp/p.pdf", "pending"),
        )
        await test_db.commit()

        r = await client.post("/api/jobs/cancel-pending/cancel")
        assert r.status_code == 200
        assert r.json()["status"] == "cancelling"

    @pytest.mark.asyncio
    async def test_cancel_review_job_returns_400(self, client, test_db):
        """review → cancelling 非法（review 只能 → archived），应返回 400。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) "
            "VALUES (?, ?, ?, ?)",
            ("cancel-review", "r.pdf", "/tmp/r.pdf", "review"),
        )
        await test_db.commit()

        r = await client.post("/api/jobs/cancel-review/cancel")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_returns_400(self, client):
        """不存在的 job → transition_status 抛 InvalidTransitionError → 400。"""
        r = await client.post("/api/jobs/does-not-exist/cancel")
        assert r.status_code == 400


class TestRetryJob:
    """POST /api/jobs/{id}/retry。"""

    @pytest.mark.asyncio
    async def test_retry_error_job_with_pdf_returns_200(self, client, test_db, tmp_path):
        """error → pending 合法且 PDF 存在，应返回 200 并重新调度 pipeline。"""
        # 创建真实 PDF 文件
        import fitz
        doc = fitz.open()
        doc.new_page().insert_text((50, 50), "retry me")
        pdf_path = tmp_path / "retry.pdf"
        doc.save(str(pdf_path))
        doc.close()

        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) "
            "VALUES (?, ?, ?, ?)",
            ("retry-error", "retry.pdf", str(pdf_path), "error"),
        )
        await test_db.commit()

        with patch("api.jobs.launch_pipeline") as mock_pipe:
            r = await client.post("/api/jobs/retry-error/retry")

        assert r.status_code == 200
        assert r.json()["status"] == "pending"
        assert mock_pipe.call_count == 1
        assert mock_pipe.call_args.args[0] == "retry-error"

    @pytest.mark.asyncio
    async def test_retry_error_job_missing_pdf_returns_400(self, client, test_db):
        """error 状态合法但 PDF 文件不在磁盘 → 400。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) "
            "VALUES (?, ?, ?, ?)",
            ("retry-missing", "m.pdf", "/nonexistent/path/m.pdf", "error"),
        )
        await test_db.commit()

        with patch("api.jobs.launch_pipeline"):
            r = await client.post("/api/jobs/retry-missing/retry")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_retry_review_job_returns_400(self, client, test_db, tmp_path):
        """review → pending 非法（review 只能 → archived），应返回 400。"""
        import fitz
        doc = fitz.open()
        doc.new_page()
        pdf_path = tmp_path / "review.pdf"
        doc.save(str(pdf_path))
        doc.close()

        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) "
            "VALUES (?, ?, ?, ?)",
            ("retry-review", "review.pdf", str(pdf_path), "review"),
        )
        await test_db.commit()

        with patch("api.jobs.launch_pipeline"):
            r = await client.post("/api/jobs/retry-review/retry")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_retry_nonexistent_returns_404(self, client):
        """job 不存在 → 404（在 transition 之前先查 job）。"""
        with patch("api.jobs.launch_pipeline"):
            r = await client.post("/api/jobs/does-not-exist/retry")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_retry_cancelled_job_returns_200(self, client, test_db, tmp_path):
        """cancelled → pending 合法，应返回 200。"""
        import fitz
        doc = fitz.open()
        doc.new_page()
        pdf_path = tmp_path / "cancelled.pdf"
        doc.save(str(pdf_path))
        doc.close()

        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) "
            "VALUES (?, ?, ?, ?)",
            ("retry-cancelled", "cancelled.pdf", str(pdf_path), "cancelled"),
        )
        await test_db.commit()

        with patch("api.jobs.launch_pipeline") as mock_pipe:
            r = await client.post("/api/jobs/retry-cancelled/retry")
        assert r.status_code == 200
        assert r.json()["status"] == "pending"
        assert mock_pipe.call_count == 1
