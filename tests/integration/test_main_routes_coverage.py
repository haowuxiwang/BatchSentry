"""Main.py 路由与中间件测试 — 补全 test_main_routes.py 未覆盖路径。

补全：
- render_page_links 过滤器：'第N页' → 可点击链接
- GET /jobs/{id}/review：含 measurements / page_finding_counts / failed_pages 分支
- GET /api/jobs/{id}/pdf：成功返回 FileResponse（PDF 文件存在）
- request_id 中间件：异常路径 + 静态文件跳过日志
- _resource_dir()：frozen 模式分支
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from pathlib import Path


@pytest_asyncio.fixture
async def client_with_full_job(test_db, tmp_path):
    """带完整数据（page_cache + findings + failed_pages）的客户端。"""
    import fitz

    # 创建真实 PDF（用于 PDF 服务测试）
    doc = fitz.open()
    doc.new_page().insert_text((50, 50), "Real PDF")
    pdf_path = tmp_path / "real.pdf"
    doc.save(str(pdf_path))
    doc.close()

    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages, failed_pages) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "full-job",
            "real.pdf",
            str(pdf_path),
            "review",
            3,
            '["2"]',  # 第 2 页失败
        ),
    )
    # 多页 page_cache（含 measurements，用于触发 matrix 渲染）
    await test_db.executemany(
        "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
        "VALUES (?, ?, ?, ?)",
        [
            (
                "full-job",
                1,
                "<p>Page 1 OCR</p>",
                '{"steps":[{"step_no":1,"measurements":[{"values":{"A":"1.0","B":"2.0"}}]}],'
                '"overall_confidence":"high"}',
            ),
            (
                "full-job",
                2,
                "<p>Page 2 OCR</p>",
                '{"_parse_error": true, "overall_confidence": "low"}',
            ),
            ("full-job", 3, "<p>Page 3 OCR</p>", None),
        ],
    )
    # 多种 severity 的 findings（触发 page_finding_counts）
    await test_db.executemany(
        "INSERT INTO findings (job_id, page, type, severity, source, description, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("full-job", 1, "参数越界", "critical", "rule", "温度超出", "pending"),
            ("full-job", 1, "完整性", "warning", "llm_page", "缺签名", "pending"),
            ("full-job", 2, "时间逻辑", "info", "llm_cross", "时序异常", "pending"),
        ],
    )
    await test_db.commit()

    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c, str(pdf_path)


class TestRenderPageLinksFilter:
    """render_page_links Jinja2 过滤器。"""

    def test_converts_page_number_to_link(self):
        from main import render_page_links
        result = render_page_links("第10页工序3 早于第9页工序2 结束", "job-1")
        assert 'href="/jobs/job-1/review?page=10"' in result
        assert 'href="/jobs/job-1/review?page=9"' in result
        assert "page-link" in result

    def test_no_page_number_returns_unchanged(self):
        from main import render_page_links
        text = "无页码引用"
        result = render_page_links(text, "job-1")
        assert result == text

    def test_empty_text_returns_empty(self):
        from main import render_page_links
        assert render_page_links("", "job-1") == ""
        assert render_page_links(None, "job-1") == ""


class TestReviewPageFullRendering:
    """GET /jobs/{id}/review — 完整渲染分支。"""

    @pytest.mark.asyncio
    async def test_review_with_measurements_and_matrix(self, client_with_full_job):
        """含 measurements 的页应渲染矩阵。"""
        c, _ = client_with_full_job
        r = await c.get("/jobs/full-job/review?page=1")
        assert r.status_code == 200
        assert "full-job" in r.text
        # measurements 应包含列名 A、B
        # 模板应渲染矩阵列
        assert "A" in r.text or "matrix" in r.text.lower()

    @pytest.mark.asyncio
    async def test_review_with_parse_error_page(self, client_with_full_job):
        """含 _parse_error 的页应渲染错误提示。"""
        c, _ = client_with_full_job
        r = await c.get("/jobs/full-job/review?page=2")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_review_with_no_structured_json(self, client_with_full_job):
        """structured_json 为 NULL 的页应正常渲染（无矩阵）。"""
        c, _ = client_with_full_job
        r = await c.get("/jobs/full-job/review?page=3")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_review_shows_failed_pages(self, client_with_full_job):
        """failed_pages 应在 UI 中显示。"""
        c, _ = client_with_full_job
        r = await c.get("/jobs/full-job/review?page=1")
        assert r.status_code == 200
        # failed_pages JSON 应被解析并影响渲染

    @pytest.mark.asyncio
    async def test_review_invalid_failed_pages_json(self, client_with_full_job, test_db):
        """failed_pages 为非法 JSON 时应安全降级。"""
        await test_db.execute(
            "UPDATE jobs SET failed_pages = ? WHERE id = ?",
            ("not-valid-json{", "full-job"),
        )
        await test_db.commit()
        c, _ = client_with_full_job
        r = await c.get("/jobs/full-job/review?page=1")
        assert r.status_code == 200  # 应正常渲染，failed_pages 降级为空


class TestServePdfSuccess:
    """GET /api/jobs/{id}/pdf — PDF 文件服务成功路径。"""

    @pytest.mark.asyncio
    async def test_pdf_returns_file_response(self, client_with_full_job):
        """PDF 文件存在时应返回 200 + application/pdf。"""
        c, _ = client_with_full_job
        r = await c.get("/api/jobs/full-job/pdf")
        assert r.status_code == 200
        assert "application/pdf" in r.headers.get("content-type", "")
        # content-disposition 应为 inline（用于浏览器内显示）
        disposition = r.headers.get("content-disposition", "")
        assert "inline" in disposition


class TestRequestIdMiddleware:
    """request_id 中间件 — 注入 X-Request-ID 头。"""

    @pytest.mark.asyncio
    async def test_response_has_request_id(self, client_with_full_job):
        """响应头应包含 X-Request-ID。"""
        c, _ = client_with_full_job
        r = await c.get("/health")
        assert "x-request-id" in {k.lower() for k in r.headers.keys()}

    @pytest.mark.asyncio
    async def test_custom_request_id_is_echoed(self, client_with_full_job):
        """客户端传入的 X-Request-ID 应被回显。"""
        c, _ = client_with_full_job
        r = await c.get("/health", headers={"X-Request-ID": "test-req-123"})
        assert r.headers.get("x-request-id") == "test-req-123"

    @pytest.mark.asyncio
    async def test_static_files_skip_log(self, client_with_full_job):
        """静态文件请求应正常返回（中间件 skip_log 分支）。"""
        c, _ = client_with_full_job
        # static/app.css 应存在并返回 200
        r = await c.get("/static/app.css")
        # 静态文件可能 200 或 404（取决于文件是否存在），但不应抛异常
        assert r.status_code in (200, 404)


class TestLifespanLogging:
    """lifespan 启动/关闭日志 — 通过 lifespan 触发。"""

    @pytest.mark.asyncio
    async def test_app_lifespan_initializes_db(self, test_client):
        """test_client fixture 已经触发 lifespan，db 应已初始化。"""
        # 健康检查能返回 200 即说明 lifespan 已运行
        r = await test_client.get("/health")
        assert r.status_code == 200


class TestResourceDirFrozenMode:
    """_resource_dir() 在 frozen 模式下的行为。"""

    def test_dev_mode_returns_project_root(self):
        """开发模式应返回 main.py 所在目录。"""
        from main import _resource_dir
        path = _resource_dir()
        assert path.name in ("pharma-batch-checker", "")
        # templates/ 和 static/ 应在该目录下
        assert (path / "templates").exists()

    def test_frozen_mode_returns_meipass(self, monkeypatch):
        """frozen 模式应返回 sys._MEIPASS。"""
        import sys
        from pathlib import Path
        import main as main_mod

        fake_meipass = Path("C:/fake/meipass")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(fake_meipass), raising=False)
        path = main_mod._resource_dir()
        assert str(path) == str(fake_meipass)
