"""Review API 集成测试 — 未覆盖端点补全。

补全 test_api_review.py 未覆盖的代码路径：
- GET  /api/jobs/{id}/audit                       (审计日志)
- GET  /api/jobs/{id}/pages/{page}                (raw_html + structured)
- GET  /api/jobs/{id}/pages/{page}/measurements   (测量矩阵)
- POST /api/jobs/{id}/findings/{fid}              (reviewer_note / corrected_text 单独更新)
- POST /api/jobs/{id}/findings/{fid}              (no fields → 400)
- POST /api/jobs/{id}/findings/{fid}              (finding 不存在 → 404)
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


@pytest_asyncio.fixture
async def review_client(test_db):
    """带 findings + page_cache 数据的客户端。"""
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        ("cov-job", "test.pdf", "/tmp/test.pdf", "review", 2),
    )
    await test_db.executemany(
        "INSERT INTO findings (job_id, page, type, severity, source, description, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("cov-job", 1, "参数越界", "critical", "rule", "温度超出", "pending"),
            ("cov-job", 2, "时间逻辑", "warning", "llm_page", "工序倒序", "pending"),
        ],
    )
    # 插入 page_cache 含 measurements 用于测试 measurements 端点
    await test_db.execute(
        "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
        "VALUES (?, ?, ?, ?)",
        (
            "cov-job",
            1,
            "<p>OCR text page 1</p>",
            '{"steps":[{"step_no":1,'
            '"measurements":[{"name":"温度","time":"10:00","values":{"A":"25.5","B":"26.0"}}]}],'
            '"overall_confidence":"high"}',
        ),
    )
    await test_db.execute(
        "INSERT INTO audit_log (job_id, action, detail) VALUES (?, ?, ?)",
        ("cov-job", "pipeline_start", "uploaded test.pdf"),
    )
    await test_db.execute(
        "INSERT INTO audit_log (job_id, action, detail) VALUES (?, ?, ?)",
        ("cov-job", "status_transition", "pending → ocr_running"),
    )
    await test_db.commit()

    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as c:
        yield c


class TestAuditLog:
    """GET /api/jobs/{id}/audit — 审计日志端点。"""

    @pytest.mark.asyncio
    async def test_audit_returns_entries(self, review_client):
        r = await review_client.get("/api/jobs/cov-job/audit")
        assert r.status_code == 200
        data = r.json()
        assert "entries" in data
        assert "count" in data
        assert data["count"] >= 2
        actions = [e["action"] for e in data["entries"]]
        assert "pipeline_start" in actions
        assert "status_transition" in actions

    @pytest.mark.asyncio
    async def test_audit_with_limit_param(self, review_client):
        """limit 参数应限制返回条数。"""
        r = await review_client.get("/api/jobs/cov-job/audit?limit=1")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 1

    @pytest.mark.asyncio
    async def test_audit_nonexistent_job_returns_empty(self, review_client):
        """不存在的 job 应返回空列表而非 404。"""
        r = await review_client.get("/api/jobs/no-such-job/audit")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 0
        assert data["entries"] == []


class TestGetPageEndpoint:
    """GET /api/jobs/{id}/pages/{page} — raw_html + structured 数据。"""

    @pytest.mark.asyncio
    async def test_get_page_returns_structured(self, review_client):
        r = await review_client.get("/api/jobs/cov-job/pages/1")
        assert r.status_code == 200
        data = r.json()
        assert data["job_id"] == "cov-job"
        assert data["page"] == 1
        assert "raw_html" in data
        assert data["structured"] is not None
        assert "steps" in data["structured"]

    @pytest.mark.asyncio
    async def test_get_page_without_structured_json(self, review_client, test_db):
        """structured_json 为 NULL 时 structured 字段应为 None。"""
        await test_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html) VALUES (?, ?, ?)",
            ("cov-job", 2, "<p>page 2 only html</p>"),
        )
        await test_db.commit()
        r = await review_client.get("/api/jobs/cov-job/pages/2")
        assert r.status_code == 200
        data = r.json()
        assert data["structured"] is None
        assert data["raw_html"] == "<p>page 2 only html</p>"

    @pytest.mark.asyncio
    async def test_get_page_nonexistent_returns_404(self, review_client):
        r = await review_client.get("/api/jobs/cov-job/pages/999")
        assert r.status_code == 404


class TestMeasurementsEndpoint:
    """GET /api/jobs/{id}/pages/{page}/measurements — 测量矩阵端点。"""

    @pytest.mark.asyncio
    async def test_measurements_returns_columns_and_data(self, review_client):
        r = await review_client.get("/api/jobs/cov-job/pages/1/measurements")
        assert r.status_code == 200
        data = r.json()
        assert data["page"] == 1
        assert "columns" in data
        assert "A" in data["columns"]
        assert "B" in data["columns"]
        assert data["count"] == 1
        m = data["measurements"][0]
        assert m["step_no"] == 1
        assert m["values"]["A"] == "25.5"
        assert m["values"]["B"] == "26.0"

    @pytest.mark.asyncio
    async def test_measurements_nonexistent_page_returns_404(self, review_client):
        r = await review_client.get("/api/jobs/cov-job/pages/999/measurements")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_measurements_empty_structured(self, review_client, test_db):
        """structured_json 为空对象时 measurements 应为空列表。"""
        await test_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
            "VALUES (?, ?, ?, ?)",
            ("cov-job", 3, "<p>empty</p>", '{"steps":[]}'),
        )
        await test_db.commit()
        r = await review_client.get("/api/jobs/cov-job/pages/3/measurements")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 0
        assert data["columns"] == []
        assert data["measurements"] == []

    @pytest.mark.asyncio
    async def test_measurements_null_structured(self, review_client, test_db):
        """structured_json 为 NULL 时应返回空 measurements。"""
        await test_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html) VALUES (?, ?, ?)",
            ("cov-job", 4, "<p>no json</p>"),
        )
        await test_db.commit()
        r = await review_client.get("/api/jobs/cov-job/pages/4/measurements")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 0


class TestUpdateFindingEdgeCases:
    """POST /api/jobs/{id}/findings/{fid} — 边界路径。"""

    @pytest.mark.asyncio
    async def test_update_with_reviewer_note_only(self, review_client):
        """仅 reviewer_note 字段更新（无 status）。"""
        r = await review_client.post(
            "/api/jobs/cov-job/findings/1",
            data={"reviewer_note": "looks valid"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_update_with_corrected_text_only(self, review_client):
        """仅 corrected_text 字段更新。"""
        r = await review_client.post(
            "/api/jobs/cov-job/findings/1",
            data={"corrected_text": "corrected value"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_update_no_fields_returns_400(self, review_client):
        """无任何字段更新应返回 400。"""
        r = await review_client.post("/api/jobs/cov-job/findings/1")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_update_nonexistent_finding_returns_404(self, review_client):
        """finding 不存在应返回 404。"""
        r = await review_client.post(
            "/api/jobs/cov-job/findings/999",
            data={"status": "confirmed"},
        )
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_update_writes_audit_log(self, review_client, test_db):
        """更新应写入 audit_log。"""
        await review_client.post(
            "/api/jobs/cov-job/findings/1",
            data={"status": "confirmed", "reviewer_note": "verified"},
        )
        cursor = await test_db.execute(
            "SELECT action FROM audit_log WHERE job_id = ? AND action = ?",
            ("cov-job", "finding_update"),
        )
        rows = await cursor.fetchall()
        assert len(rows) >= 1
