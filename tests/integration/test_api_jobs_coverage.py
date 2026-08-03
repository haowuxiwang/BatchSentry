"""API 集成测试 — jobs 路由未覆盖的端点补全。

补全 test_api_jobs.py / test_api_jobs_upload.py 未覆盖的代码路径：
- GET /api/jobs/{id}                       (job 状态详情)
- GET /api/jobs/{id}/pages/{page}          (page_confidence / parse_error 分支)
- GET /api/jobs/{id}/findings              (page=None 全量分支)
- POST /api/jobs/{id}/unarchive            (取消归档)
- DELETE /api/jobs/{id}?keep_pdf=true      (保留 PDF 分支)
- DELETE /api/jobs/{id}?keep_pdf=false      (删除 PDF 分支)
- POST /api/jobs 上传 - 文件名路径穿越    (Path 安全检查)
- GET /api/jobs/stats/overview - PDF 输出目录计数
"""
import pytest
import pytest_asyncio
from pathlib import Path
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, MagicMock, patch


@pytest_asyncio.fixture
async def client(test_db):
    """基于 ASGITransport 的 API 客户端（依赖 test_db 隔离数据库）。"""
    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def client_with_job(test_db, tmp_path):
    """插入带真实 PDF 文件的 job（用于 delete + keep_pdf=false 测试）。

    PDF 必须放在 config.output_dir 下，否则 delete_job 的路径校验会拒绝删除。
    """
    import fitz
    from config import config as _cfg

    # output_dir 已被 test_db fixture 设为 tmp_path/output
    output_dir = Path(_cfg["app"].output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    job_dir = output_dir / "coverage-job"
    job_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open()
    doc.new_page().insert_text((50, 50), "Test content")
    pdf_path = job_dir / "test.pdf"
    doc.save(str(pdf_path))
    doc.close()

    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        ("coverage-job", "test.pdf", str(pdf_path), "review", 3),
    )
    # 插入 page_cache + findings（覆盖 stats 计数）
    await test_db.execute(
        "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
        "VALUES (?, ?, ?, ?)",
        (
            "coverage-job",
            1,
            "<p>page 1 text</p>",
            '{"steps":[{"step_no":1,"measurements":[{"values":{"A":"1.0"}}]}],"overall_confidence":"high"}',
        ),
    )
    await test_db.execute(
        "INSERT INTO findings (job_id, page, type, severity, source, description, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("coverage-job", 1, "参数越界", "critical", "rule", "test", "pending"),
    )
    await test_db.commit()

    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c, str(pdf_path)


class TestGetJobStatus:
    """GET /api/jobs/{id} — job 状态详情端点。"""

    @pytest.mark.asyncio
    async def test_get_status_returns_200(self, client_with_job):
        c, _ = client_with_job
        r = await c.get("/api/jobs/coverage-job")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == "coverage-job"
        assert data["filename"] == "test.pdf"
        assert data["status"] == "review"
        assert data["total_pages"] == 3
        # 验证所有字段都存在
        for field in (
            "pages_ocr_done",
            "pages_analyzed",
            "total_findings",
            "review_findings",
            "created_at",
            "finished_at",
            "error_message",
            "stage1_ms",
            "stage2_ms",
            "stage3_ms",
            "failed_pages",
        ):
            assert field in data, f"missing field: {field}"

    @pytest.mark.asyncio
    async def test_get_status_nonexistent_returns_404(self, client):
        r = await client.get("/api/jobs/does-not-exist")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_get_status_counts_correct(self, client_with_job):
        """pages_ocr_done / pages_analyzed / total_findings 计数应反映 DB。"""
        c, _ = client_with_job
        data = (await c.get("/api/jobs/coverage-job")).json()
        assert data["pages_ocr_done"] == 1
        assert data["pages_analyzed"] == 1
        assert data["total_findings"] == 1
        assert data["review_findings"] == 1  # status=pending


class TestGetPageData:
    """GET /api/jobs/{id}/pages/{page} — 单页数据。

    注：此路由由 api/review.py 的 get_page 端点处理（review_router 先注册），
    返回 {job_id, page, raw_html, structured}。
    api/jobs.py 的 get_page_data 端点因路径冲突未被命中（设计如此）。
    """

    @pytest.mark.asyncio
    async def test_get_page_with_structured_json(self, client_with_job):
        """有 structured_json 的页应返回 structured 字段。"""
        c, _ = client_with_job
        r = await c.get("/api/jobs/coverage-job/pages/1")
        assert r.status_code == 200
        data = r.json()
        assert data["page"] == 1
        assert "raw_html" in data
        assert data["structured"] is not None
        assert "overall_confidence" in data["structured"]

    @pytest.mark.asyncio
    async def test_get_page_with_parse_error(self, client_with_job, test_db):
        """structured_json 包含 _parse_error 时应被解析并返回。"""
        await test_db.execute(
            "UPDATE page_cache SET structured_json = ? "
            "WHERE job_id = ? AND page = ?",
            ('{"_parse_error": true, "overall_confidence": "low"}',
             "coverage-job", 1),
        )
        await test_db.commit()
        c, _ = client_with_job
        r = await c.get("/api/jobs/coverage-job/pages/1")
        assert r.status_code == 200
        data = r.json()
        assert data["structured"]["_parse_error"] is True
        assert data["structured"]["overall_confidence"] == "low"

    @pytest.mark.asyncio
    async def test_get_page_nonexistent_returns_404(self, client_with_job):
        c, _ = client_with_job
        r = await c.get("/api/jobs/coverage-job/pages/999")
        assert r.status_code == 404


class TestGetPageFindingsAll:
    """GET /api/jobs/{id}/findings — page=None 全量查询分支。"""

    @pytest.mark.asyncio
    async def test_findings_all_pages_includes_page_filter(self, client_with_job):
        """无 page 参数时应返回所有 findings。"""
        c, _ = client_with_job
        r = await c.get("/api/jobs/coverage-job/findings")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 1
        assert data["findings"][0]["type"] == "参数越界"

    @pytest.mark.asyncio
    async def test_findings_with_explicit_page(self, client_with_job):
        """带 page=1 应只返回该页 findings。"""
        c, _ = client_with_job
        r = await c.get("/api/jobs/coverage-job/findings?page=1")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 1


class TestUnarchive:
    """POST /api/jobs/{id}/unarchive — 取消归档。"""

    @pytest.mark.asyncio
    async def test_unarchive_archived_job(self, client, test_db):
        """archived → review 合法转换。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) "
            "VALUES (?, ?, ?, ?)",
            ("archived-job", "a.pdf", "/tmp/a.pdf", "archived"),
        )
        await test_db.commit()

        r = await client.post("/api/jobs/archived-job/unarchive")
        assert r.status_code == 200
        assert r.json()["status"] == "review"

    @pytest.mark.asyncio
    async def test_unarchive_non_archived_returns_400(self, client, test_db):
        """review → review 非法（不能从 review unarchive）。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) "
            "VALUES (?, ?, ?, ?)",
            ("active-job", "a.pdf", "/tmp/a.pdf", "review"),
        )
        await test_db.commit()

        r = await client.post("/api/jobs/active-job/unarchive")
        assert r.status_code == 400


class TestDeleteJobKeepPdfBranches:
    """DELETE /api/jobs/{id}?keep_pdf=true|false — 两个分支。"""

    @pytest.mark.asyncio
    async def test_delete_with_keep_pdf_true(self, client_with_job):
        """keep_pdf=true 应保留 PDF 文件。"""
        c, pdf_path = client_with_job
        r = await c.delete("/api/jobs/coverage-job?keep_pdf=true")
        assert r.status_code == 200
        assert r.json()["deleted"] is True
        assert r.json()["keep_pdf"] is True
        # 文件应仍然存在
        from pathlib import Path
        assert Path(pdf_path).exists()

    @pytest.mark.asyncio
    async def test_delete_with_keep_pdf_false_removes_file(self, client_with_job):
        """keep_pdf=false 应删除整个 job 目录。"""
        c, pdf_path = client_with_job
        r = await c.delete("/api/jobs/coverage-job?keep_pdf=false")
        assert r.status_code == 200
        assert r.json()["keep_pdf"] is False
        # PDF 文件应被删除
        from pathlib import Path
        assert not Path(pdf_path).exists()


class TestUploadSecurity:
    """POST /api/jobs — 文件名安全 + 大文件保护。"""

    @pytest.mark.asyncio
    async def test_upload_strips_path_traversal_in_filename(self, client, tmp_path):
        """Path(file.filename).name 应剥离路径分隔符。"""
        import fitz
        doc = fitz.open()
        doc.new_page().insert_text((50, 50), "Test")
        pdf_path = tmp_path / "safe.pdf"
        doc.save(str(pdf_path))
        doc.close()

        with patch("api.jobs.launch_pipeline") as mock_launch:
            mock_launch.return_value = MagicMock()
            with open(pdf_path, "rb") as f:
                # 攻击者尝试写入上级目录
                r = await client.post(
                    "/api/jobs",
                    files={"file": ("../../etc/evil.pdf", f, "application/pdf")},
                )
        assert r.status_code == 200
        # 文件名应被清洗为 basename
        assert r.json()["filename"] == "evil.pdf"

    @pytest.mark.asyncio
    async def test_upload_rejects_oversized_pdf(self, client, tmp_path):
        """超过 200MB 限制应返回 400。"""
        # 创建假 PDF（仅头部），通过 mock file.read 让 total_bytes > 200MB
        # 由于 chunk 写入逻辑会在写入时检查，我们需要让第一次 chunk 就超限
        # 简化：直接 mock file.read 返回大 chunk
        from fastapi import UploadFile
        from io import BytesIO

        big_data = b"%PDF-1.4" + b"0" * (210 * 1024 * 1024)  # 210MB
        with patch("api.jobs.launch_pipeline") as mock_launch:
            mock_launch.return_value = MagicMock()
            # 直接通过 ASGI 上传大文件
            r = await client.post(
                "/api/jobs",
                files={"file": ("big.pdf", BytesIO(big_data), "application/pdf")},
            )
        assert r.status_code == 400
        assert "200" in r.text or "too large" in r.text.lower()


class TestStatsOverviewEdgeCases:
    """GET /api/jobs/stats/overview — 输出目录 / 数据库路径分支。"""

    @pytest.mark.asyncio
    async def test_stats_with_pdfs_in_output_dir(self, client, test_db, tmp_path):
        """output_dir 含 PDF 文件时 pdf_storage.count 和 size_mb 应反映。"""
        from config import config as _cfg
        from pathlib import Path

        # 在临时 output_dir 中创建 PDF 文件
        out_dir = Path(_cfg["app"].output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        fake_pdf = out_dir / "job-1" / "doc.pdf"
        fake_pdf.parent.mkdir(parents=True, exist_ok=True)
        fake_pdf.write_bytes(b"%PDF-1.4 fake content")

        r = await client.get("/api/jobs/stats/overview")
        assert r.status_code == 200
        data = r.json()
        assert data["pdf_storage"]["count"] >= 1
        assert data["pdf_storage"]["size_mb"] >= 0


class TestJobsModuleDirectFunctions:
    """直接调用 api/jobs.py 中未被路由命中的函数（路径冲突屏蔽的端点）。

    api/jobs.py 的 get_page_data 和 get_page_findings 因 review_router 先注册
    而未被路由命中。这里直接调用函数以覆盖其内部逻辑。
    """

    @pytest.mark.asyncio
    async def test_get_page_data_direct_call(self, test_db):
        """直接调用 get_page_data 覆盖 page_confidence / parse_error 分支。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("direct-job", "t.pdf", "/tmp/t.pdf", "review", 1),
        )
        await test_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
            "VALUES (?, ?, ?, ?)",
            (
                "direct-job",
                1,
                "<p>html</p>",
                '{"overall_confidence":"high","_parse_error":false}',
            ),
        )
        await test_db.commit()

        from api.jobs import get_page_data
        result = await get_page_data("direct-job", 1)
        assert result["page"] == 1
        assert result["raw_html"] == "<p>html</p>"
        assert result["page_confidence"] == "high"
        assert result["page_parse_error"] is False

    @pytest.mark.asyncio
    async def test_get_page_data_with_parse_error(self, test_db):
        """structured_json 含 _parse_error=true 时应反映。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) "
            "VALUES (?, ?, ?, ?)",
            ("direct-job-2", "t.pdf", "/tmp/t.pdf", "review"),
        )
        await test_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
            "VALUES (?, ?, ?, ?)",
            (
                "direct-job-2",
                1,
                "<p>html</p>",
                '{"overall_confidence":"low","_parse_error":true}',
            ),
        )
        await test_db.commit()

        from api.jobs import get_page_data
        result = await get_page_data("direct-job-2", 1)
        assert result["page_parse_error"] is True
        assert result["page_confidence"] == "low"

    @pytest.mark.asyncio
    async def test_get_page_data_invalid_json(self, test_db):
        """structured_json 为非法 JSON 时 page_confidence 应为空字符串。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) "
            "VALUES (?, ?, ?, ?)",
            ("direct-job-3", "t.pdf", "/tmp/t.pdf", "review"),
        )
        await test_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
            "VALUES (?, ?, ?, ?)",
            ("direct-job-3", 1, "<p>html</p>", "not-valid-json{"),
        )
        await test_db.commit()

        from api.jobs import get_page_data
        result = await get_page_data("direct-job-3", 1)
        # 非法 JSON 应被 try/except 捕获，page_confidence 保持空
        assert result["page_confidence"] == ""
        assert result["page_parse_error"] is False

    @pytest.mark.asyncio
    async def test_get_page_data_nonexistent_returns_404(self, test_db):
        """page 不存在应抛 HTTPException 404。"""
        from fastapi import HTTPException
        from api.jobs import get_page_data

        with pytest.raises(HTTPException) as exc_info:
            await get_page_data("no-such-job", 1)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_page_data_null_structured_json(self, test_db):
        """structured_json 为 NULL 时 page_confidence 应为空。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) "
            "VALUES (?, ?, ?, ?)",
            ("direct-job-4", "t.pdf", "/tmp/t.pdf", "review"),
        )
        await test_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html) "
            "VALUES (?, ?, ?)",
            ("direct-job-4", 1, "<p>html only</p>"),
        )
        await test_db.commit()

        from api.jobs import get_page_data
        result = await get_page_data("direct-job-4", 1)
        assert result["page_confidence"] == ""
        assert result["page_parse_error"] is False

    @pytest.mark.asyncio
    async def test_get_page_findings_with_page_filter(self, test_db):
        """直接调用 get_page_findings 测试 page 过滤分支。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) "
            "VALUES (?, ?, ?, ?)",
            ("findings-job", "t.pdf", "/tmp/t.pdf", "review"),
        )
        await test_db.executemany(
            "INSERT INTO findings (job_id, page, type, severity, source, description, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("findings-job", 1, "type1", "critical", "rule", "d1", "pending"),
                ("findings-job", 2, "type2", "warning", "llm_page", "d2", "pending"),
                ("findings-job", 1, "type3", "info", "rule", "d3", "confirmed"),
            ],
        )
        await test_db.commit()

        from api.jobs import get_page_findings
        # 带 page 参数
        result = await get_page_findings("findings-job", page=1)
        assert result["count"] == 2
        assert all(f["page"] == 1 for f in result["findings"])

    @pytest.mark.asyncio
    async def test_get_page_findings_without_page(self, test_db):
        """直接调用 get_page_findings 测试无 page 参数分支。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) "
            "VALUES (?, ?, ?, ?)",
            ("findings-job-2", "t.pdf", "/tmp/t.pdf", "review"),
        )
        await test_db.executemany(
            "INSERT INTO findings (job_id, page, type, severity, description, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("findings-job-2", 1, "type1", "critical", "d1", "pending"),
                ("findings-job-2", 2, "type2", "warning", "d2", "pending"),
            ],
        )
        await test_db.commit()

        from api.jobs import get_page_findings
        # 不带 page 参数
        result = await get_page_findings("findings-job-2", page=None)
        assert result["count"] == 2
