"""R121 / TODO 0-64：**已落库**的文档级事实不得逐页重复。

## 缺陷（实测，不是推演）

`4cffb358`（10 页）复核页上出现两张一模一样的卡片：

    第 5 页   completeness  「批生产记录缺少生产日期信息。」
    第 10 页  completeness  「批生产记录缺少生产日期信息。」

缺陷为真（10 页 `raw_html` 里没有任何一页有「生产日期」字段），但**报告了两遍**。
根因不是"没有聚合逻辑"，而是**聚合逻辑看不见它们**：
`reduce_document_level_facts`（CR-10）只跑在 Stage 3 的**内存** findings 上，
而 `llm_page` 在 Stage 2 每页完成时就已落库、不在那个列表里。

## 判据

1. 同一 job、同一条文档级事实（`document_fact_kind` 同组）、**跨页** ⇒ 收敛为 1 条，
   保留行改写成聚合摘要并挂 `guard_reason`（R120 起界面上可见）；
2. **同页**两条不合并 —— 那是「同页重复插入」这个**另一个缺陷**（历史数据，
   A9 之前产生），合并会把它的症状静默掩盖掉；
3. 已裁决（confirmed/rejected/corrected）的行**不动** —— 那是复核结论；
4. 规则层来源不动 —— 它已有 `reduce_completeness_noise` 的阈值治理；
5. 非文档级文案（如「第 5 页缺少签名」）不动。
"""
from __future__ import annotations

import pytest_asyncio

from core.finding_noise import plan_document_level_collapse


def _row(fid, page, desc="批生产记录缺少生产日期信息。", *, status="pending",
         source="llm_page", ftype="completeness", severity="warning"):
    return {"id": fid, "page": page, "type": ftype, "severity": severity,
            "description": desc, "source": source, "status": status}


class TestPlanner:
    def test_cross_page_doc_fact_is_merged(self):
        plan = plan_document_level_collapse([_row(1590, 5), _row(1595, 10)])
        assert plan["delete_ids"] == [1595]
        assert set(plan["update"]) == {1590}
        cols = plan["update"][1590]
        assert "全份记录" in cols["description"], "保留行必须改写成聚合摘要"
        assert cols["severity"] == "info", "文档级事实是模板属性，与内存路径同一口径"
        assert "5、10" in cols["guard_reason"]
        assert plan["groups"]["missing_production_date"]["pages"] == [5, 10]

    def test_same_page_duplicate_is_not_merged(self):
        """同页重复是**另一个缺陷**（A9 之前的历史数据），不能被这里掩盖。"""
        plan = plan_document_level_collapse([_row(1, 5), _row(2, 5)])
        assert plan["update"] == {} and plan["delete_ids"] == []

    def test_single_row_is_noop(self):
        plan = plan_document_level_collapse([_row(1, 5)])
        assert plan["update"] == {} and plan["delete_ids"] == []

    def test_reviewed_rows_are_untouched(self):
        """已裁决的行是复核结论 —— 聚合不得让它消失。"""
        plan = plan_document_level_collapse(
            [_row(1, 5, status="confirmed"), _row(2, 10, status="pending")]
        )
        assert plan["update"] == {}, "只剩 1 条 pending ⇒ 达不到门槛，不得合并"

    def test_confirmed_plus_two_pending_still_merges_pending_only(self):
        plan = plan_document_level_collapse([
            _row(1, 5, status="confirmed"),
            _row(2, 10), _row(3, 11),
        ])
        assert plan["delete_ids"] == [3]
        assert set(plan["update"]) == {2}
        assert 1 not in plan["update"], "已裁决那条不得被改写"

    def test_rule_source_is_untouched(self):
        plan = plan_document_level_collapse(
            [_row(1, 5, source="rule"), _row(2, 10, source="rule")]
        )
        assert plan["update"] == {} and plan["delete_ids"] == []

    def test_non_document_level_text_is_untouched(self):
        plan = plan_document_level_collapse([
            _row(1, 5, "第5页 缺少签名"),
            _row(2, 10, "第5页 缺少签名"),
        ])
        assert plan["update"] == {} and plan["delete_ids"] == []

    def test_existing_guard_reason_is_preserved(self):
        """保留行可能已有 R97 的降级理由 —— 合并**追加**，不得覆盖。

        实测 `4cffb358` p1/p8 的行就带着"文案为推测性表述 ⇒ 降级为待人工核对"。
        覆盖会让复核员看到 info 却不知道为什么（R120 刚把这条接上界面）。
        """
        rows = [_row(1, 5), _row(2, 10)]
        rows[0]["guard_reason"] = "文案为推测性表述 ⇒ 降级为待人工核对"
        plan = plan_document_level_collapse(rows)
        merged = plan["update"][1]["guard_reason"]
        assert "推测性表述" in merged, "原有理由被覆盖 —— 解释链断了"
        assert "已合并为 1 条" in merged

    def test_three_pages_collapse_to_one(self):
        plan = plan_document_level_collapse([_row(1, 5), _row(2, 10), _row(3, 11)])
        assert plan["delete_ids"] == [2, 3]
        assert plan["groups"]["missing_production_date"]["count"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# DB 层：计划真的被执行（改保留行 + 删多余行 + 落审计）
# ─────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def doc_db(tmp_path):
    import db.client as db_mod
    from config import config as _cfg

    orig = (_cfg["app"].database_path, _cfg["app"].output_dir, db_mod._db)
    _cfg["app"].database_path = str(tmp_path / "doc.db")
    _cfg["app"].output_dir = str(tmp_path / "output")
    db_mod._db = None
    try:
        db = await db_mod.get_db()
        await db.execute(
            "INSERT INTO jobs (id, filename, status, pdf_path) "
            "VALUES ('job-doc', 't.pdf', 'review', '/tmp/t.pdf')"
        )
        await db.commit()
        yield db
    finally:
        import core.pipeline as p_mod

        p_mod.db_lock._loop = None
        p_mod.db_lock._waiters = None
        p_mod.db_lock._locked = False
        if db_mod._db:
            await db_mod._db.close()
        db_mod._db = orig[2]
        _cfg["app"].database_path = orig[0]
        _cfg["app"].output_dir = orig[1]


async def _seed(db, page, desc, *, status="pending", source="llm_page"):
    await db.execute(
        "INSERT INTO findings (job_id, page, type, severity, description, "
        "source, status) VALUES ('job-doc', ?, 'completeness', 'warning', ?, ?, ?)",
        (page, desc, source, status),
    )


class TestPersistedCollapse:
    async def test_rows_are_merged_in_the_database(self, doc_db):
        db = doc_db
        await _seed(db, 5, "批生产记录缺少生产日期信息。")
        await _seed(db, 10, "批生产记录缺少生产日期信息。")
        await db.commit()

        from core.pipeline.stage3 import _collapse_persisted_document_facts

        plan = await _collapse_persisted_document_facts(db, "job-doc")
        assert plan["delete_ids"], "计划为空 —— 后面查库的断言会变成空转"

        rows = [dict(r) for r in await (
            await db.execute(
                "SELECT id, page, severity, description, guard_reason FROM findings "
                "WHERE job_id='job-doc'"
            )
        ).fetchall()]
        assert len(rows) == 1, f"应只剩 1 条，实际 {len(rows)} 条"
        kept = rows[0]
        assert kept["page"] == 5, "保留最早出现的那条（页序稳定，便于复核定位）"
        assert kept["severity"] == "info"
        assert "全份记录" in kept["description"]
        assert "5、10" in (kept["guard_reason"] or "")

        audit = [dict(r) for r in await (
            await db.execute(
                "SELECT action, detail FROM audit_log WHERE job_id='job-doc' "
                "AND action='findings_doc_level_collapsed'"
            )
        ).fetchall()]
        assert audit, "合并必须落审计（GMP 可追溯「为什么少了一条」）"
        assert "missing_production_date" in audit[0]["detail"]

    async def test_confirmed_row_survives_the_pass(self, doc_db):
        db = doc_db
        await _seed(db, 5, "批生产记录缺少生产日期信息。", status="confirmed")
        await _seed(db, 10, "批生产记录缺少生产日期信息。")
        await _seed(db, 11, "批生产记录缺少生产日期信息。")
        await db.commit()

        from core.pipeline.stage3 import _collapse_persisted_document_facts

        await _collapse_persisted_document_facts(db, "job-doc")

        rows = [dict(r) for r in await (
            await db.execute("SELECT page, status FROM findings WHERE job_id='job-doc'")
        ).fetchall()]
        assert len(rows) == 2, f"应剩「已裁决 1 + 合并后 1」，实际 {rows}"
        assert {r["status"] for r in rows} == {"confirmed", "pending"}

    async def test_noop_when_nothing_to_merge(self, doc_db):
        db = doc_db
        await _seed(db, 5, "第5页 缺少签名")
        await _seed(db, 10, "第10页 缺少复核人")
        await db.commit()

        from core.pipeline.stage3 import _collapse_persisted_document_facts

        plan = await _collapse_persisted_document_facts(db, "job-doc")
        assert plan["update"] == {}
        n = (await (await db.execute(
            "SELECT COUNT(*) FROM findings WHERE job_id='job-doc'")).fetchone())[0]
        assert n == 2, "不该动无关条目"


# ─────────────────────────────────────────────────────────────────────────────
# 管道层：**stage3 真的会调用它**（判据写好了但没接线 = R105 形态）
# ─────────────────────────────────────────────────────────────────────────────

async def _seed_job(db, job_id: str, pages: list[int]) -> None:
    import json as _json

    await db.execute(
        "INSERT INTO jobs (id, filename, status, pdf_path) VALUES (?, 't.pdf', 'analyzing', '/tmp/t.pdf')",
        (job_id,),
    )
    for pg in pages:
        await db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
            "VALUES (?, ?, ?, ?)",
            (job_id, pg, "<p>x</p>",
             _json.dumps({"page_info": {}, "steps": [], "findings": [],
                          "overall_confidence": "high"}, ensure_ascii=False)),
        )
    await db.commit()


async def _run_stage3(db, job_id: str) -> None:
    import time as _time
    from unittest.mock import AsyncMock, patch

    from core.pipeline import _run_stage3_cross_analysis

    with patch("core.rules._llm_fallback_check", new=AsyncMock(return_value=[])), \
            patch("core.rules._llm_based_check", new=AsyncMock(return_value=[])), \
            patch("core.pipeline._is_cancelled", new=AsyncMock(return_value=False)):
        await _run_stage3_cross_analysis(db, job_id, 100, 200, [], _time.time())


class TestPipelineWiring:
    """`TestPersistedCollapse` 直接调 helper ⇒ 即使 stage3 不调用它，那些用例照样绿。

    变异 M5（把调用点删掉）实测**没有被抓住**，正是这个原因。本类补上：
    真跑 `_run_stage3_cross_analysis`，再查 findings 表。
    """

    async def test_stage3_merges_persisted_duplicates(self, doc_db):
        db = doc_db
        await _seed_job(db, "job-pipe", [5, 10])
        await db.execute(
            "INSERT INTO findings (job_id, page, type, severity, description, "
            "source, status) VALUES "
            "('job-pipe', 5, 'completeness', 'warning', '批生产记录缺少生产日期信息。', 'llm_page', 'pending')"
        )
        await db.execute(
            "INSERT INTO findings (job_id, page, type, severity, description, "
            "source, status) VALUES "
            "('job-pipe', 10, 'completeness', 'warning', '批生产记录缺少生产日期信息。', 'llm_page', 'pending')"
        )
        await db.commit()

        await _run_stage3(db, "job-pipe")

        rows = [dict(r) for r in await (
            await db.execute(
                "SELECT page, severity, description, guard_reason FROM findings "
                "WHERE job_id='job-pipe' AND source='llm_page' AND type='completeness'"
            )
        ).fetchall()]
        assert len(rows) == 1, f"stage3 跑完应只剩 1 条，实际 {rows}"
        assert "全份记录" in rows[0]["description"]
        assert "5、10" in (rows[0]["guard_reason"] or "")
