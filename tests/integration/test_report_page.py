# -*- coding: utf-8 -*-
"""round-61 报告查看页集成测试 —— /jobs/{id}/report（SSR）+ review 入口按钮。

背景：旧入口"下载报告"是裸链 `/api/jobs/{id}/report.md`（纯文本端点），
浏览器直接导航后脱离站点外壳（顶栏消失），只能靠浏览器后退。本轮改为
站内 SSR 报告页 + "← 返回复核"优雅返回。

本文件锁定四层契约：
1. 路由状态机：未知 job 404 / 非终态 303 回复核页 / 终态（review、
   partial_review、done）200。
2. 优雅返回：报告页顶栏必须带"← 返回复核"（指向 /jobs/{id}/review）、
   设置、首页 —— 用户永远有站内出路，不依赖浏览器后退。
3. 内容正确：md 经 md_render 渲染为站内 HTML（h1/strong/hr 在场、
   finding 文本可见）。
4. 入口替换完整性：review 页按钮改为"查看报告"指向站内页，且不再有
   report.md 裸链残留；.md API 端点本身保留（e2e/下游消费者契约）。
"""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

_JOB_ID = "rpt-job"


@pytest_asyncio.fixture
async def report_client(test_db):
    """种一个终态（review）job + 1 条 finding + 1 页 page_cache。"""
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        (_JOB_ID, "rpt-test.pdf", "/tmp/rpt-test.pdf", "review", 1),
    )
    await test_db.execute(
        "INSERT INTO page_cache (job_id, page, raw_html) VALUES (?, ?, ?)",
        (_JOB_ID, 1, "<p>温度 42.1</p>"),
    )
    await test_db.execute(
        "INSERT INTO findings (job_id, page, type, severity, source, "
        "description, ocr_text, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
        (_JOB_ID, 1, "param_out_of_spec", "critical", "rule",
         "温度 42.1 超出规格范围", "温度 42.1"),
    )
    await test_db.commit()

    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        yield client


async def _set_status(test_db, status: str):
    await test_db.execute(
        "UPDATE jobs SET status = ? WHERE id = ?", (status, _JOB_ID)
    )
    await test_db.commit()


class TestRouteStateMachine:
    @pytest.mark.asyncio
    async def test_unknown_job_404(self, report_client):
        r = await report_client.get("/jobs/no-such/report")
        assert r.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [
        "pending", "ocr_running", "analyzing", "error", "cancelled",
    ])
    async def test_non_terminal_redirects_to_review(
        self, report_client, test_db, status
    ):
        """非终态没有可看的报告 —— 303 回复核页（看进度/失败原因）。"""
        await _set_status(test_db, status)
        r = await report_client.get(f"/jobs/{_JOB_ID}/report")
        assert r.status_code == 303
        assert r.headers["location"] == f"/jobs/{_JOB_ID}/review"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["review", "partial_review", "done"])
    async def test_terminal_statuses_render(
        self, report_client, test_db, status
    ):
        await _set_status(test_db, status)
        r = await report_client.get(f"/jobs/{_JOB_ID}/report")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]


class TestGracefulBack:
    @pytest.mark.asyncio
    async def test_back_to_review_button_present(self, report_client):
        """优雅返回核心：站内"← 返回复核"按钮必须存在且指向复核页。"""
        r = await report_client.get(f"/jobs/{_JOB_ID}/report")
        assert r.status_code == 200
        assert "← 返回复核" in r.text
        assert f'href="/jobs/{_JOB_ID}/review"' in r.text

    @pytest.mark.asyncio
    async def test_home_and_settings_always_reachable(self, report_client):
        r = await report_client.get(f"/jobs/{_JOB_ID}/report")
        assert 'href="/settings"' in r.text
        assert 'href="/"' in r.text

    @pytest.mark.asyncio
    async def test_header_keeps_job_identity(self, report_client):
        """顶栏保留 job 身份（文件名/job_id）—— 与 review 页外壳一致，
        用户返回前后上下文不丢。"""
        r = await report_client.get(f"/jobs/{_JOB_ID}/report")
        assert "rpt-test.pdf" in r.text
        assert _JOB_ID in r.text


class TestContent:
    @pytest.mark.asyncio
    async def test_markdown_rendered_as_html(self, report_client):
        """md → 站内 HTML：标题/粗体/hr 结构在场，正文可读。"""
        r = await report_client.get(f"/jobs/{_JOB_ID}/report")
        html = r.text
        assert "<h1>GMP 批生产记录合规检查报告</h1>" in html
        assert "<strong>文件名</strong>" in html
        assert "<hr>" in html
        assert "<h2>Findings 列表</h2>" in html

    @pytest.mark.asyncio
    async def test_finding_text_visible(self, report_client):
        r = await report_client.get(f"/jobs/{_JOB_ID}/report")
        assert "温度 42.1 超出规格范围" in r.text

    @pytest.mark.asyncio
    async def test_no_raw_markdown_leak(self, report_client):
        """渲染产物不得回退成裸 md 源文本（比如 ** 未被处理成 strong）。"""
        r = await report_client.get(f"/jobs/{_JOB_ID}/report")
        assert "**文件名**" not in r.text


class TestReviewPageEntry:
    """review 页入口替换完整性 —— 按钮必须指向站内报告页。"""

    @pytest.mark.asyncio
    async def test_review_button_is_view_report(self, report_client):
        r = await report_client.get(f"/jobs/{_JOB_ID}/review?page=1")
        assert r.status_code == 200
        assert "查看报告" in r.text
        assert f'href="/jobs/{_JOB_ID}/report"' in r.text

    @pytest.mark.asyncio
    async def test_review_page_has_no_raw_md_link(self, report_client):
        """旧裸链必须清除 —— 否则用户仍可能点进脱离外壳的纯文本页。"""
        r = await report_client.get(f"/jobs/{_JOB_ID}/review?page=1")
        assert "下载报告" not in r.text
        assert "report.md" not in r.text

    @pytest.mark.asyncio
    async def test_review_page_hides_button_for_non_terminal(
        self, report_client, test_db
    ):
        """非终态不显示查看报告入口（与旧按钮的状态门一致）。"""
        await _set_status(test_db, "ocr_running")
        r = await report_client.get(f"/jobs/{_JOB_ID}/review?page=1")
        assert "查看报告" not in r.text


class TestMdEndpointContract:
    @pytest.mark.asyncio
    async def test_md_endpoint_still_serves_markdown(self, report_client):
        """.md API 端点保留：e2e / 下游消费者的既有契约，本轮只改前端入口。"""
        r = await report_client.get(f"/api/jobs/{_JOB_ID}/report.md")
        assert r.status_code == 200
        assert "text/markdown" in r.headers["content-type"]
        assert r.text.startswith("# GMP")
