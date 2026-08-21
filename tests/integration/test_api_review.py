"""Review API 集成测试 — findings CRUD。

覆盖：
- GET /api/jobs/{id}/findings（含 page 和 status 过滤）
- GET /api/jobs/{id}/findings/{fid}
- POST /api/jobs/{id}/findings/{fid}（confirm/reject/correct）
- GET /api/jobs/{id}/audit
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient


@pytest_asyncio.fixture
async def review_client(test_db):
    """提供带 findings 数据的客户端。"""
    # 插入 job
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        ("review-job", "test.pdf", "/tmp/test.pdf", "review", 3),
    )
    # 插入 findings
    await test_db.executemany(
        "INSERT INTO findings (job_id, page, type, severity, source, description, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("review-job", 1, "参数越界", "critical", "rule", "温度超出", "pending"),
            ("review-job", 1, "时间逻辑", "warning", "llm_page", "工序倒序", "pending"),
            ("review-job", 2, "完整性", "info", "rule", "缺少签名", "pending"),
            ("review-job", 2, "参数越界", "critical", "rule", "pH偏低", "confirmed"),
        ],
    )
    await test_db.commit()

    from main import app
    from httpx import ASGITransport
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:8000") as client:
        yield client


class TestListFindings:
    """GET /api/jobs/{id}/findings。"""

    @pytest.mark.asyncio
    async def test_list_all_findings(self, review_client):
        r = await review_client.get("/api/jobs/review-job/findings")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 4

    @pytest.mark.asyncio
    async def test_list_findings_by_page(self, review_client):
        r = await review_client.get("/api/jobs/review-job/findings?page=1")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 2
        assert all(f["page"] == 1 for f in data["findings"])

    @pytest.mark.asyncio
    async def test_list_findings_by_status(self, review_client):
        r = await review_client.get("/api/jobs/review-job/findings?status=confirmed")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 1
        assert data["findings"][0]["status"] == "confirmed"

    @pytest.mark.asyncio
    async def test_findings_sorted_by_severity(self, review_client):
        """findings 应按严重度排序（critical 优先）。"""
        r = await review_client.get("/api/jobs/review-job/findings?page=1")
        data = r.json()
        if len(data["findings"]) >= 2:
            # 第一个应是 critical
            assert data["findings"][0]["severity"] == "critical"


class TestConfidenceScoring:
    """#8 字段级置信度（读时计算）。"""

    @pytest.mark.asyncio
    async def test_confidence_present_and_ranks_sources(self, review_client):
        r = await review_client.get("/api/jobs/review-job/findings")
        data = r.json()
        by_src = {f["source"]: f["confidence"] for f in data["findings"]}
        # 规则层基准 0.85；llm_page 扣 0.10 → 0.75
        assert by_src["rule"] == 0.85
        assert by_src["llm_page"] == 0.75

    @pytest.mark.asyncio
    async def test_page_flag_lowers_confidence(self, review_client, test_db):
        # 页 1 加 OCR 警告标记 → 该页 findings 置信度下降
        import json as _json
        await test_db.execute(
            "INSERT OR REPLACE INTO page_cache (job_id, page, raw_html, structured_json) "
            "VALUES (?, 1, 'p1', ?)",
            ("review-job", _json.dumps({"_ocr_warning": "3 个内容块被丢弃"})),
        )
        await test_db.commit()
        r = await review_client.get("/api/jobs/review-job/findings?page=1")
        data = r.json()
        confs = {f["source"]: f["confidence"] for f in data["findings"]}
        assert confs["rule"] == 0.65   # 0.85 - 0.20 页面标记
        assert confs["llm_page"] == 0.55

    @pytest.mark.asyncio
    async def test_order_by_confidence_ascending(self, review_client):
        r = await review_client.get(
            "/api/jobs/review-job/findings?order=confidence"
        )
        data = r.json()
        confs = [f["confidence"] for f in data["findings"]]
        assert confs == sorted(confs)
        assert confs[0] == min(confs)

    @pytest.mark.asyncio
    async def test_has_more_scoped_to_current_filter(self, review_client):
        """对抗审查：has_more 必须按当前过滤集统计，不得被全局总数误触发。

        旧实现 total 只按 job_id 统计全局（60 条），page 过滤后仅返回
        当前页数据，但 (0+50) < 60 恒成立 → 每个页面都显示"本页问题
        超过 50 条"，与实际条数完全不符（GMP 复核误导，用户实况：
        页 6 仅 1 条却提示超过 50 条）。
        """
        # 追加 55 条到 page 1（fixture 已含 2 条 → 共 57 条），page 2 保持 2 条
        await review_client.get("/api/jobs/review-job/findings")
        # 直接写库造数据
        from db.client import get_db
        db = await get_db()
        await db.executemany(
            "INSERT INTO findings (job_id, page, type, severity, source, description, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("review-job", 1, "参数越界", "warning", "rule", f"批量投料记录 {i}", "pending")
                for i in range(55)
            ],
        )
        await db.commit()

        # 当前页（page=2）只有 2 条 → has_more 必须是 False
        r = await review_client.get("/api/jobs/review-job/findings?page=2")
        data = r.json()
        assert data["count"] == 2
        assert data["total"] == 2   # total 是当前过滤集总数，不是全局
        assert data["has_more"] is False

        # page=1 有 57 条（>50 limit）→ has_more 为 True
        r2 = await review_client.get("/api/jobs/review-job/findings?page=1")
        data2 = r2.json()
        assert data2["count"] == 50   # limit 默认 50
        assert data2["total"] == 57
        assert data2["has_more"] is True


class TestGetFinding:
    """GET /api/jobs/{id}/findings/{fid}。"""

    @pytest.mark.asyncio
    async def test_get_finding_by_id(self, review_client):
        r = await review_client.get("/api/jobs/review-job/findings/1")
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "参数越界"

    @pytest.mark.asyncio
    async def test_get_nonexistent_finding_404(self, review_client):
        r = await review_client.get("/api/jobs/review-job/findings/999")
        assert r.status_code == 404


class TestUpdateFinding:
    """POST /api/jobs/{id}/findings/{fid}。"""

    @pytest.mark.asyncio
    async def test_confirm_finding(self, review_client):
        r = await review_client.post(
            "/api/jobs/review-job/findings/1",
            data={"status": "confirmed"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_reject_finding(self, review_client):
        r = await review_client.post(
            "/api/jobs/review-job/findings/2",
            data={"status": "rejected"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_correct_finding(self, review_client):
        r = await review_client.post(
            "/api/jobs/review-job/findings/3",
            data={"status": "corrected", "corrected_text": "新值"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_invalid_action_returns_400(self, review_client):
        r = await review_client.post(
            "/api/jobs/review-job/findings/1",
            data={"status": "invalid"},
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_update_rejected_from_non_local_origin(self, test_db):
        """对抗审查（cr-18）：恶意 Origin 的 finding 更新必须 403 —— Form 编码是
        CORS 简单请求，此前无守卫，跨站可篡改 GMP 审计数据。"""
        from main import app
        from httpx import AsyncClient, ASGITransport
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("evil-job", "test.pdf", "/tmp/test.pdf", "review", 1),
        )
        await test_db.execute(
            "INSERT INTO findings (job_id, page, type, severity, source, description, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("evil-job", 1, "参数越界", "critical", "rule", "温度超出", "pending"),
        )
        await test_db.commit()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.com:8000"
        ) as c:
            r = await c.post("/api/jobs/evil-job/findings/1", data={"status": "confirmed"})
        assert r.status_code == 403
        assert "non-local" in r.text


class TestLlmAuditLog:
    """GET /api/jobs/{id}/llm_audit — Phase 7 GMP 追溯 + P2-1 守卫。"""

    @pytest.mark.asyncio
    async def test_llm_audit_log_returns_entries(self, review_client, test_db):
        """插入 audit 行后应返回全部记录（含 provider/model/tokens）。"""
        await test_db.execute(
            "INSERT INTO llm_call_audit (job_id, page, stage, provider, protocol, model, "
            "prompt_version, prompt_tokens, completion_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("review-job", 1, "page_analysis", "deepseek", "openai", "deepseek-chat",
             "v3", 120, 80),
        )
        await test_db.commit()
        r = await review_client.get("/api/jobs/review-job/llm_audit")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] >= 1
        assert data["entries"][0]["model"] == "deepseek-chat"

    @pytest.mark.asyncio
    async def test_llm_audit_log_empty(self, review_client):
        """无 audit 行 → 200 + 空列表（不 500）。"""
        r = await review_client.get("/api/jobs/review-job/llm_audit")
        assert r.status_code == 200
        assert r.json()["count"] == 0

    @pytest.mark.asyncio
    async def test_llm_audit_log_guard(self, test_db):
        """P2-1：非本地 Host 读端点 → 403。"""
        from main import app
        from httpx import AsyncClient, ASGITransport
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("audit-job", "test.pdf", "/tmp/test.pdf", "review", 1),
        )
        await test_db.commit()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.com:8000"
        ) as c:
            r = await c.get("/api/jobs/audit-job/llm_audit")
        assert r.status_code == 403


class TestPageExemption:
    """POST /api/jobs/{id}/pages/{page}/exemption — 门禁 2 人工豁免。"""

    @pytest_asyncio.fixture
    async def exempt_client(self, test_db):
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("exempt-job", "test.pdf", "/tmp/test.pdf", "review", 3),
        )
        await test_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html, ocr_diagnostics) "
            "VALUES (?, ?, ?, ?)",
            ("exempt-job", 1, "<p>page1</p>",
             '{"source":"mineru","integrity":"incomplete","reasons":["检测到 OCR 缺失内容占位"]}'),
        )
        await test_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html, ocr_diagnostics) "
            "VALUES (?, ?, ?, ?)",
            ("exempt-job", 2, "<p>page2</p>", None),
        )
        await test_db.commit()
        from main import app
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost:8000"
        ) as c:
            yield c

    @pytest.mark.asyncio
    async def test_set_exemption(self, exempt_client):
        r = await exempt_client.post(
            "/api/jobs/exempt-job/pages/1/exemption",
            data={"reason": "人工核对原图，内容可接受"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["exemption"]["reason"] == "人工核对原图，内容可接受"
        assert "created_at" in data["exemption"]

    @pytest.mark.asyncio
    async def test_revoke_exemption(self, exempt_client):
        await exempt_client.post(
            "/api/jobs/exempt-job/pages/1/exemption",
            data={"reason": "已核对"},
        )
        r = await exempt_client.post(
            "/api/jobs/exempt-job/pages/1/exemption",
            data={"revoke": "1"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["exemption"] is None

    @pytest.mark.asyncio
    async def test_revoke_without_exemption_fails(self, exempt_client):
        r = await exempt_client.post(
            "/api/jobs/exempt-job/pages/2/exemption",
            data={"revoke": "1"},
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_empty_reason_fails(self, exempt_client):
        r = await exempt_client.post(
            "/api/jobs/exempt-job/pages/1/exemption",
            data={"reason": ""},
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_exemption_written_to_audit_log(self, exempt_client):
        await exempt_client.post(
            "/api/jobs/exempt-job/pages/1/exemption",
            data={"reason": "GMP 豁免原因"},
        )
        r = await exempt_client.get("/api/jobs/exempt-job/audit")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert any(e["action"] == "ocr_exemption" for e in entries)

    @pytest.mark.asyncio
    async def test_exemption_page_not_found(self, exempt_client):
        r = await exempt_client.post(
            "/api/jobs/exempt-job/pages/99/exemption",
            data={"reason": "test"},
        )
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_exemption_guard(self, test_db):
        from main import app
        from httpx import AsyncClient, ASGITransport
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES (?, ?, ?, ?, ?)",
            ("exempt-guard", "test.pdf", "/tmp/test.pdf", "review", 1),
        )
        await test_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html) VALUES (?, ?, ?)",
            ("exempt-guard", 1, "<p>x</p>"),
        )
        await test_db.commit()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.com:8000"
        ) as c:
            r = await c.post(
                "/api/jobs/exempt-guard/pages/1/exemption",
                data={"reason": "csrf"},
            )
        assert r.status_code == 403
