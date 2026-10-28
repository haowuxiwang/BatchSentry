"""round-23 C2：复核反馈统计测试。

覆盖：
- _review_stats_from_rows（纯函数）：状态聚合/比率、高频驳回类型 Top-N
  与份额、按来源驳回率排序、空池与缺省值（None source/status 回退）
- get_review_stats 端点直调：404（job 不存在）/ 正常返回带 job_id
"""
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def pipeline_db(tmp_path):
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


def _row(source, status, ftype="completeness"):
    return {"type": ftype, "severity": "warning", "source": source,
            "status": status}


class TestReviewStatsFromRows:
    def test_empty_rows(self):
        from api.review import _review_stats_from_rows

        s = _review_stats_from_rows([])
        assert s["total"] == 0
        assert s["adjudicated"] == 0
        assert s["pending"] == 0
        assert s["confirm_rate"] is None
        assert s["reject_rate"] is None
        assert s["top_rejected_types"] == []
        assert s["by_source"] == []

    def test_status_aggregation_and_rates(self):
        from api.review import _review_stats_from_rows

        rows = [
            _row("rule", "confirmed"),
            _row("rule", "confirmed"),
            _row("rule", "rejected"),
            _row("llm_page", "corrected"),
            _row("llm_page", "pending"),
        ]
        s = _review_stats_from_rows(rows)
        assert s["total"] == 5
        assert s["adjudicated"] == 4
        assert s["pending"] == 1
        assert s["by_status"] == {
            "pending": 1, "confirmed": 2, "rejected": 1, "corrected": 1,
        }
        assert s["confirm_rate"] == round(2 / 4, 4)
        assert s["reject_rate"] == round(1 / 4, 4)

    def test_top_rejected_types_sorted_with_share(self):
        from api.review import _review_stats_from_rows

        rows = (
            [_row("rule", "rejected", ftype="time_reversal")] * 3
            + [_row("rule", "rejected", ftype="completeness")] * 2
            + [_row("llm_page", "rejected", ftype="param_out_of_spec")]
            + [_row("rule", "confirmed")]  # 确认项不进驳回统计
        )
        s = _review_stats_from_rows(rows)
        tops = s["top_rejected_types"]
        assert [t["type"] for t in tops[:2]] == ["time_reversal", "completeness"]
        assert tops[0]["count"] == 3
        assert tops[0]["share"] == round(3 / 6, 4)
        # type_zh 中文映射就位（复核页/报告直接展示）
        assert tops[0]["type_zh"] == "时间倒序"

    def test_by_source_aggregation_and_order(self):
        from api.review import _review_stats_from_rows

        rows = (
            [_row("rule", "rejected")] * 3
            + [_row("rule", "pending")]
            + [_row("llm_page", "confirmed")] * 2
        )
        s = _review_stats_from_rows(rows)
        by_src = {x["source"]: x for x in s["by_source"]}
        # 按总量降序：rule(4) 在前
        assert [x["source"] for x in s["by_source"]] == ["rule", "llm_page"]
        assert by_src["rule"]["total"] == 4
        assert by_src["rule"]["rejected"] == 3
        assert by_src["rule"]["pending"] == 1
        assert by_src["rule"]["reject_rate"] == round(3 / 4, 4)
        assert by_src["llm_page"]["reject_rate"] == 0.0

    def test_none_source_status_fallbacks(self):
        """缺省回退：source=None → 'rule'；status=None → 'pending'。"""
        from api.review import _review_stats_from_rows

        s = _review_stats_from_rows(
            [{"type": "t", "severity": "warning", "source": None, "status": None}]
        )
        assert s["pending"] == 1
        assert s["by_source"][0]["source"] == "rule"


class TestReviewStatsEndpoint:
    @pytest.mark.asyncio
    async def test_404_for_missing_job(self, pipeline_db):
        from fastapi import HTTPException
        from api.review import get_review_stats

        with pytest.raises(HTTPException) as ei:
            await get_review_stats("no-such-job")
        assert ei.value.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_stats_with_job_id(self, pipeline_db):
        from api.review import get_review_stats

        await pipeline_db.execute(
            "INSERT INTO jobs (id, filename, status, pdf_path) "
            "VALUES ('job-st', 't.pdf', 'review', '/tmp/t.pdf')"
        )
        for i, (src, st) in enumerate(
            [("rule", "confirmed"), ("rule", "rejected"), ("llm_page", "pending")]
        ):
            await pipeline_db.execute(
                "INSERT INTO findings (job_id, page, type, severity, "
                "description, source, status, created_at) VALUES "
                "('job-st', 1, 'completeness', 'warning', ?, ?, ?, "
                "datetime('now','localtime'))",
                (f"finding-{i}", src, st),
            )
        await pipeline_db.commit()

        s = await get_review_stats("job-st")
        assert s["job_id"] == "job-st"
        assert s["total"] == 3
        assert s["by_status"]["rejected"] == 1
        assert s["reject_rate"] == 0.5  # 1/2 已裁决
        assert {x["source"] for x in s["by_source"]} == {"rule", "llm_page"}
