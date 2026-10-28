"""M5 多源知识库的 API 面：设置页浏览/来源开关 + 报告按源分组附录。

覆盖 T5.6（后端部分）与 T5.5：
- GET /api/settings/kb 返回多源清单 + 组合版本；按 source 参数过滤
- PUT /api/settings/kb/sources 写入 config.json，且校验未知来源 / 不允许全禁
- 报告"依据条文附录"按来源分组，外文摘编标注「要点摘编」
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(test_db, tmp_path):
    """带隔离 config.json 的客户端（KB 来源开关会写 config）。"""
    from main import app
    import api.settings.rules as rules_mod

    cfg = tmp_path / "config.json"
    with patch.object(rules_mod, "_settings_config_path", return_value=cfg), \
         patch("config._config_path", return_value=cfg):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
            headers={"Origin": "http://127.0.0.1:8000"},
        ) as c:
            yield c


class TestBrowseMultiSource:
    @pytest.mark.asyncio
    async def test_lists_all_sources_with_versions(self, client):
        r = await client.get("/api/settings/kb?limit=200")
        assert r.status_code == 200
        d = r.json()
        assert len(d["sources"]) >= 6
        for s in d["sources"]:
            assert s["source_id"] and s["title"]
            assert len(s["version"]) == 12
            assert s["entries"] > 0
        assert len(d["kb_version"]) == 12
        # 默认浏览跨多个来源（不是只回主源）
        assert len({e["source_id"] for e in d["entries"]}) >= 3

    @pytest.mark.asyncio
    async def test_total_entries_matches_store(self, client):
        from core.kb import store

        d = (await client.get("/api/settings/kb?limit=1")).json()
        assert d["total_entries"] == len(store.entries())

    @pytest.mark.asyncio
    async def test_filter_by_source(self, client):
        d = (await client.get("/api/settings/kb",
                              params={"source": "alcoa_plus",
                                      "limit": 200})).json()
        assert d["active_source"] == "alcoa_plus"
        assert d["source"]["source_id"] == "alcoa_plus"
        assert d["entries"], "alcoa_plus 应返回条目"
        assert {e["source_id"] for e in d["entries"]} == {"alcoa_plus"}
        assert d["total_entries"] == len(d["entries"])

    @pytest.mark.asyncio
    async def test_unknown_source_404(self, client):
        r = await client.get("/api/settings/kb", params={"source": "nope"})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_search_scoped_to_source(self, client):
        d = (await client.get("/api/settings/kb",
                              params={"q": "记录", "source": "nmpa_di_2020",
                                      "limit": 5})).json()
        assert d["returned"] >= 1
        assert all("记录" in e["text"] for e in d["entries"])

    @pytest.mark.asyncio
    async def test_limit_is_clamped(self, client):
        assert (await client.get("/api/settings/kb?limit=99999")).json()[
            "returned"] <= 200
        assert (await client.get("/api/settings/kb?limit=0")).json()[
            "returned"] >= 1


class TestSourceSwitch:
    @pytest.mark.asyncio
    async def test_get_reports_disabled_sources_for_ui(self, client):
        """设置页要据此回填开关；默认未停用 → []。"""
        d = (await client.get("/api/settings/kb?limit=1")).json()
        assert d["disabled_sources"] == []
        await client.put("/api/settings/kb/sources",
                         json={"disabled": ["fda_21cfr_part11"]})
        d2 = (await client.get("/api/settings/kb?limit=1")).json()
        assert d2["disabled_sources"] == ["fda_21cfr_part11"]

    @pytest.mark.asyncio
    async def test_put_persists_to_config(self, client, tmp_path):
        from core.kb import store

        r = await client.put("/api/settings/kb/sources",
                             json={"disabled": ["fda_21cfr_part11"]})
        assert r.status_code == 200
        assert r.json()["disabled"] == ["fda_21cfr_part11"]
        import config as cfg_mod

        assert cfg_mod.load_disabled_kb_sources() == ["fda_21cfr_part11"]
        assert "fda_21cfr_part11" in store.source_ids()

    @pytest.mark.asyncio
    async def test_put_unknown_source_400(self, client):
        r = await client.put("/api/settings/kb/sources",
                             json={"disabled": ["does_not_exist"]})
        assert r.status_code == 400
        assert "未知来源" in json.dumps(r.json(), ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_put_cannot_disable_everything(self, client):
        from core.kb import store

        r = await client.put("/api/settings/kb/sources",
                             json={"disabled": store.source_ids()})
        assert r.status_code == 400
        assert "至少保留一个" in json.dumps(r.json(), ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_put_replaces_previous_list(self, client):
        await client.put("/api/settings/kb/sources",
                         json={"disabled": ["fda_21cfr_part11"]})
        await client.put("/api/settings/kb/sources",
                         json={"disabled": ["alcoa_plus"]})
        import config as cfg_mod

        assert cfg_mod.load_disabled_kb_sources() == ["alcoa_plus"]

    @pytest.mark.asyncio
    async def test_put_dedupes(self, client):
        r = await client.put("/api/settings/kb/sources",
                             json={"disabled": ["alcoa_plus", "alcoa_plus"]})
        assert r.status_code == 200
        assert r.json()["disabled"] == ["alcoa_plus"]

    @pytest.mark.asyncio
    async def test_put_blocks_non_local(self, test_db, tmp_path):
        from main import app
        import api.settings.rules as rules_mod

        cfg = tmp_path / "config.json"
        with patch.object(rules_mod, "_settings_config_path",
                          return_value=cfg):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://evil.com:8000",
            ) as c:
                r = await c.put("/api/settings/kb/sources",
                                json={"disabled": []})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_disabled_source_is_not_cited(self, client, monkeypatch):
        """端到端：关闭一个源后，该源不再出现在新生成的引用里。"""
        import config as cfg_mod
        from core.kb import retriever

        monkeypatch.setattr(cfg_mod, "load_disabled_kb_sources",
                            lambda: ["fda_21cfr_part11"])
        f = {"type": "alteration", "description": "电子记录被直接覆盖修改"}
        retriever.attach_kb_refs([f])
        assert f.get("kb_refs")
        assert all(r["source_id"] != "fda_21cfr_part11"
                   for r in f["kb_refs"])


class TestReportGroupedAppendix:
    @pytest.mark.asyncio
    async def test_appendix_groups_by_source_and_marks_digest(self, test_db):
        """跨源引用 → 附录按源分组；外文摘编条目标注「要点摘编」。"""
        refs = json.dumps([
            {"entry_id": "gmp2010-151", "label": "第一百五十一条",
             "excerpt": "x", "score": 9.9, "source_id": "gmp2010",
             "source_title": "药品生产质量管理规范（2010年修订）",
             "text_kind": "original"},
            {"entry_id": "alcoa_plus-002", "label": "可归因性 Attributable",
             "excerpt": "y", "score": 8.1, "source_id": "alcoa_plus",
             "source_title": "ALCOA+ 数据完整性原则",
             "text_kind": "digest"},
        ], ensure_ascii=False)
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES ('kb2', 'k.pdf', '/tmp/k.pdf', 'review', 1)")
        await test_db.execute(
            "INSERT INTO findings (job_id, page, type, severity, source, "
            "description, status, kb_refs) VALUES ('kb2', 1, 'alteration', "
            "'warning', 'llm_page', '记录被覆盖修改', 'pending', ?)", (refs,))
        await test_db.commit()

        from main import app
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://localhost:8000") as c:
            r = await c.get("/api/jobs/kb2/report.md")
        assert r.status_code == 200
        text = r.text
        assert "依据条文附录" in text
        # 两个来源各自成节（分组渲染）
        assert "药品生产质量管理规范" in text
        assert "ALCOA+" in text
        # 摘编条目必须明确标注，不能与条文原文混淆（合规要求）
        assert "要点摘编" in text
        # 来源排序稳定（按 source_id）
        assert text.index("ALCOA+") < text.index("药品生产质量管理规范")

    @pytest.mark.asyncio
    async def test_legacy_ref_without_source_still_renders(self, test_db):
        """历史 kb_refs 无 source_id → 落入"未标注来源"桶，不丢引用。"""
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
            "VALUES ('kb3', 'k.pdf', '/tmp/k.pdf', 'review', 1)")
        await test_db.execute(
            "INSERT INTO findings (job_id, page, type, severity, source, "
            "description, status, kb_refs) VALUES ('kb3', 1, 'completeness', "
            "'warning', 'llm_page', '旧数据', 'pending', ?)",
            (json.dumps([{"entry_id": "gmp2010-151",
                          "label": "第一百五十一条", "excerpt": "z"}],
                        ensure_ascii=False),))
        await test_db.commit()
        from main import app
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://localhost:8000") as c:
            r = await c.get("/api/jobs/kb3/report.md")
        assert r.status_code == 200
        assert "第一百五十一条" in r.text
        assert "未标注来源" in r.text
