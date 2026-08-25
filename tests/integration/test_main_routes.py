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
from pathlib import Path


@pytest_asyncio.fixture
async def client_with_job(test_db):
    """预置一条 job 后再构建 ASGITransport 客户端。

    用于需要 DB 数据的路由测试（review 页 / PDF 服务）。
    插入的 job pdf_path 指向 output_dir 内不存在的文件，便于测试 404 路径。
    （serve_pdf 有路径遍历防护，pdf_path 必须在 output_dir 内）
    """
    from config import config as _cfg
    from pathlib import Path
    output_dir = Path(_cfg["app"].output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # 路径在 output_dir 内但文件不存在 → 应返回 404 PDF file missing
    pdf_path = output_dir / "nonexistent_test.pdf"
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        ("route-job", "test.pdf", str(pdf_path), "review", 3),
    )
    await test_db.commit()

    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
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

    @pytest.mark.asyncio
    async def test_review_page_with_ocr_diagnostics_renders(
        self, client_with_job, test_db
    ):
        """对抗审查 P0 回归：ocr_diagnostics 非空时 review 页不得 500。

        main.py 曾缺 import json —— json.loads(row["ocr_diagnostics"])
        抛 NameError 使整个复核页崩溃。Stage 0 assess_ocr_page 给低 DPI /
        超大 MediaBox 的页写入诊断即触发（真实扫描件必现）。
        """
        await test_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html, ocr_diagnostics) "
            "VALUES (?, ?, ?, ?)",
            (
                "route-job", 1, "<p>x</p>",
                '{"integrity": "incomplete", "reasons": ["low_dpi"], '
                '"effective_dpi": 150}',
            ),
        )
        await test_db.commit()
        r = await client_with_job.get("/jobs/route-job/review")
        assert r.status_code == 200
        assert "<html" in r.text.lower()

    @pytest.mark.asyncio
    async def test_review_page_llm_truncated_flag_renders_banner(
        self, client_with_job, test_db
    ):
        """对抗审查 P1 回归：_truncated_warn/_schema_warn 必须透出到 SSR。

        此前这两个标记只写进 structured_json 无任何消费终端 — 截断被
        静默恢复后尾部数据丢失，复核者看不到任何提示。
        """
        await test_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
            "VALUES (?, ?, ?, ?)",
            (
                "route-job", 2, "<p>x</p>",
                '{"steps": [], "_truncated_warn": true, '
                '"_schema_warn": ["findings[0].type: missing"]}',
            ),
        )
        await test_db.commit()
        r = await client_with_job.get("/jobs/route-job/review?page=2")
        assert r.status_code == 200
        assert "llm-integrity-banner" in r.text
        # 关键断言：横幅内容出现（非 hidden 态由模板条件分支保证）
        assert "输出过长被截断后自动恢复" in r.text
        assert "结构校验未完全通过" in r.text


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

    @pytest.mark.asyncio
    async def test_pdf_non_local_host_returns_403(self, test_db, test_client):
        """对抗审查 P1 回归：serve_pdf 是最后一个无守卫的读端点。

        最敏感资产（原始 PDF）此前是唯一没有 is_local_request 守卫的
        GET 读端点，与"所有读端点统一守卫"策略不一致。
        """
        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = output_dir / "guard_probe.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n")
        try:
            await test_db.execute(
                "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
                "VALUES (?, ?, ?, ?, ?)",
                ("guarded-pdf", "g.pdf", str(pdf_path.resolve()), "review", 1),
            )
            await test_db.commit()
            r = await test_client.get(
                "/api/jobs/guarded-pdf/pdf", headers={"Host": "evil.com:80"}
            )
            assert r.status_code == 403
        finally:
            pdf_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_pdf_path_traversal_blocked(self, test_db, test_client):
        """serve_pdf 路径遍历防护：pdf_path 在 output_dir 外 → 403。

        攻击场景：DB 被篡改（SQL 注入或直接改库），pdf_path 指向
        任意系统文件（如 C:\\Windows\\win.ini），可读取敏感文件。
        """
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("traversal-job", "evil.pdf", "C:/Windows/win.ini", "review", 1),
        )
        await test_db.commit()
        r = await test_client.get("/api/jobs/traversal-job/pdf")
        assert r.status_code == 403


class TestLiveRouteNotShadowed:
    """GET /api/jobs/live — 模块拆分路由顺序回归（冻结版 e2e 发现）。

    背景：api/jobs.py 拆分后，listings.py 顶层 from api.jobs.status import
    ... 令 status.py 的 /{job_id} 先注册，FastAPI 按注册顺序匹配，
    GET /api/jobs/live 会命中 /{job_id}，查询 job_id="live" 不存在返回 404
    （冻结版 e2e：{"detail":"Job not found"}，upload 页 EventSource 404 →
    实时进度降级为 10s 轮询）。修复：listings.py 改为函数体内延迟引入
    status 符号（见 listings.py 头注释），/live 在 /{job_id} 之前注册。

    注意：不能用 httpx stream 请求验证（ASGITransport 无法中途断开
    永不结束的 SSE 流，会挂起测试）；直接断言路由注册顺序最稳定。
    """

    @pytest.mark.asyncio
    async def test_live_registered_before_job_id(self):
        from api.jobs import router as jobs_router
        routes = [
            r for r in jobs_router.routes
            if "GET" in (getattr(r, "methods", None) or ())
            and getattr(r, "path", None) in ("/api/jobs/live", "/api/jobs/{job_id}")
        ]
        paths = [r.path for r in routes]
        assert paths == ["/api/jobs/live", "/api/jobs/{job_id}"], (
            f"route order broken: {paths} — listings must load before status "
            "(listings.py must not top-level import status)"
        )
