"""Report API 集成测试 — Markdown + JSON 报告生成。

覆盖：
- GET /api/jobs/{id}/report.md（Markdown 报告）
- GET /api/jobs/{id}/report.json（结构化 JSON 报告）
- 404 for nonexistent job
- 报告内容包含 filename / findings count / severity breakdown
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient


@pytest_asyncio.fixture
async def report_client(test_db):
    """提供带 findings 数据的报表测试客户端。"""
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        ("report-job", "test.pdf", "/tmp/test.pdf", "review", 2),
    )
    await test_db.executemany(
        "INSERT INTO findings (job_id, page, type, severity, source, description, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("report-job", 1, "参数越界", "critical", "rule", "温度超出", "pending"),
            ("report-job", 1, "时间逻辑", "warning", "llm_page", "工序倒序", "confirmed"),
            ("report-job", 2, "完整性", "info", "rule", "缺少签名", "rejected"),
        ],
    )
    await test_db.commit()
    from main import app
    from httpx import ASGITransport
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


class TestReportMarkdown:
    """GET /api/jobs/{id}/report.md。"""

    @pytest.mark.asyncio
    async def test_report_md_returns_200(self, report_client):
        r = await report_client.get("/api/jobs/report-job/report.md")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_report_md_content_type(self, report_client):
        """Markdown 报告应返回 text/markdown 媒体类型。"""
        r = await report_client.get("/api/jobs/report-job/report.md")
        assert "text/markdown" in r.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_report_md_contains_filename(self, report_client):
        """报告应包含 job 的文件名。"""
        r = await report_client.get("/api/jobs/report-job/report.md")
        assert "test.pdf" in r.text

    @pytest.mark.asyncio
    async def test_report_md_contains_findings_count(self, report_client):
        """报告应包含 findings 总数。"""
        r = await report_client.get("/api/jobs/report-job/report.md")
        assert "**总 Findings**: 3" in r.text

    @pytest.mark.asyncio
    async def test_report_md_contains_severity_breakdown(self, report_client):
        """报告应按严重度分组并显示每组的数量。"""
        r = await report_client.get("/api/jobs/report-job/report.md")
        text = r.text
        # 三种严重度各 1 条
        assert "🔴 CRITICAL (1)" in text
        assert "🟡 WARNING (1)" in text
        assert "🔵 INFO (1)" in text

    @pytest.mark.asyncio
    async def test_report_md_contains_job_id(self, report_client):
        """报告应包含 Job ID。"""
        r = await report_client.get("/api/jobs/report-job/report.md")
        assert "report-job" in r.text

    @pytest.mark.asyncio
    async def test_report_md_contains_finding_descriptions(self, report_client):
        """报告应包含 finding 描述。"""
        r = await report_client.get("/api/jobs/report-job/report.md")
        text = r.text
        assert "温度超出" in text
        assert "工序倒序" in text
        assert "缺少签名" in text

    @pytest.mark.asyncio
    async def test_report_md_nonexistent_job_returns_404(self, report_client):
        r = await report_client.get("/api/jobs/nonexistent-id/report.md")
        assert r.status_code == 404


class TestReportJson:
    """GET /api/jobs/{id}/report.json。"""

    @pytest.mark.asyncio
    async def test_report_json_returns_200(self, report_client):
        r = await report_client.get("/api/jobs/report-job/report.json")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_report_json_has_job_metadata(self, report_client):
        """JSON 报告应包含 job 元数据。"""
        r = await report_client.get("/api/jobs/report-job/report.json")
        data = r.json()
        assert "job" in data
        assert data["job"]["id"] == "report-job"
        assert data["job"]["filename"] == "test.pdf"
        assert data["job"]["status"] == "review"
        assert data["job"]["total_pages"] == 2

    @pytest.mark.asyncio
    async def test_report_json_findings_count(self, report_client):
        """JSON 报告应返回正确的 findings 数量。"""
        r = await report_client.get("/api/jobs/report-job/report.json")
        data = r.json()
        assert data["count"] == 3
        assert len(data["findings"]) == 3

    @pytest.mark.asyncio
    async def test_report_json_severity_breakdown(self, report_client):
        """JSON findings 应覆盖三种严重度。"""
        r = await report_client.get("/api/jobs/report-job/report.json")
        findings = r.json()["findings"]
        severities = [f["severity"] for f in findings]
        assert severities.count("critical") == 1
        assert severities.count("warning") == 1
        assert severities.count("info") == 1

    @pytest.mark.asyncio
    async def test_report_json_findings_sorted_by_severity(self, report_client):
        """findings 应按严重度排序（critical 优先）。"""
        r = await report_client.get("/api/jobs/report-job/report.json")
        findings = r.json()["findings"]
        assert findings[0]["severity"] == "critical"
        assert findings[1]["severity"] == "warning"
        assert findings[2]["severity"] == "info"

    @pytest.mark.asyncio
    async def test_report_json_nonexistent_job_returns_404(self, report_client):
        r = await report_client.get("/api/jobs/nonexistent-id/report.json")
        assert r.status_code == 404
