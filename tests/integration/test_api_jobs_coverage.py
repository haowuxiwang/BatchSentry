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
            "ocr_progress",
            "page_finding_counts",
        ):
            assert field in data, f"missing field: {field}"

    @pytest.mark.asyncio
    async def test_get_status_ocr_progress_parsed(self, client_with_job, test_db):
        """jobs.ocr_progress JSON 字符串应解析为 {done, total} dict。"""
        await test_db.execute(
            "UPDATE jobs SET ocr_progress = ? WHERE id = ?",
            ('{"done": 12, "total": 51}', "coverage-job"),
        )
        await test_db.commit()
        c, _ = client_with_job
        data = (await c.get("/api/jobs/coverage-job")).json()
        assert data["ocr_progress"] == {"done": 12, "total": 51}

    @pytest.mark.asyncio
    async def test_get_status_ocr_progress_none(self, client_with_job):
        """未设置 ocr_progress 时返回空 dict（前端回退到 pages_ocr_done）。"""
        c, _ = client_with_job
        data = (await c.get("/api/jobs/coverage-job")).json()
        assert data["ocr_progress"] == {}

    @pytest.mark.asyncio
    async def test_get_status_reports_ocr_backend_used(self, client_with_job, test_db):
        """双 OCR 审计：快照应包含实际使用的 OCR 后端（未设置时为 None）。"""
        c, _ = client_with_job
        data = (await c.get("/api/jobs/coverage-job")).json()
        assert data["ocr_backend_used"] is None

        await test_db.execute(
            "UPDATE jobs SET ocr_backend_used = ? WHERE id = ?",
            ("paddle", "coverage-job"),
        )
        await test_db.commit()
        data = (await c.get("/api/jobs/coverage-job")).json()
        assert data["ocr_backend_used"] == "paddle"

    @pytest.mark.asyncio
    async def test_get_status_page_finding_counts(self, client_with_job):
        """page_finding_counts 应按 (page, severity) 聚合统计。

        注：JSON 序列化后 int key 变为字符串（如 "1"），前端 JS 对象访问
        counts[1] 会自动转字符串，无碍；此处按序列化后的 key 断言。
        """
        c, _ = client_with_job
        data = (await c.get("/api/jobs/coverage-job")).json()
        pfc = data["page_finding_counts"]
        # client_with_job 预置了 1 条 critical finding（第 1 页，见 fixture）
        assert pfc.get("1", {}).get("critical", 0) >= 1
        assert pfc.get("1", {}).get("total", 0) >= 1

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

    @pytest.mark.asyncio
    async def test_unarchive_when_pdf_missing_still_succeeds(self, client, test_db):
        """对抗审查(cr-3): PDF 已被删除的归档 job 也应能恢复（数据库记录
        仍在，仅提示 PDF 文件缺失，不阻断 unarchive）。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) "
            "VALUES (?, ?, ?, ?)",
            ("ghost-job", "ghost.pdf", "/tmp/does-not-exist.pdf", "archived"),
        )
        await test_db.commit()

        r = await client.post("/api/jobs/ghost-job/unarchive")
        assert r.status_code == 200
        assert r.json()["status"] == "review"
        assert r.json()["pdf_missing"] is True


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
    async def test_upload_rejects_oversized_pdf(self, test_db):
        """超过 200MB 限制应返回 400。

        流式检查设计：create_job 逐块读 + 累计 total_bytes，超过 _MAX_PDF_BYTES
        即拒绝。测试应验证"累计超限"逻辑而非真分配 210MB 内存。
        """
        from fastapi import HTTPException
        from api.jobs import create_job, _MAX_PDF_BYTES

        class _StubUpload:
            """模拟 UploadFile，read 第一次返回超大 chunk 触发超限检查。"""
            filename = "huge.pdf"

            async def read(self, size: int = -1) -> bytes:
                # 返回一个略大于 _MAX_PDF_BYTES 的 chunk，单次 read 即超限
                return b"%PDF-1.4" + b"0" * (_MAX_PDF_BYTES + 1)

            async def close(self):
                pass

        with patch("api.jobs.launch_pipeline"):
            with pytest.raises(HTTPException) as exc:
                await create_job(file=_StubUpload())
        assert exc.value.status_code == 400
        assert "200" in str(exc.value.detail) or "too large" in str(exc.value.detail).lower()


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

    api/jobs.py 的 get_page_data 因 review_router 先注册而未被路由命中。
    这里直接调用函数以覆盖其内部逻辑。
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


class TestConcurrencyGuard:
    """POST /api/jobs — 并发上限保护（lines 46-49，409 路径）。"""

    @pytest.mark.asyncio
    async def test_upload_rejected_when_concurrent_limit_reached(self, client, test_db, tmp_path):
        """active job 数 >= 上限时应返回 409。"""
        import fitz
        # 插入 1 个 active job（pending 属于 _ACTIVE_STATUSES）
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) VALUES (?, ?, ?, ?)",
            ("active-job", "a.pdf", "/tmp/a.pdf", "pending"),
        )
        await test_db.commit()

        doc = fitz.open()
        doc.new_page().insert_text((50, 50), "Test")
        pdf_path = tmp_path / "new.pdf"
        doc.save(str(pdf_path))
        doc.close()

        # 将上限降到 1 → 已有 1 个 active job → 新上传被拒
        with patch("api.jobs._MAX_CONCURRENT_JOBS", 1), \
                patch("api.jobs.launch_pipeline"):
            with open(pdf_path, "rb") as f:
                r = await client.post(
                    "/api/jobs",
                    files={"file": ("new.pdf", f, "application/pdf")},
                )
        assert r.status_code == 409
        assert "上限" in r.text or "409" in r.text


class TestUploadFilenameFallback:
    """POST /api/jobs — 文件名清洗回退（line 63，safe_name 为空时回退到 {job_id}.pdf）。"""

    @pytest.mark.asyncio
    async def test_filename_fallback_when_path_name_empty(self, test_db, tmp_path):
        """当 Path(filename).name 为空时，回退到 {job_id}.pdf。

        通过 fake_path 让 Path("evil.pdf").name 返回空串，触发 line 62-63 的回退分支。
        该分支在正常 pathlib 语义下结合 .pdf 后缀检查属防御性代码，这里通过
        构造 stub 显式触达。
        """
        import fitz
        from pathlib import Path as RealPath
        from api.jobs import create_job

        doc = fitz.open()
        doc.new_page().insert_text((50, 50), "Test")
        src = tmp_path / "src.pdf"
        doc.save(str(src))
        doc.close()
        data = src.read_bytes()

        class _StubUpload:
            filename = "evil.pdf"  # 通过 .pdf 后缀检查

            def __init__(self, d):
                self._d = d
                self._p = 0

            async def read(self, n):
                if self._p >= len(self._d):
                    return b""
                chunk = self._d[self._p:self._p + n]
                self._p += len(chunk)
                return chunk

        # 构造 fake Path：对 "evil.pdf" 返回 name 为空的 mock，其余走真实 Path
        def fake_path(arg):
            p = RealPath(arg)
            if arg == "evil.pdf":
                m = MagicMock(spec=p)
                m.name = ""  # 触发 `not safe_name` 回退
                return m
            return p

        with patch("api.jobs.Path", fake_path), patch("api.jobs.launch_pipeline"):
            result = await create_job(file=_StubUpload(data))
        # 回退后的 filename 应为 {job_id}.pdf
        assert result["filename"] == f"{result['job_id']}.pdf"
        assert result["status"] == "pending"


class TestUploadWriteFailure:
    """POST /api/jobs — 上传写盘失败（lines 83-87，500 路径）。"""

    @pytest.mark.asyncio
    async def test_upload_write_failure_returns_500(self, test_db):
        """file.read 抛 OSError 时应返回 500。"""
        from fastapi import HTTPException
        from api.jobs import create_job

        class _FailingReadUpload:
            filename = "test.pdf"

            async def read(self, n=-1):
                raise OSError("simulated read failure")

        with patch("api.jobs.launch_pipeline"):
            with pytest.raises(HTTPException) as exc:
                await create_job(file=_FailingReadUpload())
        assert exc.value.status_code == 500
        assert "Upload failed" in str(exc.value.detail) or "500" in str(exc.value.status_code)


class TestUploadMagicBytes:
    """POST /api/jobs — magic bytes 校验（lines 97-105，400 + 500 路径）。"""

    @pytest.mark.asyncio
    async def test_upload_bad_magic_bytes_returns_400(self, client):
        """文件内容不以 %PDF- 开头应返回 400（lines 97-99）。"""
        from io import BytesIO
        # 19 字节非 PDF 内容，通过 total_bytes >= 5 检查
        r = await client.post(
            "/api/jobs",
            files={"file": ("bad.pdf", BytesIO(b"NOTPDF-content-12345"), "application/pdf")},
        )
        assert r.status_code == 400
        assert "PDF" in r.text or "valid PDF" in r.text.lower()

    @pytest.mark.asyncio
    async def test_upload_magic_check_exception_returns_500(self, test_db, tmp_path, monkeypatch):
        """magic bytes 读取异常时应返回 500（lines 102-105）。"""
        import fitz
        from fastapi import HTTPException
        from api.jobs import create_job

        doc = fitz.open()
        doc.new_page().insert_text((50, 50), "Test")
        src = tmp_path / "src.pdf"
        doc.save(str(src))
        doc.close()
        data = src.read_bytes()  # 先读取（在 patch open 之前）

        class _StubUpload:
            filename = "test.pdf"

            def __init__(self, d):
                self._d = d
                self._p = 0

            async def read(self, n):
                if self._p >= len(self._d):
                    return b""
                chunk = self._d[self._p:self._p + n]
                self._p += len(chunk)
                return chunk

        real_open = open

        def fake_open(path, mode="r", *args, **kwargs):
            # 读模式抛错 → 触发 magic bytes 校验的 except Exception 分支
            if "rb" in mode:
                raise OSError("simulated read failure")
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr("api.jobs.open", fake_open, raising=False)

        with patch("api.jobs.launch_pipeline"):
            with pytest.raises(HTTPException) as exc:
                await create_job(file=_StubUpload(data))
        assert exc.value.status_code == 500
        assert "validation failed" in str(exc.value.detail).lower() or "500" in str(exc.value.status_code)


class TestGetJobProgress:
    """_get_job_progress 辅助函数（lines 186-207）。"""

    @pytest.mark.asyncio
    async def test_get_job_progress_returns_snapshot(self, test_db):
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("prog-job", "t.pdf", "/tmp/t.pdf", "review", 3),
        )
        await test_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
            "VALUES (?, ?, ?, ?)",
            ("prog-job", 1, "<p>html</p>", '{"overall_confidence":"high"}'),
        )
        await test_db.execute(
            "INSERT INTO findings (job_id, page, type, severity, source, description, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("prog-job", 1, "type1", "critical", "rule", "d1", "pending"),
        )
        await test_db.commit()

        from api.jobs import _get_job_progress
        result = await _get_job_progress(test_db, "prog-job")
        assert result is not None
        assert result["id"] == "prog-job"
        assert result["status"] == "review"
        assert result["total_pages"] == 3
        assert result["pages_ocr_done"] == 1
        assert result["pages_analyzed"] == 1
        assert result["total_findings"] == 1

    @pytest.mark.asyncio
    async def test_get_job_progress_returns_none_for_missing(self, test_db):
        from api.jobs import _get_job_progress
        result = await _get_job_progress(test_db, "no-such-job")
        assert result is None


class TestStreamJobProgress:
    """GET /api/jobs/{id}/stream — SSE 端点（lines 228-248）。"""

    @pytest.mark.asyncio
    async def test_stream_terminal_job_sends_done_event(self, client, test_db):
        """review 终态 job 应推送 data + done 事件后关闭。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) VALUES (?, ?, ?, ?)",
            ("stream-job", "t.pdf", "/tmp/t.pdf", "review"),
        )
        await test_db.commit()

        body = ""
        async with client.stream("GET", "/api/jobs/stream-job/stream") as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                body += chunk
                if "event: done" in body:
                    break
        assert "data:" in body
        assert "event: done" in body

    @pytest.mark.asyncio
    async def test_stream_missing_job_sends_error_event(self, client):
        """不存在的 job 应推送 error 事件后关闭。"""
        body = ""
        async with client.stream("GET", "/api/jobs/no-such-job/stream") as resp:
            async for chunk in resp.aiter_text():
                body += chunk
                if "event: error" in body:
                    break
        assert "event: error" in body
        assert "Job not found" in body

    @pytest.mark.asyncio
    async def test_stream_events_carry_id_and_retry(self, client, test_db):
        """每条 SSE 事件带自增 id + 块头 retry 指令（断线重连语义）。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) VALUES (?, ?, ?, ?)",
            ("stream-id-job", "t.pdf", "/tmp/t.pdf", "review"),
        )
        await test_db.commit()

        body = ""
        async with client.stream("GET", "/api/jobs/stream-id-job/stream") as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                body += chunk
                if "event: done" in body:
                    break
        assert "retry: 2000" in body
        ids = [ln[3:].strip() for ln in body.splitlines() if ln.startswith("id: ")]
        assert len(ids) >= 2, f"expected sequential event ids, got {ids!r}"
        assert ids == [str(i) for i in range(1, len(ids) + 1)]

    @pytest.mark.asyncio
    async def test_progress_snapshot_includes_failed_pages(self, client, test_db):
        """SSE 快照（含 done 事件）应携带 failed_pages 便于终态判断。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, failed_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("stream-fp-job", "t.pdf", "/tmp/t.pdf", "partial_review", "[2]"),
        )
        await test_db.commit()

        body = ""
        async with client.stream("GET", "/api/jobs/stream-fp-job/stream") as resp:
            async for chunk in resp.aiter_text():
                body += chunk
                if "event: done" in body:
                    break
        assert '"failed_pages": "[2]"' in body

    @pytest.mark.asyncio
    async def test_live_snapshot_only_includes_active_jobs(self, test_db):
        """_live_jobs_snapshot 只含活跃任务，且快照携带终态判断所需字段。

        注：不走 ASGITransport 流式（httpx 0.28 的 ASGITransport 要等 app
        完成后才交付 Response，无法收永不结束的 SSE 流）；流式行为由
        真实 server 的 E2E（e2e_full.py）覆盖。
        """
        from api.jobs import _live_jobs_snapshot

        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) VALUES (?, ?, ?, ?)",
            ("live-active", "a.pdf", "/tmp/a.pdf", "analyzing"),
        )
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, failed_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("live-active2", "c.pdf", "/tmp/c.pdf", "ocr_running", None),
        )
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) VALUES (?, ?, ?, ?)",
            ("live-terminal", "b.pdf", "/tmp/b.pdf", "review"),
        )
        await test_db.commit()

        snaps = await _live_jobs_snapshot(test_db)
        ids = {s["id"] for s in snaps}
        assert ids == {"live-active", "live-active2"}, ids
        snap = next(s for s in snaps if s["id"] == "live-active2")
        assert "failed_pages" in snap
        assert "status" in snap and snap["status"] == "ocr_running"


class TestArchiveKeepPdfFalse:
    """POST /api/jobs/{id}/archive?keep_pdf=false — PDF 清理分支（lines 385-398）。"""

    @pytest.mark.asyncio
    async def test_archive_removes_pdf_when_keep_pdf_false(self, client_with_job):
        """keep_pdf=false 且 PDF 在 output_dir 内时应删除 job 目录（lines 385-396）。"""
        c, pdf_path = client_with_job
        from pathlib import Path
        assert Path(pdf_path).exists()

        r = await c.post("/api/jobs/coverage-job/archive?keep_pdf=false")
        assert r.status_code == 200
        assert r.json()["status"] == "archived"
        # PDF 及 job 目录应被删除
        assert not Path(pdf_path).exists()

    @pytest.mark.asyncio
    async def test_archive_skips_cleanup_when_pdf_outside_output(self, client, test_db, tmp_path):
        """pdf_path 在 output_dir 外时路径校验失败，跳过清理（lines 397-398）。"""
        from pathlib import Path
        outside_pdf = tmp_path / "outside.pdf"
        outside_pdf.write_bytes(b"%PDF-1.4 test")
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) VALUES (?, ?, ?, ?)",
            ("outside-archive-job", "t.pdf", str(outside_pdf), "review"),
        )
        await test_db.commit()

        r = await client.post("/api/jobs/outside-archive-job/archive?keep_pdf=false")
        assert r.status_code == 200
        # 路径校验失败 → 跳过 rmtree → 文件仍存在
        assert outside_pdf.exists()


class TestDeleteJobEdgeCases:
    """DELETE /api/jobs/{id} — 路径校验 + 文件清理边界（lines 454-459, 469-480）。"""

    @pytest.mark.asyncio
    async def test_delete_refuses_path_outside_output_dir(self, client, test_db, tmp_path):
        """pdf_path 在 output_dir 外时 DELETE keep_pdf=false 应返回 400（lines 454-459）。"""
        from pathlib import Path
        outside_pdf = tmp_path / "evil.pdf"
        outside_pdf.write_bytes(b"%PDF-1.4")
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) VALUES (?, ?, ?, ?)",
            ("evil-out-job", "t.pdf", str(outside_pdf), "review"),
        )
        await test_db.commit()

        r = await client.delete("/api/jobs/evil-out-job?keep_pdf=false")
        assert r.status_code == 400
        assert "outside" in r.text.lower() or "refused" in r.text.lower()

    @pytest.mark.asyncio
    async def test_delete_with_rmtree_failure_returns_warning(self, client_with_job, monkeypatch):
        """rmtree 失败时应返回带 warning 的部分成功响应（lines 469-478）。"""
        c, pdf_path = client_with_job

        def failing_rmtree(path, *args, **kwargs):
            raise OSError("simulated locked file")

        monkeypatch.setattr("shutil.rmtree", failing_rmtree)

        r = await c.delete("/api/jobs/coverage-job?keep_pdf=false")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["deleted"] is True
        assert "warning" in data

    @pytest.mark.asyncio
    async def test_delete_when_pdf_missing_logs_warning(self, client, test_db):
        """pdf_path 指向不存在文件时应走 else 分支（lines 479-480）。"""
        from pathlib import Path
        from config import config as _cfg

        output_dir = Path(_cfg["app"].output_dir)
        job_dir = output_dir / "ghost-job"
        job_dir.mkdir(parents=True, exist_ok=True)
        missing_pdf = job_dir / "ghost.pdf"  # 故意不创建文件
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) VALUES (?, ?, ?, ?)",
            ("ghost-job", "ghost.pdf", str(missing_pdf), "review"),
        )
        await test_db.commit()

        r = await client.delete("/api/jobs/ghost-job?keep_pdf=false")
        assert r.status_code == 200
        assert r.json()["deleted"] is True
