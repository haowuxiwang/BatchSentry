"""分片路径保护补齐测试（P0-3）。

覆盖：
- _self_heal_empty_pages：skip_pages 排除 + 返回恢复页列表
- 分片路径 pdf_diag 注入（assess_ocr_page 收到页级 PDF 结构诊断）
- 分片路径空页自愈 + 恢复页补跑分析（structured_json 重建）
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from config import config


@pytest_asyncio.fixture
async def pipeline_db(tmp_path):
    """与 test_pipeline.py 同款隔离库 fixture。"""
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


async def _insert_job(db, job_id="job-1", status="pending"):
    await db.execute(
        "INSERT INTO jobs (id, filename, status, pdf_path) VALUES (?, ?, ?, ?)",
        (job_id, "test.pdf", status, "/tmp/test.pdf"),
    )
    await db.commit()
    return job_id


class TestSelfHealSkipAndReturn:
    @pytest.mark.asyncio
    async def test_returns_recovered_pages(self, pipeline_db, tmp_path):
        from core.pipeline.self_heal import _self_heal_empty_pages

        job_id = await _insert_job(pipeline_db)
        # p1 空（待恢复）、p2 正常长文本
        await pipeline_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html) VALUES (?, 1, '')",
            (job_id,),
        )
        await pipeline_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html) VALUES (?, 2, ?)",
            (job_id, "page 2 content " + "x" * 200),
        )
        await pipeline_db.commit()

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            return [(pno, "recovered content " + "y" * 200, 0) for pno in page_nums]

        with patch("core.mineru_client.run_ocr_pages", side_effect=fake_retry):
            recovered = await _self_heal_empty_pages(
                pipeline_db, job_id, str(tmp_path / "x.pdf"), [], "mineru"
            )
        assert recovered == [1]

    @pytest.mark.asyncio
    async def test_skip_pages_excluded_from_targets(self, pipeline_db, tmp_path):
        from core.pipeline.self_heal import _self_heal_empty_pages

        job_id = await _insert_job(pipeline_db)
        # p1、p2 都是空页，但 p1 已成功分析 → 应被跳过
        for pno in (1, 2):
            await pipeline_db.execute(
                "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
                "VALUES (?, ?, '', ?)",
                (job_id, pno, json.dumps({"steps": [], "_marker": "ok"})),
            )
        await pipeline_db.commit()

        calls = []

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            calls.append(list(page_nums))
            return [(pno, "", 0) for pno in page_nums]

        with patch("core.mineru_client.run_ocr_pages", side_effect=fake_retry):
            recovered = await _self_heal_empty_pages(
                pipeline_db, job_id, str(tmp_path / "x.pdf"), [], "mineru",
                skip_pages={1},
            )
        assert recovered == []
        assert calls and calls[0] == [2]  # 只重跑了 p2


class TestFullPathDiagnosticsWorkingCopy:
    """#1 整份路径页级诊断基于规范化工作副本（与分片路径对齐）。"""

    @pytest.mark.asyncio
    async def test_full_path_diag_from_normalized_copy(self, pipeline_db, tmp_path):
        import fitz
        from core import pipeline as pipeline_mod

        orig_backend = pipeline_mod.config["app"].ocr_backend
        orig_dual = pipeline_mod.config["app"].ocr_dual_compare
        pipeline_mod.config["app"].ocr_backend = "mineru"
        pipeline_mod.config["app"].ocr_dual_compare = False
        try:
            job_id = await _insert_job(pipeline_db)
            pdf_path = str(tmp_path / "abnormal.pdf")
            # 第 1 页正常 A4；第 2 页 3000x4000pt 畸形盒（触发规范化）
            doc = fitz.open()
            p1 = doc.new_page(width=595, height=842)
            pix1 = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2479, 3509), False)
            pix1.clear_with(200)
            p1.insert_image(p1.rect, pixmap=pix1)
            p2 = doc.new_page(width=3000, height=4000)
            pix2 = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 3000, 4000), False)
            pix2.clear_with(200)
            p2.insert_image(p2.rect, pixmap=pix2)
            doc.save(pdf_path)
            doc.close()

            def fake_run(pdf, cb=None, job_id=None):
                return [
                    {"markdown": {"text": "page 1 " + "x" * 200}},
                    {"markdown": {"text": "page 2 " + "y" * 200}},
                ]

            with patch(
                "core.pipeline._get_ocr_backend", return_value=fake_run
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(
                    return_value={"steps": [], "findings": [],
                                  "overall_confidence": "high"}
                ),
            ), patch(
                "core.pipeline.analyze_cross_page",
                new=AsyncMock(return_value=[]),
            ):
                from core.pipeline import run_pipeline
                await run_pipeline(job_id, pdf_path)

            # 规范化后工作副本第 2 页为 720x960pt@300dpi → 不应再带
            # low_dpi / 页面盒异常警告（原件诊断是过时证据）
            cur = await pipeline_db.execute(
                "SELECT raw_html, ocr_diagnostics FROM page_cache "
                "WHERE job_id = ? AND page = 2",
                (job_id,),
            )
            row = await cur.fetchone()
            assert "页面盒异常" not in (row["raw_html"] or "")
            assert "有效 DPI" not in (row["raw_html"] or "")
            diag = json.loads(row["ocr_diagnostics"])
            assert diag.get("low_dpi") is not True

            # 原件诊断另存审计（原始证据链）
            cur = await pipeline_db.execute(
                "SELECT detail FROM audit_log WHERE job_id = ? "
                "AND action = 'ocr_input_normalized_diag'",
                (job_id,),
            )
            audit_row = await cur.fetchone()
            assert audit_row and "3000" in audit_row["detail"]
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend
            pipeline_mod.config["app"].ocr_dual_compare = orig_dual


class TestSlicedPathProtections:
    async def _run_sliced(self, pipeline_db, tmp_path, pages, fake_retry=None):
        """驱动分片路径（复用 test_pipeline.py 的 patch 模式）。"""
        from core import pipeline as pipeline_mod

        orig_backend = pipeline_mod.config["app"].ocr_backend
        orig_slices = pipeline_mod.config["app"].ocr_slices
        orig_timeout = pipeline_mod._SLICE_QUEUE_TIMEOUT
        orig_dual = pipeline_mod.config["app"].ocr_dual_compare
        pipeline_mod.config["app"].ocr_backend = "mineru"
        pipeline_mod.config["app"].ocr_slices = 2
        pipeline_mod.config["app"].ocr_dual_compare = False
        pipeline_mod._SLICE_QUEUE_TIMEOUT = 0.05

        job_id = await _insert_job(pipeline_db)
        pdf_path = str(tmp_path / "fake.pdf")
        from pathlib import Path
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        def fake_run_sliced(pdf_path, slice_pages, on_batch, progress_cb, job_id=None):
            on_batch(1, pages, len(pages))
            return []

        try:
            async def fake_analyze(raw_html, page_num=None, job_id=None, cancel_check=None):
                # 模拟真实 analyze_page：剥 OCR 警告前缀后空页短路
                import re as _re
                cleaned = _re.sub(r"^\[OCR 警告:[^\]]*\]\s*", "", raw_html or "")
                if not cleaned.strip() or "此页无文本内容" in cleaned:
                    return {
                        "_ocr_empty": True, "overall_confidence": "low",
                        "steps": [], "findings": [],
                    }
                return {"steps": [], "findings": [], "overall_confidence": "high"}

            patches = [
                patch(
                    "core.mineru_client.run_ocr_sliced",
                    side_effect=fake_run_sliced,
                ),
                patch(
                    "core.pipeline.analyze_page",
                    new=AsyncMock(side_effect=fake_analyze),
                ),
                patch(
                    "core.pipeline.analyze_cross_page",
                    new=AsyncMock(return_value=[]),
                ),
                # fake.pdf 不是真 PDF — patch 诊断注入以验证管道传参
                patch(
                    "core.pipeline.ocr_support._pdf_page_diagnostics",
                    return_value={
                        1: {
                            "media_box_pt": [595.0, 842.0], "rotation": 0,
                            "aspect_ratio": 0.707, "image_pixels": [2479, 3509],
                            "effective_dpi": 300.0, "low_dpi": False,
                        },
                    },
                ),
            ]
            if fake_retry is not None:
                patches.append(
                    patch(
                        "core.mineru_client.run_ocr_pages",
                        side_effect=fake_retry,
                    )
                )
            import contextlib
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                from core.pipeline import run_pipeline
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend
            pipeline_mod.config["app"].ocr_slices = orig_slices
            pipeline_mod._SLICE_QUEUE_TIMEOUT = orig_timeout
            pipeline_mod.config["app"].ocr_dual_compare = orig_dual
        return job_id

    @pytest.mark.asyncio
    async def test_sliced_persists_pdf_diagnostics(self, pipeline_db, tmp_path):
        """分片落库的 ocr_diagnostics 应含页级 PDF 结构诊断字段。"""
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}, "page_count": i}
            for i in range(1, 4)
        ]
        job_id = await self._run_sliced(pipeline_db, tmp_path, pages)

        cur = await pipeline_db.execute(
            "SELECT page, ocr_diagnostics FROM page_cache WHERE job_id = ? "
            "ORDER BY page LIMIT 1",
            (job_id,),
        )
        row = await cur.fetchone()
        diag = json.loads(row["ocr_diagnostics"])
        assert diag.get("media_box_pt") == [595.0, 842.0]
        assert diag.get("effective_dpi") == 300.0

    @pytest.mark.asyncio
    async def test_sliced_empty_page_selfheals_and_reanalyzes(
        self, pipeline_db, tmp_path
    ):
        """分片模式空页 → 自愈恢复 → structured_json 重建（非 _ocr_empty）。"""
        pages = [
            {"markdown": {"text": "page 1 content " + "x" * 200}, "page_count": 1},
            {"markdown": {"text": ""}, "page_count": 2},  # 空页
            {"markdown": {"text": "page 3 content " + "x" * 200}, "page_count": 3},
        ]

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            return [
                (pno, f"recovered p{pno} " + "y" * 200, 0) for pno in page_nums
            ]

        job_id = await self._run_sliced(pipeline_db, tmp_path, pages, fake_retry)

        cur = await pipeline_db.execute(
            "SELECT raw_html, structured_json FROM page_cache "
            "WHERE job_id = ? AND page = 2",
            (job_id,),
        )
        row = await cur.fetchone()
        assert "recovered p2" in (row["raw_html"] or "")
        sj = json.loads(row["structured_json"])
        # 恢复页补跑分析后不再是空页短路结果
        assert not sj.get("_ocr_empty")

        # 自愈审计存在
        cur = await pipeline_db.execute(
            "SELECT 1 FROM audit_log WHERE job_id = ? AND action = 'stage1_empty_recovered'",
            (job_id,),
        )
        assert await cur.fetchone() is not None
