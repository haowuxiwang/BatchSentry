"""Pipeline orchestration 单元测试 — core/pipeline.py。

覆盖：
- 状态机：transition_status 合法/非法转换、不存在的 job、audit_log
- Pipeline 全流程：mock OCR + LLM，验证状态转换到 review、findings 保存、page_cache 填充
- 取消：job 状态预设为 cancelling，验证 pipeline 提前退出
- 错误处理：mock OCR 抛异常，验证 job 状态变为 error
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from core.pipeline import (
    InvalidTransitionError,
    VALID_TRANSITIONS,
    run_pipeline,
    transition_status,
    recover_stuck_jobs,
    launch_pipeline,
    _pipeline_tasks,
)


@pytest_asyncio.fixture
async def pipeline_db(tmp_path):
    """提供带完整 schema 的隔离测试数据库。

    通过 patch config 指向临时文件，重置 db.client._db 全局连接，
    确保 pipeline 内部 get_db() 拿到同一个连接。
    """
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
        if db_mod._db:
            await db_mod._db.close()
        db_mod._db = orig[2]
        _cfg["app"].database_path = orig[0]
        _cfg["app"].output_dir = orig[1]


async def _insert_job(db, job_id="job-1", status="pending", filename="test.pdf"):
    """插入一条 job 记录并 commit，返回 job_id。"""
    await db.execute(
        "INSERT INTO jobs (id, filename, status, pdf_path) VALUES (?, ?, ?, ?)",
        (job_id, filename, status, "/tmp/test.pdf"),
    )
    await db.commit()
    return job_id


# ─── 1. 状态机测试 ──────────────────────────────────────────────


class TestStateMachine:
    """transition_status 合法/非法转换。"""

    @pytest.mark.asyncio
    async def test_valid_transition_pending_to_ocr_running(self, pipeline_db):
        job_id = await _insert_job(pipeline_db, status="pending")
        result = await transition_status(pipeline_db, job_id, "ocr_running", "Stage 1 start")
        assert result == "ocr_running"

        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        assert (await cursor.fetchone())["status"] == "ocr_running"

    @pytest.mark.asyncio
    async def test_valid_transition_ocr_running_to_ocr_done(self, pipeline_db):
        job_id = await _insert_job(pipeline_db, status="pending")
        await transition_status(pipeline_db, job_id, "ocr_running")
        await transition_status(pipeline_db, job_id, "ocr_done", "Stage 1 complete")

        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        assert (await cursor.fetchone())["status"] == "ocr_done"

    @pytest.mark.asyncio
    async def test_valid_transition_chain_to_review(self, pipeline_db):
        """链式合法转换：pending → ocr_running → ocr_done → analyzing → review。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        await transition_status(pipeline_db, job_id, "ocr_running")
        await transition_status(pipeline_db, job_id, "ocr_done")
        await transition_status(pipeline_db, job_id, "analyzing")
        await transition_status(pipeline_db, job_id, "review")

        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        assert (await cursor.fetchone())["status"] == "review"

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self, pipeline_db):
        """非法转换应抛 InvalidTransitionError（pending → review 不在 allowed 集合）。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        with pytest.raises(InvalidTransitionError) as exc_info:
            await transition_status(pipeline_db, job_id, "review")

        assert "pending" in str(exc_info.value)
        assert "review" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_invalid_transition_does_not_update_status(self, pipeline_db):
        """非法转换不应修改 status（pending → review 不在 allowed 集合）。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        with pytest.raises(InvalidTransitionError):
            await transition_status(pipeline_db, job_id, "review")

        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        assert (await cursor.fetchone())["status"] == "pending"

    @pytest.mark.asyncio
    async def test_transition_nonexistent_job_raises(self, pipeline_db):
        """不存在的 job 应抛 InvalidTransitionError。"""
        with pytest.raises(InvalidTransitionError):
            await transition_status(pipeline_db, "does-not-exist", "ocr_running")

    @pytest.mark.asyncio
    async def test_transition_writes_audit_log(self, pipeline_db):
        """合法转换应写 audit_log，包含 from/to 状态与 detail。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        await transition_status(pipeline_db, job_id, "ocr_running", "Stage 1 start")

        cursor = await pipeline_db.execute(
            "SELECT action, detail FROM audit_log WHERE job_id = ? ORDER BY id DESC LIMIT 1",
            (job_id,),
        )
        log = await cursor.fetchone()
        assert log is not None
        assert log["action"] == "status_transition"
        assert "pending" in log["detail"]
        assert "ocr_running" in log["detail"]
        assert "Stage 1 start" in log["detail"]

    @pytest.mark.asyncio
    async def test_valid_transitions_table_covers_all_states(self):
        """VALID_TRANSITIONS 应包含所有关键状态。"""
        for state in (
            "pending",
            "ocr_running",
            "ocr_done",
            "analyzing",
            "review",
            "partial_review",
            "cancelling",
            "cancelled",
            "error",
            "archived",
        ):
            assert state in VALID_TRANSITIONS


# ─── 2. Pipeline 全流程测试（mock OCR + LLM）─────────────────────


class TestPipelineRun:
    """run_pipeline 全流程：mock OCR/LLM，验证状态与持久化。"""

    @pytest.mark.asyncio
    async def test_full_pipeline_reaches_review(self, pipeline_db, tmp_path):
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        fake_pages = [
            {"markdown": {"text": "page 1"}},
            {"markdown": {"text": "page 2"}},
        ]
        fake_page_result = {
            "steps": [],
            "findings": [],
            "overall_confidence": "high",
        }
        fake_findings = [
            {
                "page": 1,
                "type": "test",
                "severity": "warning",
                "description": "test finding",
                "source": "rule",
            }
        ]

        with patch(
            "core.pipeline._get_ocr_backend", return_value=lambda p: fake_pages
        ), patch(
            "core.pipeline.analyze_page", new=AsyncMock(return_value=fake_page_result)
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=fake_findings)
        ):
            await run_pipeline(job_id, pdf_path)

        # 验证状态转换为 review
        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        assert (await cursor.fetchone())["status"] == "review"

        # 验证 findings 已保存
        cursor = await pipeline_db.execute(
            "SELECT page, type, severity, description, source FROM findings WHERE job_id = ?",
            (job_id,),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["page"] == 1
        assert rows[0]["type"] == "test"
        assert rows[0]["severity"] == "warning"
        assert rows[0]["description"] == "test finding"
        assert rows[0]["source"] == "rule"

        # 验证 page_cache 已填充（raw_html + structured_json）
        cursor = await pipeline_db.execute(
            "SELECT page, raw_html, structured_json FROM page_cache "
            "WHERE job_id = ? ORDER BY page",
            (job_id,),
        )
        pages = await cursor.fetchall()
        assert len(pages) == 2
        assert pages[0]["raw_html"] == "page 1"
        assert pages[1]["raw_html"] == "page 2"
        assert pages[0]["structured_json"] is not None
        assert pages[1]["structured_json"] is not None

        parsed = json.loads(pages[0]["structured_json"])
        assert parsed["overall_confidence"] == "high"

        # 验证 total_pages 已更新
        cursor = await pipeline_db.execute(
            "SELECT total_pages FROM jobs WHERE id = ?", (job_id,)
        )
        assert (await cursor.fetchone())["total_pages"] == 2

    @pytest.mark.asyncio
    async def test_pipeline_records_stage_durations(self, pipeline_db, tmp_path):
        """验证 stage1_ms / stage2_ms / stage3_ms 已写入 jobs。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        with patch(
            "core.pipeline._get_ocr_backend",
            return_value=lambda p: [{"markdown": {"text": "x"}}],
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(
                return_value={"steps": [], "findings": [], "overall_confidence": "high"}
            ),
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT stage1_ms, stage2_ms, stage3_ms, failed_pages, finished_at "
            "FROM jobs WHERE id = ?",
            (job_id,),
        )
        row = await cursor.fetchone()
        assert row["stage1_ms"] is not None and row["stage1_ms"] >= 0
        assert row["stage2_ms"] is not None and row["stage2_ms"] >= 0
        assert row["stage3_ms"] is not None and row["stage3_ms"] >= 0
        assert row["failed_pages"] is None  # 无失败页
        assert row["finished_at"] is not None

    @pytest.mark.asyncio
    async def test_pipeline_writes_audit_log_entries(self, pipeline_db, tmp_path):
        """验证 pipeline 关键节点写入 audit_log。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        with patch(
            "core.pipeline._get_ocr_backend",
            return_value=lambda p: [{"markdown": {"text": "page 1"}}],
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(
                return_value={"steps": [], "findings": [], "overall_confidence": "high"}
            ),
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT action FROM audit_log WHERE job_id = ? ORDER BY id",
            (job_id,),
        )
        actions = [row["action"] for row in await cursor.fetchall()]
        assert "pipeline_start" in actions
        assert "stage1_complete" in actions
        assert "stage2_complete" in actions
        assert "pipeline_complete" in actions


# ─── 3. 取消测试 ──────────────────────────────────────────────────


class TestPipelineCancellation:
    """取消场景：job 预设为 cancelling，pipeline 应提前退出。"""

    @pytest.mark.asyncio
    async def test_pipeline_exits_early_when_already_cancelling(
        self, pipeline_db, tmp_path
    ):
        """job 状态为 cancelling 时，pipeline 首个 transition_status 即抛 InvalidTransitionError，
        被内部 except 捕获后提前返回，不应执行任何 OCR/LLM 工作。"""
        job_id = await _insert_job(pipeline_db, status="cancelling")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        ocr_fn_mock = MagicMock(return_value=[{"markdown": {"text": "page 1"}}])
        analyze_page_mock = AsyncMock(
            return_value={"steps": [], "findings": [], "overall_confidence": "high"}
        )
        analyze_cross_page_mock = AsyncMock(return_value=[])

        with patch(
            "core.pipeline._get_ocr_backend", return_value=ocr_fn_mock
        ), patch(
            "core.pipeline.analyze_page", new=analyze_page_mock
        ), patch(
            "core.pipeline.analyze_cross_page", new=analyze_cross_page_mock
        ):
            # pipeline 应吞掉 InvalidTransitionError 并提前返回（不抛异常给调用方）
            await run_pipeline(job_id, pdf_path)

        # 状态应保持 cancelling（ocr_running 转换被阻断）
        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        assert (await cursor.fetchone())["status"] == "cancelling"

        # OCR/LLM 均不应被调用
        ocr_fn_mock.assert_not_called()
        analyze_page_mock.assert_not_called()
        analyze_cross_page_mock.assert_not_called()

        # 不应写入 findings 或 page_cache
        cursor = await pipeline_db.execute(
            "SELECT COUNT(*) AS c FROM findings WHERE job_id = ?", (job_id,)
        )
        assert (await cursor.fetchone())["c"] == 0
        cursor = await pipeline_db.execute(
            "SELECT COUNT(*) AS c FROM page_cache WHERE job_id = ?", (job_id,)
        )
        assert (await cursor.fetchone())["c"] == 0


# ─── 6. 启动恢复 + 优雅关闭测试 ──────────────────────────────


class TestStuckJobRecovery:
    """recover_stuck_jobs — 启动时将卡死的非终态 job 标记为 error。"""

    @pytest.mark.asyncio
    async def test_no_stuck_jobs_returns_zero(self, pipeline_db):
        """无卡死 job 时返回 0，不修改任何记录。"""
        # 插入一个终态 job（不应被恢复）
        await _insert_job(pipeline_db, job_id="done-1", status="review")
        count = await recover_stuck_jobs()
        assert count == 0

    @pytest.mark.asyncio
    async def test_recovers_ocr_running_job(self, pipeline_db):
        """ocr_running 状态的 job 被标记为 error + error_message。"""
        job_id = await _insert_job(pipeline_db, job_id="stuck-1", status="ocr_running")
        count = await recover_stuck_jobs()
        assert count == 1

        cursor = await pipeline_db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        job = await cursor.fetchone()
        assert job["status"] == "error"
        assert "ocr_running" in job["error_message"]
        assert job["finished_at"] is not None

    @pytest.mark.asyncio
    async def test_recovers_multiple_stuck_statuses(self, pipeline_db):
        """不同卡死状态的 job 都被恢复。"""
        await _insert_job(pipeline_db, job_id="s1", status="pending")
        await _insert_job(pipeline_db, job_id="s2", status="ocr_running")
        await _insert_job(pipeline_db, job_id="s3", status="ocr_done")
        await _insert_job(pipeline_db, job_id="s4", status="analyzing")
        await _insert_job(pipeline_db, job_id="s5", status="cancelling")
        # 终态不被恢复
        await _insert_job(pipeline_db, job_id="ok1", status="review")
        await _insert_job(pipeline_db, job_id="ok2", status="archived")

        count = await recover_stuck_jobs()
        assert count == 5

        # 验证终态 job 未被修改
        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = 'ok1'")
        assert (await cursor.fetchone())["status"] == "review"

    @pytest.mark.asyncio
    async def test_recovery_writes_audit_log(self, pipeline_db):
        """恢复操作写入 audit_log。"""
        job_id = await _insert_job(pipeline_db, job_id="stuck-aud", status="analyzing")
        await recover_stuck_jobs()

        cursor = await pipeline_db.execute(
            "SELECT * FROM audit_log WHERE job_id = ? AND action = 'stuck_recovery'",
            (job_id,),
        )
        log = await cursor.fetchone()
        assert log is not None
        assert "analyzing" in log["detail"]
        assert "error" in log["detail"]


class TestLaunchPipeline:
    """launch_pipeline — 创建 task 并注册到 _pipeline_tasks。"""

    @pytest.mark.asyncio
    async def test_launch_registers_task(self, pipeline_db):
        """launch_pipeline 创建 task 并注册。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        # Mock run_pipeline 避免实际执行
        with patch("core.pipeline.run_pipeline", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = None
            task = launch_pipeline(job_id, "/tmp/test.pdf")
            assert job_id in _pipeline_tasks
            assert _pipeline_tasks[job_id] is task
            # 等待 task 完成以避免 warning
            await task
            # task 完成后从注册表移除
            assert job_id not in _pipeline_tasks

    @pytest.mark.asyncio
    async def test_launch_task_callback_removes_on_cancel(self, pipeline_db):
        """task 被取消时从注册表移除。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        with patch("core.pipeline.run_pipeline", new_callable=AsyncMock) as mock_run:
            # 让 run_pipeline 模拟被取消
            mock_run.side_effect = asyncio.CancelledError
            task = launch_pipeline(job_id, "/tmp/test.pdf")
            assert job_id in _pipeline_tasks
            with pytest.raises(asyncio.CancelledError):
                await task
            # 取消后从注册表移除
            assert job_id not in _pipeline_tasks


# ─── 4. 错误处理测试 ──────────────────────────────────────────────


class TestPipelineErrorHandling:
    """OCR 抛异常时，job 状态应转为 error。"""

    @pytest.mark.asyncio
    async def test_ocr_failure_sets_job_to_error(self, pipeline_db, tmp_path):
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        def _boom(pdf_path):
            raise RuntimeError("OCR backend crashed")

        analyze_page_mock = AsyncMock(
            return_value={"steps": [], "findings": [], "overall_confidence": "high"}
        )
        analyze_cross_page_mock = AsyncMock(return_value=[])

        with patch(
            "core.pipeline._get_ocr_backend", return_value=_boom
        ), patch(
            "core.pipeline.analyze_page", new=analyze_page_mock
        ), patch(
            "core.pipeline.analyze_cross_page", new=analyze_cross_page_mock
        ):
            # pipeline 内部捕获异常，不应向上抛出
            await run_pipeline(job_id, pdf_path)

        # 状态应转为 error（ocr_running → error 合法）
        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        assert (await cursor.fetchone())["status"] == "error"

        # Stage 2/3 不应执行
        analyze_page_mock.assert_not_called()
        analyze_cross_page_mock.assert_not_called()

        # 不应写入 findings
        cursor = await pipeline_db.execute(
            "SELECT COUNT(*) AS c FROM findings WHERE job_id = ?", (job_id,)
        )
        assert (await cursor.fetchone())["c"] == 0

        # pipeline_error 应写入 audit_log
        cursor = await pipeline_db.execute(
            "SELECT action, detail FROM audit_log WHERE job_id = ? AND action = ?",
            (job_id, "pipeline_error"),
        )
        log = await cursor.fetchone()
        assert log is not None
        assert "OCR backend crashed" in log["detail"]
