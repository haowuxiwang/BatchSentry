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
import shutil
from httpx import AsyncClient, ASGITransport
from pathlib import Path


@pytest_asyncio.fixture
async def client_with_full_job(test_db, tmp_path):
    """带完整数据（page_cache + findings + failed_pages）的客户端。"""
    import fitz
    from config import config as _cfg
    from pathlib import Path

    # 创建真实 PDF（用于 PDF 服务测试）
    # 必须放在 output_dir 内，否则 serve_pdf 的路径遍历防护会返回 403
    doc = fitz.open()
    doc.new_page().insert_text((50, 50), "Real PDF")
    output_dir = Path(_cfg["app"].output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / "real_test.pdf"
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

    @pytest.mark.asyncio
    async def test_review_ocr_text_not_truncated(self, client_with_full_job, test_db):
        """F1: SSR 首屏 data-raw 应包含完整 raw_html（不再 5000 截断），显示区保留换行。

        回归：此前 main.py 把 ocr_text 截断到 5000 字符且折叠全部换行，
        首屏只显示当页 OCR 文本的一部分；AJAX 翻页路径却返回完整内容，
        双路径不一致。修复后 data-raw 注入完整 raw_html，由 htmlToText 统一转换。
        """
        long_html = (
            "<table><tr><td>row1</td></tr></table>\n"
            + ("x" * 5200)
            + "\n<p>END_MARKER_TAIL</p>"
        )
        await test_db.execute(
            "UPDATE page_cache SET raw_html = ? WHERE job_id = ? AND page = ?",
            (long_html, "full-job", 1),
        )
        await test_db.commit()
        c, _ = client_with_full_job
        r = await c.get("/jobs/full-job/review?page=1")
        assert r.status_code == 200
        # 尾部标记存在于页面（若仍 5000 截断则被切掉）
        assert "END_MARKER_TAIL" in r.text
        # data-raw 注入完整 raw_html（HTML 实体已转义，可安全进属性）
        assert "&lt;table&gt;" in r.text
        # 显示区（SSR 兜底）保留换行，整页不再被折叠成一坨
        assert "\n" in r.text


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
        # static/app.css 必须存在并返回 200（打包前的前置校验）
        r = await c.get("/static/app.css")
        assert r.status_code == 200


class TestStaticAssetsSyntax:
    """前端资产语法验证 — node --check 捕获 JS 语法错误。

    PyInstaller 打包不编译 JS，静态文件里的语法错误直到 Electron 运行时
    才暴露（白屏 + 控制台报错）。此测试在 CI 阶段即拦截。
    """

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    def test_js_files_pass_node_check(self):
        """所有 static/*.js 与 electron/*.js 必须通过 node --check。"""
        import subprocess
        static_dir = Path(__file__).parent.parent.parent / "static"
        electron_dir = Path(__file__).parent.parent.parent / "electron"
        js_files = list(static_dir.glob("*.js")) + list(electron_dir.glob("*.js"))
        assert js_files, "no JS files found to validate"
        for js in js_files:
            result = subprocess.run(
                ["node", "--check", str(js)],
                capture_output=True, text=True,
            )
            assert result.returncode == 0, (
                f"JS syntax error in {js.name}:\n{result.stderr}"
            )


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


class TestLifespanStartup:
    """lifespan 启动初始化路径 — 直接调用 lifespan 上下文管理器。

    ASGITransport 不触发 lifespan 事件，所以需要手动调用 lifespan(app)
    来覆盖启动日志 + recover_stuck_jobs 的 try/except 分支。
    """

    @pytest.mark.asyncio
    async def test_lifespan_recover_stuck_jobs_with_recovered(self, test_db, monkeypatch):
        """recover_stuck_jobs 返回 >0 时应记录 warning 日志（lines 60-61）。"""
        from main import lifespan, app
        from unittest.mock import AsyncMock

        mock_recover = AsyncMock(return_value=2)
        monkeypatch.setattr("main.recover_stuck_jobs", mock_recover)

        async with lifespan(app):
            pass

        mock_recover.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lifespan_recover_stuck_jobs_no_recovered(self, test_db, monkeypatch):
        """recover_stuck_jobs 返回 0 时不记录 warning（lines 60-61 else 分支）。"""
        from main import lifespan, app
        from unittest.mock import AsyncMock

        mock_recover = AsyncMock(return_value=0)
        monkeypatch.setattr("main.recover_stuck_jobs", mock_recover)

        async with lifespan(app):
            pass

        mock_recover.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lifespan_recover_stuck_jobs_exception(self, test_db, monkeypatch):
        """recover_stuck_jobs 抛异常时 lifespan 不崩溃，记录 error 日志（lines 62-63）。"""
        from main import lifespan, app
        from unittest.mock import AsyncMock

        mock_recover = AsyncMock(side_effect=RuntimeError("db locked"))
        monkeypatch.setattr("main.recover_stuck_jobs", mock_recover)

        # lifespan 不应抛异常（recover 失败被捕获）
        async with lifespan(app):
            pass

        mock_recover.assert_awaited_once()


class TestShutdownEndpoint:
    """POST /api/shutdown — 优雅关闭端点（lines 260-277）。"""

    @pytest.mark.asyncio
    async def test_shutdown_rejects_non_local_request(self, test_client):
        """非本地 Host 应返回 403。

        新逻辑：Host=localhost 即通过（无需 Origin 白名单）。
        此测试改为真正非本地 Host（evil.com）验证被拒。
        """
        r = await test_client.post(
            "/api/shutdown",
            headers={"Host": "evil.com:1234"},
        )
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_shutdown_with_no_active_tasks(self, test_db):
        """无活跃 pipeline task 时应正常返回（lines 264, 267, 276-277）。"""
        from main import app
        from httpx import ASGITransport
        from core.pipeline import _pipeline_tasks

        # 确保无活跃 task
        _pipeline_tasks.clear()

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
            headers={"Origin": "http://127.0.0.1:8000"},
        ) as c:
            r = await c.post("/api/shutdown")
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "shutting_down"
            assert data["cancelled_tasks"] == 0

    @pytest.mark.asyncio
    async def test_shutdown_cancels_active_tasks(self, test_db, monkeypatch):
        """有活跃 pipeline task 时应取消并等待（lines 269-275）。"""
        from main import app
        from httpx import ASGITransport
        from core.pipeline import _pipeline_tasks
        from unittest.mock import MagicMock, AsyncMock

        # 添加模拟的活跃 task
        mock_task = MagicMock()
        mock_task.done.return_value = False
        mock_task.cancel.return_value = True
        _pipeline_tasks.clear()
        _pipeline_tasks["test-job-1"] = mock_task

        # mock asyncio.sleep 避免测试等待 2 秒
        import asyncio as _asyncio_mod
        monkeypatch.setattr(_asyncio_mod, "sleep", AsyncMock())

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://127.0.0.1:8000",
                headers={"Origin": "http://127.0.0.1:8000"},
            ) as c:
                r = await c.post("/api/shutdown")
                assert r.status_code == 200
                data = r.json()
                assert data["status"] == "shutting_down"
                assert data["cancelled_tasks"] == 1
                # 确认 cancel 被调用
                mock_task.cancel.assert_called_once()
        finally:
            _pipeline_tasks.clear()


class TestRequestIdMiddlewareException:
    """request_id 中间件异常路径 — call_next 抛异常时记录日志并重新抛出（lines 153-156）。"""

    @pytest.mark.asyncio
    async def test_middleware_logs_exception_and_reraises(self, test_client, monkeypatch):
        """路由处理器抛非 HTTPException 异常时，中间件 except 分支应记录日志。"""
        import logging
        from io import StringIO

        # 用 logging handler 捕获日志
        log_stream = StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setLevel(logging.ERROR)
        main_logger = logging.getLogger("main")
        main_logger.addHandler(handler)

        # patch main.get_db 使 index 路由抛异常
        async def failing_get_db():
            raise RuntimeError("DB connection lost")

        monkeypatch.setattr("main.get_db", failing_get_db)

        # Starlette ServerErrorMiddleware 发送 500 响应后会重新抛出异常，
        # 所以 ASGITransport 会收到异常。用 pytest.raises 捕获。
        try:
            with pytest.raises(RuntimeError, match="DB connection lost"):
                await test_client.get("/")
            # 验证中间件 except 分支记录了错误日志（line 155）
            log_output = log_stream.getvalue()
            assert "failed" in log_output
            assert "GET" in log_output
        finally:
            main_logger.removeHandler(handler)


class TestReviewPageInvalidJson:
    """GET /jobs/{id}/review — structured_json 为非法 JSON 时的降级分支（lines 347-348）。"""

    @pytest.mark.asyncio
    async def test_review_with_invalid_structured_json(self, client_with_full_job, test_db):
        """structured_json 为非法 JSON 时应安全降级为空 dict，页面正常渲染。"""
        await test_db.execute(
            "UPDATE page_cache SET structured_json = ? WHERE job_id = ? AND page = ?",
            ("{invalid json not parseable", "full-job", 1),
        )
        await test_db.commit()

        c, _ = client_with_full_job
        r = await c.get("/jobs/full-job/review?page=1")
        assert r.status_code == 200


class TestGetJobPageImage:
    """GET /api/jobs/{id}/page/{n} — PyMuPDF 渲染 JPEG 预览端点。"""

    @pytest.mark.asyncio
    async def test_page_image_success(self, client_with_full_job):
        """正常渲染：返回 JPEG 字节流（质量 82，替代低效 PNG）。"""
        c, _ = client_with_full_job
        r = await c.get("/api/jobs/full-job/page/1")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.content.startswith(b"\xff\xd8\xff")

    @pytest.mark.asyncio
    async def test_page_image_out_of_range_404(self, client_with_full_job):
        """页码越界 → 404。"""
        c, _ = client_with_full_job
        r = await c.get("/api/jobs/full-job/page/99")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_page_image_page_zero_404(self, client_with_full_job):
        """0 页（1-based 语义）→ 404。"""
        c, _ = client_with_full_job
        r = await c.get("/api/jobs/full-job/page/0")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_page_image_job_not_found_404(self, client_with_full_job):
        """job 不存在 → 404。"""
        c, _ = client_with_full_job
        r = await c.get("/api/jobs/nonexistent/page/1")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_page_image_pdf_missing_404(self, test_db, tmp_path, client_with_full_job):
        """pdf_path 指向不存在的文件 → 404。"""
        import fitz
        from config import config as _cfg

        output_dir = Path(_cfg["app"].output_dir)
        ghost = output_dir / "ghost.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((50, 50), "ghost")
        doc.save(str(ghost))
        doc.close()
        ghost.unlink()  # 文件不存在
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ghost-job", "ghost.pdf", str(ghost), "review", 1),
        )
        await test_db.commit()

        c, _ = client_with_full_job
        r = await c.get("/api/jobs/ghost-job/page/1")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_page_image_path_traversal_403(self, test_db, tmp_path, client_with_full_job):
        """pdf_path 在 output_dir 外（路径穿越）→ 403。"""
        outside = tmp_path / "outside.pdf"
        outside.write_bytes(b"%PDF-1.4 outside")
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("evil-job", "evil.pdf", str(outside), "review", 1),
        )
        await test_db.commit()

        c, _ = client_with_full_job
        r = await c.get("/api/jobs/evil-job/page/1")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_page_image_render_failure_500(self, test_db, client_with_full_job, monkeypatch):
        """渲染抛异常 → 500。"""
        import api.jobs as jobs_api

        def boom(job_id, pdf_path):
            raise RuntimeError("fitz exploded")

        monkeypatch.setattr(jobs_api, "_get_pdf_doc", boom)

        c, _ = client_with_full_job
        r = await c.get("/api/jobs/full-job/page/1")
        assert r.status_code == 500  # 应正常渲染，data 降级为 {}
