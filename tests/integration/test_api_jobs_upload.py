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


class TestUploadDedup:
    """上传去重 — 相同内容（md5）二次上传应 409，force=1 可绕过。"""

    @staticmethod
    def _make_pdf(tmp_path, name: str, text: str):
        import fitz
        doc = fitz.open()
        doc.new_page().insert_text((50, 50), text)
        pdf_path = tmp_path / name
        doc.save(str(pdf_path))
        doc.close()
        return pdf_path

    @pytest.mark.asyncio
    async def test_duplicate_upload_returns_409(self, client, tmp_path, test_db):
        """相同内容二次上传：409 + 提示已有任务，不创建第二个 job、不启动 pipeline、
        新上传的临时目录被清理。"""
        pdf_path = self._make_pdf(tmp_path, "dup.pdf", "duplicate me")

        with patch("api.jobs.launch_pipeline") as mock_pipe:
            with open(pdf_path, "rb") as f:
                r1 = await client.post(
                    "/api/jobs", files={"file": ("dup.pdf", f, "application/pdf")}
                )
            with open(pdf_path, "rb") as f:
                r2 = await client.post(
                    "/api/jobs", files={"file": ("dup.pdf", f, "application/pdf")}
                )

        assert r1.status_code == 200
        assert r2.status_code == 409
        detail = r2.json()["detail"]
        assert r1.json()["job_id"] in detail
        assert mock_pipe.call_count == 1  # 去重后未再次调度 pipeline

        # DB 中只有 1 个 job，且 md5 已写入
        cursor = await test_db.execute("SELECT COUNT(*) FROM jobs WHERE md5 IS NOT NULL")
        assert (await cursor.fetchone())[0] == 1

        # 上传目录只有 1 个 job 目录（二次上传的孤儿目录已清理）
        from config import config as _cfg
        from pathlib import Path
        out = Path(_cfg["app"].output_dir)
        assert [d.name for d in out.iterdir() if d.is_dir()] == [r1.json()["job_id"]]

    @pytest.mark.asyncio
    async def test_duplicate_upload_force_creates_second_job(self, client, tmp_path, test_db):
        """force=1 应绕过去重，创建第二个 job。"""
        pdf_path = self._make_pdf(tmp_path, "force.pdf", "force me")

        with patch("api.jobs.launch_pipeline") as mock_pipe:
            with open(pdf_path, "rb") as f:
                r1 = await client.post(
                    "/api/jobs", files={"file": ("force.pdf", f, "application/pdf")}
                )
            with open(pdf_path, "rb") as f:
                r2 = await client.post(
                    "/api/jobs?force=1",
                    files={"file": ("force.pdf", f, "application/pdf")},
                )

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["job_id"] != r2.json()["job_id"]
        assert mock_pipe.call_count == 2

    @pytest.mark.asyncio
    async def test_different_content_both_accepted(self, client, tmp_path, test_db):
        """内容不同的文件都应正常接受。"""
        pdf_a = self._make_pdf(tmp_path, "a.pdf", "content A")
        pdf_b = self._make_pdf(tmp_path, "b.pdf", "content B")

        with patch("api.jobs.launch_pipeline"):
            with open(pdf_a, "rb") as f:
                r1 = await client.post(
                    "/api/jobs", files={"file": ("same.pdf", f, "application/pdf")}
                )
            with open(pdf_b, "rb") as f:
                r2 = await client.post(
                    "/api/jobs", files={"file": ("same.pdf", f, "application/pdf")}
                )

        assert r1.status_code == 200
        assert r2.status_code == 200

        cursor = await test_db.execute(
            "SELECT COUNT(DISTINCT md5) FROM jobs WHERE md5 IS NOT NULL"
        )
        assert (await cursor.fetchone())[0] == 2

    @pytest.mark.asyncio
    async def test_archived_job_does_not_block_reupload(self, client, tmp_path, test_db):
        """归档任务（软删除）不应拦截相同 md5 的重传 — 归档不占用内容指纹，
        用户归档旧任务后可直接重新上传分析（回归：发布前审查发现归档任务
        仍触发 409「已上传过」，用户删除旧任务后才允许重传，体验与审计冲突）。
        """
        pdf_path = self._make_pdf(tmp_path, "arch.pdf", "archived content")

        with patch("api.jobs.launch_pipeline"):
            with open(pdf_path, "rb") as f:
                r1 = await client.post(
                    "/api/jobs", files={"file": ("arch.pdf", f, "application/pdf")}
                )
            job_id = r1.json()["job_id"]

            # 归档第一个任务
            ar = await client.post(f"/api/jobs/{job_id}/archive")
            assert ar.status_code == 200

            # 同内容再次上传：不再 409，直接创建新任务
            with open(pdf_path, "rb") as f:
                r2 = await client.post(
                    "/api/jobs", files={"file": ("arch.pdf", f, "application/pdf")}
                )
            with open(pdf_path, "rb") as f:
                r3 = await client.post(
                    "/api/jobs", files={"file": ("arch.pdf", f, "application/pdf")}
                )

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r2.json()["job_id"] != job_id
        # 新任务创建后再传同文件，仍被去重拦截（非归档任务）
        assert r3.status_code == 409

        # 归档任务保留在表中，新任务已创建
        cursor = await test_db.execute(
            "SELECT COUNT(*) FROM jobs WHERE md5 IS NOT NULL"
        )
        assert (await cursor.fetchone())[0] == 2


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
