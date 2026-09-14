"""T6.4：三色复核分级的服务端表面（SSR 页面 + AJAX 端点）。

验收：视觉分级在**两条渲染路径**上都成立，且 tier 由服务端单一来源给出 —
SSR（GET /jobs/{id}/review）与 AJAX（GET /api/jobs/{id}/findings）不各持映射。
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def tier_client(test_db):
    """预置 job + 规则/LLM 两类 finding，返回 ASGI 客户端。"""
    from config import config as _cfg
    from pathlib import Path

    output_dir = Path(_cfg["app"].output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        ("tier-job", "t.pdf", str(output_dir / "t.pdf"), "review", 2),
    )
    rows = [
        # (page, type, severity, source, description)
        (1, "time_reversal", "warning", "rule", "页内时间倒序"),
        (1, "batch_inconsistency", "critical", "rule", "跨页批号不一致"),
        (1, "completeness", "info", "llm_page", "步骤签名缺失"),
        (1, "deviation_link", "warning", "llm_cross", "未关联偏差"),
        (2, "alteration", "warning", "rule", "涂改未划改签名"),
    ]
    for page, ftype, sev, src, desc in rows:
        await test_db.execute(
            "INSERT INTO findings (job_id, page, type, severity, source, "
            "description, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            ("tier-job", page, ftype, sev, src, desc),
        )
    await test_db.commit()

    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as c:
        yield c


class TestFindingsApiCarriesTier:
    @pytest.mark.asyncio
    async def test_each_finding_has_tier(self, tier_client):
        r = await tier_client.get("/api/jobs/tier-job/findings?page=1")
        assert r.status_code == 200
        d = r.json()
        assert d["findings"], "预置 finding 未返回"
        for f in d["findings"]:
            assert f["tier"] in ("rule", "llm"), f
        by_src = {f["source"]: f["tier"] for f in d["findings"]}
        assert by_src["rule"] == "rule"
        assert by_src["llm_page"] == "llm"
        assert by_src["llm_cross"] == "llm"

    @pytest.mark.asyncio
    async def test_tier_counts_are_page_scoped(self, tier_client):
        r = await tier_client.get("/api/jobs/tier-job/findings?page=1")
        d = r.json()
        # 第 1 页：2 条 rule + 2 条 llm
        assert d["tier_counts"] == {"rule": 2, "llm": 2}

        r2 = await tier_client.get("/api/jobs/tier-job/findings?page=2")
        d2 = r2.json()
        assert d2["tier_counts"] == {"rule": 1, "llm": 0}

    @pytest.mark.asyncio
    async def test_single_finding_detail_has_tier(self, tier_client):
        r = await tier_client.get("/api/jobs/tier-job/findings?page=2")
        fid = r.json()["findings"][0]["id"]
        r2 = await tier_client.get(f"/api/jobs/tier-job/findings/{fid}")
        assert r2.status_code == 200
        assert r2.json()["tier"] == "rule"


class TestReviewPageRendersTierPanel:
    @pytest.mark.asyncio
    async def test_tier_bar_and_coverage_panel_render(self, tier_client):
        r = await tier_client.get("/jobs/tier-job/review?page=1")
        assert r.status_code == 200
        html = r.text
        # 三色计数条
        assert 'id="tier-bar"' in html
        assert "规则命中" in html and "LLM 辅助" in html and "系统校验通过" in html
        # 绿色覆盖面面板（本批次口径）
        assert 'id="rule-coverage-panel"' in html
        assert "本批次已通过的系统校验项" in html

    @pytest.mark.asyncio
    async def test_card_carries_tier_class_and_attr(self, tier_client):
        r = await tier_client.get("/jobs/tier-job/review?page=1")
        html = r.text
        assert 'data-tier="rule"' in html
        assert 'data-tier="llm"' in html
        assert "finding-card tier-rule" in html
        assert "finding-card tier-llm" in html

    @pytest.mark.asyncio
    async def test_page_counts_match_rendered_findings(self, tier_client):
        """红/蓝计数必须等于本页实际 finding 数（页级口径）。"""
        r = await tier_client.get("/jobs/tier-job/review?page=1")
        html = r.text
        assert 'id="tier-count-rule">2<' in html
        assert 'id="tier-count-llm">2<' in html

    @pytest.mark.asyncio
    async def test_clean_page_zero_findings_but_green_intact(self, tier_client):
        """无 finding 的页：红/蓝计 0，绿色（全批次覆盖面）不受影响。

        这正是"绿"的意义 —— 没有问题的那一页，面板仍要告诉复核者系统查了
        什么、哪些通过，而不是只剩一个"本页无问题"的黑洞。
        注意口径差异：红/蓝是**本页**，绿是**本批次**。
        """
        r = await tier_client.get("/jobs/tier-job/review?page=3")
        assert r.status_code == 200
        html = r.text
        assert "本页无问题" in html
        assert 'id="tier-count-rule">0<' in html
        assert 'id="tier-count-llm">0<' in html

        # 本批次共 5 类命中（见 fixture）→ 绿 = 全部类型 − 5
        from core.rules.registry import rule_coverage

        job_counts = {"time_reversal": 1, "batch_inconsistency": 1,
                      "completeness": 1, "deviation_link": 1, "alteration": 1}
        cov = rule_coverage(job_counts)
        assert cov["fired_count"] == 5
        assert cov["passed_count"] == cov["type_total"] - 5 > 0
        assert f'id="tier-count-pass">{cov["passed_count"]}<' in html
        # 命中的类型不得出现在"已通过"清单里
        assert "物料平衡/收率" in html  # mass_balance 未命中 → 在绿色清单中
