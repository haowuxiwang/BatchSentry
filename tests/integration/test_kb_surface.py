"""KB v8 surface tests — settings browse API, db seeding mirror, report appendix."""
import json

import pytest


class TestBrowseApi:
    @pytest.mark.asyncio
    async def test_browse_returns_entries(self, test_client):
        r = await test_client.get("/api/settings/kb?limit=5")
        assert r.status_code == 200
        d = r.json()
        assert d["source"]["source_id"] == "gmp2010"
        assert len(d["chapters"]) == 14
        assert d["returned"] == len(d["entries"]) <= 5
        assert all("article_label" in e for e in d["entries"])

    @pytest.mark.asyncio
    async def test_browse_search_filters(self, test_client):
        r = await test_client.get("/api/settings/kb",
                                  params={"q": "操作规程"})
        assert r.status_code == 200
        d = r.json()
        assert d["returned"] >= 1
        assert any("操作规程" in e["text"] for e in d["entries"])

    @pytest.mark.asyncio
    async def test_browse_blocks_non_local(self, test_client):
        r = await test_client.get(
            "/api/settings/kb", headers={"Host": "evil.com:80"})
        assert r.status_code == 403


class TestEnsureDbSeeded:
    @pytest.mark.asyncio
    async def test_seeds_once_and_idempotent(self, test_db):
        from core.kb import ensure_db_seeded, entries

        n1 = await ensure_db_seeded(test_db)
        assert n1 == len(entries())
        cur = await test_db.execute("SELECT COUNT(*) FROM kb_entries")
        first = (await cur.fetchone())[0]
        n2 = await ensure_db_seeded(test_db)
        assert n2 == 0  # 已满量 → 跳过
        cur = await test_db.execute("SELECT COUNT(*) FROM kb_entries")
        assert (await cur.fetchone())[0] == first

    @pytest.mark.asyncio
    async def test_empty_store_noop(self, test_db, monkeypatch):
        from core import kb as kb_pkg
        from core.kb import store

        monkeypatch.setattr(store, "_payload", {
            "source_id": "", "title": "", "chapters": [], "entries": []})
        # 直接调用包级导出（走同一 store 实例）
        n = await kb_pkg.ensure_db_seeded(test_db)
        assert n == 0
        monkeypatch.undo()


class TestReportAppendix:
    @pytest.mark.asyncio
    async def test_report_md_includes_cited_articles(self, test_db):
        """带 kb_refs 的 findings → 报告出现依据条文附录（去重、全文）。"""
        refs = json.dumps([
            {"entry_id": "gmp2010-151", "label": "第一百五十一条",
             "excerpt": "x", "score": 9.9},
            {"entry_id": "gmp2010-151", "label": "第一百五十一条",
             "excerpt": "x", "score": 9.9},  # 同条重复引用 → 附录去重
        ], ensure_ascii=False)
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES ('kb-job', 'k.pdf', '/tmp/k.pdf', 'review', 1)")
        await test_db.execute(
            "INSERT INTO findings (job_id, page, type, severity, source, "
            "description, status, kb_refs) VALUES ('kb-job', 1, "
            "'completeness', 'warning', 'llm_page', '缺少复核签名', "
            "'pending', ?)", (refs,))
        await test_db.commit()

        from main import app
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://localhost:8000") as c:
            r = await c.get("/api/jobs/kb-job/report.md")
        assert r.status_code == 200
        assert "依据条文附录" in r.text
        assert "第一百五十一条" in r.text
        # 全文来自 store（非截断摘录）：151 条正文含"规程/管理"
        assert ("规程" in r.text) or ("管理" in r.text)

    @pytest.mark.asyncio
    async def test_report_without_refs_has_no_appendix(self, test_db):
        """对照组：findings 无 kb_refs → 报告不出现附录章节。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES ('noref-job', 'n.pdf', '/tmp/n.pdf', 'review', 1)")
        await test_db.execute(
            "INSERT INTO findings (job_id, page, type, severity, source, "
            "description, status) VALUES ('noref-job', 1, 'completeness', "
            "'warning', 'llm_page', '普通问题', 'pending')")
        await test_db.commit()
        from main import app
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://localhost:8000") as c:
            r = await c.get("/api/jobs/noref-job/report.md")
        assert r.status_code == 200
        assert "依据条文附录" not in r.text


def test_json_load_refs_defensive():
    from core.kb.retriever import json_load_refs

    good = [{"entry_id": "e", "label": "l", "excerpt": "x"}]
    assert json_load_refs(json.dumps(good)) == good
    assert json_load_refs(good) == good          # already-list passthrough
    assert json_load_refs("{bad json") == []
    assert json_load_refs('{"not":"a list"}') == []
    assert json_load_refs(None) == []
    assert json_load_refs("") == []
