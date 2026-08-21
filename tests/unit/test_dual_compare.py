"""门禁 3 双后端对比测试（OCR_GOLDEN_CORPUS.md gate 3）。

覆盖：
- compare_page 纯逻辑：一致 / 文本差异 / 单侧空页 / 表格数差异
- run_dual_compare：开关关闭跳过、备选凭据缺失跳过、备选异常不阻断、
  差异检出 + 审计留痕
- stage3 消费 dual_diff：强制 partial_review + completeness findings 落库
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from config import config


@pytest_asyncio.fixture
async def pipeline_db(tmp_path):
    """与 test_pipeline.py 同款隔离库 fixture（fixture 不跨文件共享）。"""
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


def _page(text: str) -> dict:
    return {"markdown": {"text": text}}


class TestComparePage:
    def test_identical_pages_no_diff(self):
        from core.pipeline.dual_compare import compare_page

        html = "<table><tr><td>温度 25.0</td></tr></table> 正文内容"
        is_diff, reason = compare_page(html, html)
        assert is_diff is False
        assert reason == ""

    def test_whitespace_and_tags_ignored(self):
        from core.pipeline.dual_compare import compare_page

        a = "<table><tr><td>温 度</td></tr></table>"
        b = "温\t度\n"  # 不同标签结构 + 空白差异 → 内容一致不算差异
        is_diff, _ = compare_page(a, b)
        assert is_diff is False

    def test_table_markup_style_difference_not_flagged(self):
        """标记风格差异（HTML 表 vs 纯文本）不误报 — 结构≠内容。"""
        from core.pipeline.dual_compare import compare_page

        a = "<table><tr><td>A</td></tr></table><table><tr><td>B</td></tr></table>"
        b = "A B"
        is_diff, _ = compare_page(a, b)
        assert is_diff is False

    def test_low_similarity_diff(self):
        from core.pipeline.dual_compare import compare_page

        a = "第一批记录正文内容温度压力时间签名完整数据" * 10
        b = "完全不同的另一份文档内容关于包装和标签的信息描述" * 10
        is_diff, reason = compare_page(a, b)
        assert is_diff is True
        assert "覆盖率" in reason

    def test_one_side_empty_diff(self):
        from core.pipeline.dual_compare import compare_page

        is_diff, reason = compare_page("有内容的页面", "")
        assert is_diff is True
        assert "单侧空页" in reason
        # 双侧都空不算差异
        is_diff2, _ = compare_page("", "")
        assert is_diff2 is False

    def test_dropped_table_content_lower_similarity(self):
        """整表内容丢失（非标记风格差异）→ 覆盖率下降 → 判差异。"""
        from core.pipeline.dual_compare import compare_page

        filler = "正文说明文字" * 20
        a = f"<table><tr><td>清洗温度25</td><td>时长30分钟</td></tr></table>{filler}"
        b = filler  # 副侧整表内容丢失
        is_diff, reason = compare_page(a, b)
        assert is_diff is True
        assert "主侧" in reason and "缺失" in reason

    def test_table_cells_present_in_plaintext_not_flagged(self):
        """纯标记风格差异（同内容不同包装）→ 不误报。"""
        from core.pipeline.dual_compare import compare_page

        a = "<table><tr><td>清洗温度25</td><td>时长30分钟</td></tr></table>"
        b = "<div>清洗温度25</div><p>时长30分钟</p>"
        is_diff, _ = compare_page(a, b)
        assert is_diff is False

    def test_extra_content_on_one_side_is_reported(self):
        """副侧多识别出的实质内容（如主侧漏的手写签名）→ 报差异。

        门禁 3 语义：一侧丢失对方已有的内容即差异；"额外内容不是
        丢失"仅指包裹性文字，新出现的实体信息属于识别分歧。
        """
        from core.pipeline.dual_compare import compare_page

        a = "<table><tr><td>清洗温度25</td><td>时长30分钟</td></tr></table>"
        b = "清洗温度25 时长30分钟 操作员张三复核日期2026.08.21"
        is_diff, reason = compare_page(a, b)
        assert is_diff is True
        assert "副侧内容在主侧缺失" in reason

    def test_ocr_warning_prefix_stripped(self):
        """警告前缀只在一侧存在时不应误判差异。"""
        from core.pipeline.dual_compare import compare_page

        a = "[OCR 警告: 页面无可用文本，以下内容可能不完整，分析仅供参考]\n\n正文"
        is_diff, _ = compare_page(a, "正文")
        assert is_diff is False


class TestRunDualCompare:
    @pytest.mark.asyncio
    async def test_disabled_returns_empty(self, pipeline_db):
        from core.pipeline.dual_compare import run_dual_compare

        orig = config["app"].ocr_dual_compare
        config["app"].ocr_dual_compare = False
        try:
            diffs = await run_dual_compare(
                pipeline_db, "job-1", "/tmp/x.pdf", "paddle", [_page("a")]
            )
            assert diffs == []
        finally:
            config["app"].ocr_dual_compare = orig

    @pytest.mark.asyncio
    async def test_secondary_unavailable_skips_with_audit(self, pipeline_db):
        from core.pipeline.dual_compare import run_dual_compare

        orig = config["app"].ocr_dual_compare
        orig_mineru_token = config["mineru"].token
        config["app"].ocr_dual_compare = True
        config["mineru"].token = ""
        try:
            diffs = await run_dual_compare(
                pipeline_db, "job-1", "/tmp/x.pdf", "paddle", [_page("a")]
            )
            assert diffs == []
            cur = await pipeline_db.execute(
                "SELECT detail FROM audit_log WHERE job_id=? AND action='dual_compare_skipped'",
                ("job-1",),
            )
            row = await cur.fetchone()
            assert row and "mineru" in row["detail"]
        finally:
            config["app"].ocr_dual_compare = orig
            config["mineru"].token = orig_mineru_token

    @pytest.mark.asyncio
    async def test_cached_backend_skips(self, pipeline_db):
        from core.pipeline.dual_compare import run_dual_compare

        orig = config["app"].ocr_dual_compare
        config["app"].ocr_dual_compare = True
        try:
            diffs = await run_dual_compare(
                pipeline_db, "job-1", "/tmp/x.pdf", "cached", [_page("a")]
            )
            assert diffs == []
        finally:
            config["app"].ocr_dual_compare = orig

    @pytest.mark.asyncio
    async def test_secondary_crash_does_not_block(self, pipeline_db):
        from core.pipeline.dual_compare import run_dual_compare

        orig = config["app"].ocr_dual_compare
        orig_mineru_token = config["mineru"].token
        config["app"].ocr_dual_compare = True
        config["mineru"].token = "tok"
        try:
            with patch(
                "core.mineru_client.run_ocr",
                side_effect=RuntimeError("secondary down"),
            ):
                diffs = await run_dual_compare(
                    pipeline_db, "job-1", "/tmp/x.pdf", "paddle", [_page("a")]
                )
            assert diffs == []
            cur = await pipeline_db.execute(
                "SELECT detail FROM audit_log WHERE job_id=? AND action='dual_compare_error'",
                ("job-1",),
            )
            assert await cur.fetchone() is not None
        finally:
            config["app"].ocr_dual_compare = orig
            config["mineru"].token = orig_mineru_token

    @pytest.mark.asyncio
    async def test_diffs_detected_and_audited(self, pipeline_db):
        from core.pipeline.dual_compare import run_dual_compare

        orig = config["app"].ocr_dual_compare
        orig_mineru_token = config["mineru"].token
        config["app"].ocr_dual_compare = True
        config["mineru"].token = "tok"
        primary = [
            _page("第一批页面内容温度二十五度完整记录" * 5),
            _page("第二批页面内容压力正常记录在案" * 5),
            _page(""),  # 主侧空页 vs 副侧有内容 → 差异
        ]
        secondary = [
            _page("第一批页面内容温度二十五度完整记录" * 5),  # 一致
            _page("完全不同第二页内容关于包装标签的说明文字" * 5),  # 相似度低 → 差异
            _page("副侧识别出了内容主侧没有"),
        ]
        try:
            with patch(
                "core.mineru_client.run_ocr", return_value=secondary
            ):
                diffs = await run_dual_compare(
                    pipeline_db, "job-1", "/tmp/x.pdf", "paddle", primary
                )
            assert [d["page"] for d in diffs] == [2, 3]
            cur = await pipeline_db.execute(
                "SELECT detail FROM audit_log WHERE job_id=? AND action='dual_compare_done'",
                ("job-1",),
            )
            row = await cur.fetchone()
            assert row and "diffs=2" in row["detail"]
        finally:
            config["app"].ocr_dual_compare = orig
            config["mineru"].token = orig_mineru_token


class TestStage3DualDiffConsumption:
    @pytest.mark.asyncio
    async def test_dual_diff_forces_partial_review_and_findings(
        self, pipeline_db
    ):
        from core.pipeline import _run_stage3_cross_analysis

        job_id = "job-dual"
        await pipeline_db.execute(
            "INSERT INTO jobs (id, filename, status, pdf_path) VALUES (?, ?, ?, ?)",
            (job_id, "t.pdf", "analyzing", "/tmp/t.pdf"),
        )
        # 一页已分析（无空页）
        await pipeline_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
            "VALUES (?, 1, 'p1', '{}')",
            (job_id,),
        )
        await pipeline_db.commit()

        dual_diff = [
            {"page": 1, "reason": "文本相似度 0.42（阈值 0.85）"},
            {"page": 2, "reason": "单侧空页（一侧有正文另一侧无可识别文本）"},
        ]
        with patch(
            "core.pipeline.analyze_cross_page",
            new=AsyncMock(return_value=[]),
        ), patch(
            "core.pipeline._is_cancelled", new=AsyncMock(return_value=False)
        ):
            await _run_stage3_cross_analysis(
                pipeline_db, job_id, 100, 200, [], __import__("time").time(),
                dual_diff=dual_diff,
            )

        cur = await pipeline_db.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        )
        assert (await cur.fetchone())["status"] == "partial_review"

        cur = await pipeline_db.execute(
            "SELECT page, type, severity, source, description FROM findings "
            "WHERE job_id = ? ORDER BY page",
            (job_id,),
        )
        rows = await cur.fetchall()
        assert len(rows) == 2
        assert all(r["type"] == "completeness" for r in rows)
        assert all(r["severity"] == "warning" for r in rows)
        assert all(r["source"] == "rule" for r in rows)
        assert "双后端" in rows[0]["description"]

    @pytest.mark.asyncio
    async def test_no_dual_diff_keeps_review(self, pipeline_db):
        from core.pipeline import _run_stage3_cross_analysis

        job_id = "job-clean"
        await pipeline_db.execute(
            "INSERT INTO jobs (id, filename, status, pdf_path) VALUES (?, ?, ?, ?)",
            (job_id, "t.pdf", "analyzing", "/tmp/t.pdf"),
        )
        await pipeline_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
            "VALUES (?, 1, 'p1', '{}')",
            (job_id,),
        )
        await pipeline_db.commit()

        with patch(
            "core.pipeline.analyze_cross_page",
            new=AsyncMock(return_value=[]),
        ), patch(
            "core.pipeline._is_cancelled", new=AsyncMock(return_value=False)
        ):
            await _run_stage3_cross_analysis(
                pipeline_db, job_id, 100, 200, [], __import__("time").time()
            )

        cur = await pipeline_db.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        )
        assert (await cur.fetchone())["status"] == "review"
