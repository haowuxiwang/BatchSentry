"""R119 管道级回归：判据必须在**真实 stage3 路径**上落到 findings 表。

为什么单写这一组（`test_r119_signature_pairing.py` 已经覆盖判据本身）：
R105 的教训是「判据层绿、管道层丢字段，端到端等于没上线」。这条判据需要
`page_cache.raw_html` 从 SQL 一路传到规则层，中间有三道门：

    SELECT raw_html → page_structures[i]["raw_html"]
        → _normalize_pages 保留 → _check_completeness 读 page["raw_html"]

任何一道漏传，判据**静默失效**且所有单测仍然绿。本文件只做一件事：
真的跑 `_run_stage3_cross_analysis`，然后查 **findings 表**。

LLM 两条支路打桩为空实现（不打桩会真调上游）—— 这也是 `r111_replay.py` 的
口径：量到的是**规则层 + 守卫层**的净效果。
"""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

PAIRING_SQL = "SELECT page, type, severity, source, description, ocr_text FROM findings WHERE job_id = ? AND ocr_text LIKE 'label=%'"


@pytest_asyncio.fixture
async def pipeline_db(tmp_path):
    """与 `test_dual_compare.py` 同款隔离库 fixture（**fixture 不跨文件共享**）。"""
    import db.client as db_mod
    from config import config as _cfg

    db_path = tmp_path / "test.db"
    orig = (_cfg["app"].database_path, _cfg["app"].output_dir, db_mod._db)
    _cfg["app"].database_path = str(db_path)
    _cfg["app"].output_dir = str(tmp_path / "output")
    db_mod._db = None
    try:
        db = await db_mod.get_db()
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


async def _seed(pipeline_db, job_id: str, raw_html: str) -> None:
    await pipeline_db.execute(
        "INSERT INTO jobs (id, filename, status, pdf_path) VALUES (?, ?, ?, ?)",
        (job_id, "t.pdf", "analyzing", "/tmp/t.pdf"),
    )
    structured = {
        "page_info": {}, "steps": [], "findings": [],
        "overall_confidence": "high",
    }
    await pipeline_db.execute(
        "INSERT INTO page_cache (job_id, page, raw_html, structured_json) VALUES (?, 1, ?, ?)",
        (job_id, raw_html, json.dumps(structured, ensure_ascii=False)),
    )
    await pipeline_db.commit()


async def _run(pipeline_db, job_id: str) -> None:
    from core.pipeline import _run_stage3_cross_analysis

    with patch("core.rules._llm_fallback_check", new=AsyncMock(return_value=[])), \
            patch("core.rules._llm_based_check", new=AsyncMock(return_value=[])), \
            patch("core.pipeline._is_cancelled", new=AsyncMock(return_value=False)):
        await _run_stage3_cross_analysis(
            pipeline_db, job_id, 100, 200, [], time.time(),
        )


async def _pairing_rows(pipeline_db, job_id: str) -> list:
    cur = await pipeline_db.execute(PAIRING_SQL, (job_id,))
    return list(await cur.fetchall())


class TestPairingSurvivesTheWholePath:
    @pytest.mark.asyncio
    async def test_date_only_slot_lands_in_findings_table(self, pipeline_db):
        job = "job-pair-pos"
        await _seed(
            pipeline_db, job,
            "<table><tr><td>复核人：2025.02.21</td></tr></table>",
        )
        await _run(pipeline_db, job)

        rows = await _pairing_rows(pipeline_db, job)
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["page"] == 1
        assert row["type"] == "completeness"
        assert row["severity"] == "info"
        assert row["source"] == "rule"
        assert "只识别到日期" in row["description"]
        # 归因不能写成"未签"：原页可能签了，只是 OCR 只认到日期
        assert "OCR" in row["description"]

    @pytest.mark.asyncio
    async def test_name_only_slot_lands_as_warning(self, pipeline_db):
        job = "job-pair-pos2"
        await _seed(
            pipeline_db, job,
            "<table><tr><td>中控QA签名/日期：赵阿珍</td></tr></table>",
        )
        await _run(pipeline_db, job)

        rows = await _pairing_rows(pipeline_db, job)
        assert len(rows) == 1, rows
        assert rows[0]["severity"] == "warning"
        assert "没有填写日期" in rows[0]["description"]

    @pytest.mark.asyncio
    async def test_complete_slot_produces_nothing(self, pipeline_db):
        """反向对照：两半都有的格不得产出任何 label= 行（否则是空转）。"""
        job = "job-pair-neg"
        await _seed(
            pipeline_db, job,
            "<table><tr><td>复核人：付付 2025.02.21</td></tr></table>",
        )
        await _run(pipeline_db, job)

        assert await _pairing_rows(pipeline_db, job) == []
