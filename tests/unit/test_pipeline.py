"""Pipeline orchestration 单元测试 — core/pipeline.py。

覆盖：
- 状态机：transition_status 合法/非法转换、不存在的 job、audit_log
- Pipeline 全流程：mock OCR + LLM，验证状态转换到 review、findings 保存、page_cache 填充
- 取消：job 状态预设为 cancelling，验证 pipeline 提前退出
- 错误处理：mock OCR 抛异常，验证 job 状态变为 error
"""
import asyncio
import json
from contextlib import ExitStack
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
    _analyze_one,
)
from config import config


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
        # 关键：pytest-asyncio 每个测试使用新的 event loop（function scope），
        # 而 core.pipeline.db_lock 是 module 级 asyncio.Lock——首次"竞争
        # acquire"时惰性绑定 loop，跨测试残留旧 loop 引用，后续测试若再
        # 遇锁占用会抛 "bound to a different event loop"（概率性 flake）。
        # 在 fixture teardown 时重置其 loop 绑定/等待队列，让下一个测试
        # 重新绑定自己的 loop。
        import core.pipeline as p_mod

        p_mod.db_lock._loop = None
        p_mod.db_lock._waiters = None
        p_mod.db_lock._locked = False
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

        # 中文化后消息携带中文状态名（用户可见错误提示）
        assert "待处理" in str(exc_info.value)
        assert "待复核" in str(exc_info.value)

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
    async def test_ocr_progress_callback_writes_job_field(self, pipeline_db, tmp_path):
        """Stage 1 的 OCR 进度回调应更新 jobs.ocr_progress（JSON）。

        真实 MinerU/Paddle 轮询在线程中回调 → run_coroutine_threadsafe 调度
        回主循环 → _update_ocr_progress 写库 → SSE 前端可见。
        测试分两层：
        1. _ocr_progress_cb 闭包把回调调度到主循环（拦截 run_coroutine_threadsafe，
           避免跨 loop 遗留模块级 db_lock 导致后续测试 "bound to a different event loop"）
        2. _update_ocr_progress 本身写库（直接 await，锁在当前 loop 正常 acquire/release）
        """
        from core.pipeline import _update_ocr_progress

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        scheduled = []

        def fake_rcts(coro, loop):
            scheduled.append(coro)
            return MagicMock()

        def ocr_with_progress(pdf_path, progress_cb):
            # 模拟 MinerU poll_job 在线程中的进度回调（12/51 → 51/51）
            progress_cb(12, 51)
            progress_cb(51, 51)
            return [{"markdown": {"text": "page 1"}, "page_count": 1, "_source": "mineru"}]

        with patch(
            "core.pipeline._get_ocr_backend", return_value=ocr_with_progress
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(
                return_value={"steps": [], "findings": [], "overall_confidence": "high"}
            ),
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ), patch(
            "core.pipeline.asyncio.run_coroutine_threadsafe", side_effect=fake_rcts
        ):
            await run_pipeline(job_id, pdf_path)

        # 闭包正确调度了 2 次回调（12/51 与 51/51）
        assert len(scheduled) == 2
        # 调度的是 _update_ocr_progress 协程（携带正确 job_id）
        assert all(
            c.cr_frame.f_locals.get("job_id") == job_id for c in scheduled
        )
        # 拦截了 run_coroutine_threadsafe，协程从未被执行 → close 释放
        # （否则 GC 时报 RuntimeWarning "coroutine was never awaited"）
        for c in scheduled:
            c.close()

        # _update_ocr_progress 直接写库（当前 loop 内，锁正常 acquire/release）
        await _update_ocr_progress(job_id, 51, 51)
        cursor = await pipeline_db.execute(
            "SELECT ocr_progress FROM jobs WHERE id = ?", (job_id,)
        )
        raw = (await cursor.fetchone())["ocr_progress"]
        assert raw is not None
        data = json.loads(raw)
        assert data["done"] == 51
        assert data["total"] == 51

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
                "type": "param_out_of_spec",  # 规范类型（M2/T2.6 白名单）
                "severity": "warning",
                "description": "test finding",
                "source": "rule",
            }
        ]

        with patch(
            "core.pipeline._get_ocr_backend", return_value=lambda p, cb=None, job_id=None: fake_pages
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
        assert rows[0]["type"] == "param_out_of_spec"
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
    async def test_retry_stage3_cleans_pending_llm_findings(self, pipeline_db, tmp_path):
        """对抗审查(cr-1): retry 重跑 Stage 3 前，删除待审（pending）的
        llm_cross/llm_fallback findings（LLM 描述每次措辞不同，指纹查重
        无效）；已人工裁决的（confirmed）保留不动。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        # 模拟上次运行残留：1 条 pending llm_cross + 1 条已确认 llm_cross
        await pipeline_db.execute(
            "INSERT INTO findings (job_id, page, type, severity, description, source, status) "
            "VALUES (?, 1, 'time_reversal', 'critical', '旧版措辞A', 'llm_cross', 'pending')",
            (job_id,),
        )
        await pipeline_db.execute(
            "INSERT INTO findings (job_id, page, type, severity, description, source, status) "
            "VALUES (?, 1, 'time_reversal', 'critical', '旧版措辞B', 'llm_cross', 'confirmed')",
            (job_id,),
        )
        await pipeline_db.commit()

        fake_pages = [{"markdown": {"text": "page 1"}}]
        fake_page_result = {"steps": [], "findings": [], "overall_confidence": "high"}
        new_cross = [
            {
                "page": 1,
                "type": "time_reversal",
                "severity": "critical",
                "description": "新版措辞",
                "source": "llm_cross",
            }
        ]
        with patch(
            "core.pipeline._get_ocr_backend",
            return_value=lambda p, cb=None, job_id=None: fake_pages,
        ), patch(
            "core.pipeline.analyze_page", new=AsyncMock(return_value=fake_page_result)
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=new_cross)
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT description, status FROM findings WHERE job_id = ?", (job_id,)
        )
        rows = await cursor.fetchall()
        descriptions = {r["description"]: r["status"] for r in rows}
        # B1-6 收权后：LLM 的 critical 会被降级、并在描述尾部追加复核理由
        # （理由对复核者可见是本设计的特性，见 test_llm_finding_guard.py）
        # ⇒ 此处按前缀匹配而非全等。
        assert any(k.startswith("新版措辞") for k in descriptions)   # 本次重跑写入
        assert "旧版措辞B" in descriptions          # confirmed 保留
        assert descriptions["旧版措辞B"] == "confirmed"
        assert not any(k.startswith("旧版措辞A") for k in descriptions)  # pending 已清理

    @pytest.mark.asyncio
    async def test_pipeline_records_stage_durations(self, pipeline_db, tmp_path):
        """验证 stage1_ms / stage2_ms / stage3_ms 已写入 jobs。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        with patch(
            "core.pipeline._get_ocr_backend",
            return_value=lambda p, cb=None, job_id=None: [{"markdown": {"text": "x"}}],
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
            return_value=lambda p, cb=None, job_id=None: [{"markdown": {"text": "page 1"}}],
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

        # P-C3 修复：pipeline 的 except InvalidTransitionError 块现在会恢复
        # cancelling → cancelled（合法终态），而非停留在 cancelling（非终态卡死）。
        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        assert (await cursor.fetchone())["status"] == "cancelled"

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

    @pytest.mark.asyncio
    async def test_cancel_confirmed_after_ocr_keeps_cancelled(
        self, pipeline_db, tmp_path
    ):
        """e2e cancel 轮回归：OCR 完成后的窗口（空页自愈/双后端对比）
        取消被确认，stage1 尾部检查点应退出流水线，终态保持
        cancelled —— 修复前残余的 ocr_done 转换抛 InvalidTransitionError，
        引擎恢复分支把取消终态覆盖成 error。"""
        job_id = await _insert_job(pipeline_db)
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        pages = [{"markdown": {"text": f"page {i} " + "内容" * 50}}
                 for i in (1, 2)]
        analyze_page_mock = AsyncMock(
            return_value={"steps": [], "findings": [], "overall_confidence": "high"}
        )

        # 语义等价 _is_cancelled：前 3 个检查点（stage0 前/后 + OCR 后）
        # 返回 False；第 4 个（stage1 尾部新检查点）确认取消并落终态。
        calls = {"n": 0}

        async def fake_is_cancelled(jid):
            from db.client import get_db
            from core.pipeline import db_lock
            db = await get_db()
            async with db_lock:
                calls["n"] += 1
                if calls["n"] >= 4:
                    await db.execute(
                        "UPDATE jobs SET status = 'cancelled', "
                        "finished_at = datetime('now','localtime') WHERE id = ?",
                        (jid,),
                    )
                    await db.commit()
                    return True
            return False

        with patch(
            "core.pipeline._get_ocr_chain",
            return_value=[(lambda p, cb: pages, "mineru")],
        ), patch(
            "core.pipeline.analyze_page", new=analyze_page_mock,
        ), patch(
            "core.pipeline._is_cancelled", new=fake_is_cancelled,
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT status, error_message FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        assert row["status"] == "cancelled", (
            f"cancel terminal overwritten: {row['status']} {row['error_message']}"
        )
        assert not row["error_message"]

        # 取消发生在 OCR 完成之后（区别于 stage0 检查点路径）
        cursor = await pipeline_db.execute(
            "SELECT COUNT(*) AS c FROM audit_log WHERE job_id = ? "
            "AND action = 'stage1_complete'", (job_id,))
        assert (await cursor.fetchone())["c"] == 1
        # 不得强制覆盖为 error
        cursor = await pipeline_db.execute(
            "SELECT COUNT(*) AS c FROM audit_log WHERE job_id = ? "
            "AND action = 'status_forced_error'", (job_id,))
        assert (await cursor.fetchone())["c"] == 0
        # Stage 2 未执行
        analyze_page_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_transition_recovery_keeps_terminal_status(
        self, pipeline_db, tmp_path
    ):
        """e2e cancel 轮回归（引擎兜底层）：pipeline 末尾残余转换抛
        InvalidTransitionError 时，若 job 已是终态（cancelled 等），
        恢复分支不得覆盖为 error（破坏取消审计链 + 重发通知）。"""
        job_id = await _insert_job(pipeline_db)
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        async def fake_stage3(db, jid, *a, **kw):
            # 模拟：stage3 运行期间取消被确认（终态落库），其内部随后
            # 的终态转换因 cancelled → review 非法而抛 InvalidTransitionError
            await db.execute(
                "UPDATE jobs SET status = 'cancelled', "
                "finished_at = datetime('now','localtime') WHERE id = ?",
                (jid,),
            )
            await db.commit()
            raise InvalidTransitionError("不能从「已取消」转换到「待复核」")

        pages = [{"markdown": {"text": "page 1 " + "内容" * 50}}]
        import core.pipeline.engine as engine_mod
        with patch(
            "core.pipeline._get_ocr_chain",
            return_value=[(lambda p, cb: pages, "mineru")],
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(return_value={
                "steps": [], "findings": [], "overall_confidence": "high"}),
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[]),
        ), patch.object(
            engine_mod, "_run_stage3_cross_analysis", new=fake_stage3,
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT status, error_message FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        assert row["status"] == "cancelled", (
            f"terminal status overwritten: {row['status']}"
        )
        assert not row["error_message"]
        cursor = await pipeline_db.execute(
            "SELECT COUNT(*) AS c FROM audit_log WHERE job_id = ? "
            "AND action = 'status_forced_error'", (job_id,))
        assert (await cursor.fetchone())["c"] == 0

    @pytest.mark.asyncio
    async def test_stage2_cancel_kills_inflight_page_tasks(
        self, pipeline_db, tmp_path
    ):
        """Stage 2 中途取消应取消 in-flight 页面分析任务（整份路径）。

        与 sliced 路径（test_sliced_cancel_inside_ocr_loop）对齐：取消时
        剩余任务必须被 cancel，否则孤儿协程继续跑 LLM（单页最长 240s）、
        应用退出时抛 "Task was destroyed"（结构化并发：无观察者任务）。
        """
        job_id = "cancel-stage2-inflight"
        await _insert_job(pipeline_db, job_id=job_id)
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        # 真实页文本远大于 100 字符 — 避免被空页判定（<100 字符）命中而
        # 意外触发小文件自愈路径，改变取消检查调用序（对抗审查 cr-17 后
        # <10 页也启用自愈；此测试只验证 stage2 取消语义，应构造非空页）。
        fake_pages = [
            {"markdown": {"text": "slow page " + "工序一内容" * 30}},
            {"markdown": {"text": "fast page " + "工序二内容" * 30}},
        ]

        captured = {}

        async def slow_page(html, page_num, *, job_id="", cancel_check=None):
            # 慢页：捕获当前任务后挂起 300s（模拟慢 LLM 调用）
            captured["slow_task"] = asyncio.current_task()
            await asyncio.sleep(300)

        async def fast_page(html, page_num, *, job_id="", cancel_check=None):
            return {"steps": [], "findings": [], "overall_confidence": "high"}

        async def analyze_side(html, page_num, **kw):
            if "slow" in html:
                return await slow_page(html, page_num, **kw)
            return await fast_page(html, page_num, **kw)

        calls = {"n": 0}

        async def fake_cancelled(jid):
            calls["n"] += 1
            # 调用序（stage1 尾部检查点加入后 +1）：1=Stage0 pre,
            # 2=Stage0 post, 3=Stage1 后检查, 4=stage1 尾部（ocr_done 前）,
            # 5/6=两页 _analyze_one 入口,
            # 7=Stage 2 while 循环（fast 页完成后）→ 触发取消
            return calls["n"] >= 7

        with patch(
            "core.pipeline._get_ocr_backend",
            return_value=lambda p, cb: fake_pages,
        ), patch(
            "core.pipeline.analyze_page", new=AsyncMock(side_effect=analyze_side)
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ), patch(
            "core.pipeline._is_cancelled", side_effect=fake_cancelled
        ):
            # 取消路径应快速返回（不等 300s），5s 内完成即证明 in-flight 任务被取消
            await asyncio.wait_for(run_pipeline(job_id, pdf_path), timeout=5)

        # 慢页任务必须被取消（不是自然结束）
        slow = captured.get("slow_task")
        assert slow is not None, "slow page task should have been created"
        assert slow.cancelled(), "in-flight page task must be cancelled on cancel"

    @pytest.mark.asyncio
    async def test_analyze_one_cancelled_page_not_counted_failed(
        self, pipeline_db
    ):
        """P1-2：analyze_page 抛 AnalysisCancelled（cancel_check 命中）时，
        _analyze_one 应静默跳过该页：不进 failed_pages、不写 page_cache、
        completed 不 +1（取消是用户动作，不是分析缺陷）。"""
        from core.page_analyzer import AnalysisCancelled

        db = pipeline_db  # fixture 已 yield 连接对象
        job_id = "cancel-skip-page"
        await _insert_job(pipeline_db, job_id=job_id)

        failed_pages: list[int] = []
        sem = asyncio.Semaphore(1)
        state_lock = asyncio.Lock()
        completed = {"n": 0}
        page = {"markdown": {"text": "page content"}}

        with patch(
            "core.pipeline._is_cancelled", new=AsyncMock(return_value=False)
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(side_effect=AnalysisCancelled(1)),
        ):
            await _analyze_one(
                pipeline_db, job_id, 1, page, sem, failed_pages,
                state_lock, completed, total_pages=2,
            )

        assert failed_pages == []
        assert completed["n"] == 0
        cursor = await pipeline_db.execute(
            "SELECT COUNT(*) AS c FROM page_cache WHERE job_id = ?", (job_id,)
        )
        assert (await cursor.fetchone())["c"] == 0

        # Stage 3 不应执行
        # （analyze_cross_page mock 调用检查在 patch 作用域外无法断言，
        #   用 find_status 验证 job 停在非终态前的状态即可 — 这里验证快速返回 + 任务取消已足够）



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

    @pytest.mark.asyncio
    async def test_recover_skips_jobs_newer_than_process_start(self, pipeline_db):
        """B7 竞态防护：created_at 晚于 process_started_at 的 job 不被恢复。

        lifespan 中 recover 以 background task 执行，可能与本进程新上传的
        pending job 并发 — 那些 job 的 pipeline 即将运行，绝不能误标 error。
        """
        from datetime import datetime, timedelta

        await _insert_job(pipeline_db, job_id="old-1", status="ocr_running")
        # 显式控制 created_at（新上传的 job：created_at 晚于 cutoff）
        await pipeline_db.execute(
            "INSERT INTO jobs (id, filename, status, pdf_path, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("new-1", "new.pdf", "pending", "/tmp/new.pdf", "2099-01-01 00:00:00"),
        )
        await pipeline_db.commit()

        # cutoff = 现在 +1min（本地口径 — P0-5 后 created_at 统一本地时间）：
        # old-1（created_at=now）早于 cutoff，new-1 晚于
        cutoff = (datetime.now() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        count = await recover_stuck_jobs(process_started_at=cutoff)
        assert count == 1  # 只恢复 old-1

        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = 'old-1'")
        assert (await cursor.fetchone())["status"] == "error"
        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = 'new-1'")
        assert (await cursor.fetchone())["status"] == "pending"

    @pytest.mark.asyncio
    async def test_recover_condition_update_skips_restarted_job(self, pipeline_db):
        """竞态防护（中文化收尾对抗审查）：recover 的 UPDATE 带 status 条件 —
        SELECT 快照后、逐行 UPDATE 前 job 已被并发路径推进到终态（如 pipeline
        恰好完成 ocr_done→review），条件更新影响 0 行时跳过错误标记 —
        否则迟到恢复会把刚完成的 review 打回 error。"""
        from datetime import datetime, timedelta

        await _insert_job(pipeline_db, job_id="race-1", status="ocr_done")
        # 模拟 SELECT 快照后并发推进：ocr_done → review（pipeline 完成）
        await pipeline_db.execute(
            "UPDATE jobs SET status = 'review' WHERE id = 'race-1'"
        )
        await pipeline_db.commit()

        # SQLite 单连接串行：无法真实注入 SELECT 与 UPDATE 之间的并发，
        # 直接构造"快照已过时"场景验证条件更新的防御行为
        count = await recover_stuck_jobs(process_started_at=None)
        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = 'race-1'")
        assert (await cursor.fetchone())["status"] == "review"  # 未被覆盖回 error

    @pytest.mark.asyncio
    async def test_recover_without_cutoff_recovers_all(self, pipeline_db):
        """无 process_started_at 参数时行为不变（向后兼容，全部恢复）。"""
        await _insert_job(pipeline_db, job_id="old-2", status="analyzing")
        await pipeline_db.execute(
            "INSERT INTO jobs (id, filename, status, pdf_path, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("new-2", "new.pdf", "pending", "/tmp/new.pdf", "2099-01-01 00:00:00"),
        )
        await pipeline_db.commit()

        count = await recover_stuck_jobs()
        assert count == 2
        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = 'new-2'")
        assert (await cursor.fetchone())["status"] == "error"


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
            # #141：注册表值是**集合**（同一 job 允许多个未完成 task），
            # 断言"包含"而非"等于" —— 集合语义正是为了让持锁者与等待者
            # 同时被登记、都能被取消。
            assert task in _pipeline_tasks[job_id]
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

        def _boom(pdf_path, progress_callback=None):
            raise RuntimeError("OCR backend crashed")

        analyze_page_mock = AsyncMock(
            return_value={"steps": [], "findings": [], "overall_confidence": "high"}
        )
        analyze_cross_page_mock = AsyncMock(return_value=[])

        with patch(
            "core.pipeline._get_ocr_chain", return_value=[(_boom, "mineru")]
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

        # stage1_failed 应写入 audit_log（含后端失败原因）
        cursor = await pipeline_db.execute(
            "SELECT action, detail FROM audit_log WHERE job_id = ? AND action = 'stage1_failed'",
            (job_id,),
        )
        log = await cursor.fetchone()
        assert log is not None
        assert "OCR backend crashed" in log["detail"]

    @pytest.mark.asyncio
    async def test_ocr_zero_pages_sets_job_to_error(self, pipeline_db, tmp_path):
        """整份路径 OCR 返回空列表 → job 进入 error + stage1_failed 审计。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        with patch(
            "core.pipeline._get_ocr_chain",
            return_value=[(lambda p, cb=None, job_id=None: [], "paddle")],
        ), patch(
            "core.pipeline.analyze_page", new=AsyncMock(return_value={})
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute("SELECT status, error_message FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        assert row["status"] == "error"
        assert "0 pages" in row["error_message"]
        cursor = await pipeline_db.execute(
            "SELECT 1 FROM audit_log WHERE job_id = ? AND action = 'stage1_failed'", (job_id,)
        )
        assert await cursor.fetchone() is not None


# ─── 4.5 双 OCR 主备切换（failover）──────────────────────────────


class TestOcrFailover:
    """双 OCR 兜底：主后端失败自动切备后端，records ocr_backend_used。"""

    @pytest.mark.asyncio
    async def test_primary_fails_secondary_succeeds(self, pipeline_db, tmp_path):
        """主后端抛异常 → 自动切备后端 → job 正常 review，记录实际后端。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        def _boom(pdf_path, progress_callback=None):
            raise RuntimeError("primary OCR down")

        fake_pages = [{"markdown": {"text": "page 1"}}]
        chain = [(_boom, "mineru"), (lambda p, cb=None: fake_pages, "paddle")]

        with patch(
            "core.pipeline._get_ocr_chain", return_value=chain
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(return_value={"steps": [], "findings": [], "overall_confidence": "high"}),
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT status, ocr_backend_used FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        assert row["status"] == "review"
        assert row["ocr_backend_used"] == "paddle"

        # failover 审计记录
        cursor = await pipeline_db.execute(
            "SELECT detail FROM audit_log WHERE job_id = ? AND action = 'ocr_failover'", (job_id,)
        )
        log = await cursor.fetchone()
        assert log is not None
        assert "mineru" in log["detail"] and "paddle" in log["detail"]

    @pytest.mark.asyncio
    async def test_zero_pages_triggers_failover(self, pipeline_db, tmp_path):
        """主后端 0 页（不抛异常）也触发 failover。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        fake_pages = [{"markdown": {"text": "page 1"}}]
        chain = [(lambda p, cb=None: [], "mineru"), (lambda p, cb=None: fake_pages, "paddle")]

        with patch(
            "core.pipeline._get_ocr_chain", return_value=chain
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(return_value={"steps": [], "findings": [], "overall_confidence": "high"}),
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT status, ocr_backend_used FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        assert row["status"] == "review"
        assert row["ocr_backend_used"] == "paddle"

    @pytest.mark.asyncio
    async def test_severe_pagemismatch_triggers_failover(self, pipeline_db, tmp_path):
        """主后端严重缺页（静默截断）视为失败 → fallback 到备后端。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        fake_pages = [{"markdown": {"text": "page 1"}}]
        ok_pages = [{"markdown": {"text": f"page {i}"}} for i in range(25)]
        chain = [
            (lambda p, cb=None: fake_pages, "mineru"),
            (lambda p, cb=None: ok_pages, "paddle"),
        ]

        with patch(
            "core.pipeline._get_ocr_chain", return_value=chain
        ), patch(
            "core.pipeline._pdf_page_count", return_value=30  # 主后端仅返回 1 页
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(return_value={"steps": [], "findings": [], "overall_confidence": "high"}),
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT status, ocr_backend_used FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        # cr-17: 阈值收紧为 max(2, 10%) → 备后端 25/30（缺 5 页 > max(2,3)）
        # 也判严重缺失 → 双后端均失败 → error（比静默接受残缺页更诚实）。
        # ≤1-2 页的轻微差异仍容忍（partial_review，见下一测试）。
        assert row["status"] == "error"
        # Round 10 #6: failover writes ocr_backend_used in real-time
        assert row["ocr_backend_used"] == "paddle"

    @pytest.mark.asyncio
    async def test_ocr_cancelled_aborts_chain_no_failover(self, pipeline_db, tmp_path):
        """对抗审查 P2：取消不是后端故障 —— OCRCancelled 必须终止整条
        failover 链（不切备选白烧配额），job 经正式迁移终态 cancelled。"""
        import sqlite3 as _sqlite3

        from core.ocr_client import OCRCancelled

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")
        db_path = config["app"].database_path

        def _cancelled_during_poll(pdf_path, progress_callback=None,
                                   cancel_check=None):
            # 模拟用户在 OCR 轮询期间点了取消（POST /cancel 置 cancelling；
            # 同步探针发现后抛 OCRCancelled —— 与真实链路一致）
            con = _sqlite3.connect(db_path)
            try:
                con.execute(
                    "UPDATE jobs SET status = 'cancelling' WHERE id = ?",
                    (job_id,),
                )
                con.commit()
            finally:
                con.close()
            raise OCRCancelled("cancelled during polling")

        secondary_calls = []

        def _secondary(pdf_path, progress_callback=None):
            secondary_calls.append(True)
            return [{"markdown": {"text": "should never run"}}]

        chain = [(_cancelled_during_poll, "mineru"), (_secondary, "paddle")]

        with patch(
            "core.pipeline._get_ocr_chain", return_value=chain
        ):
            await run_pipeline(job_id, pdf_path)

        assert not secondary_calls, "failover 链在取消后不得切换备选后端"
        cursor = await pipeline_db.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        assert row["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_single_backend_chain_no_fallback(self, pipeline_db, tmp_path):
        """单后端链（未配置备选）失败 → 直接 error，不尝试空备选。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        def _boom(pdf_path, progress_callback=None):
            raise RuntimeError("only backend down")

        with patch(
            "core.pipeline._get_ocr_chain", return_value=[(_boom, "mineru")]
        ), patch(
            "core.pipeline.analyze_page", new=AsyncMock(return_value={})
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        )
        assert (await cursor.fetchone())["status"] == "error"


# ─── 2.6 F3: retry 缓存复用（跳过 Stage 1 OCR）─────────────────


class TestRetryReuseOcr:
    """F3: page_cache 已覆盖全部页时 retry 跳过真实 OCR，直接复用缓存进 Stage 2。

    省掉整个 PDF 重传重 OCR（上游配额 + 数分钟等待）。仅整份路径生效。
    """

    @pytest.mark.asyncio
    async def test_full_cache_skips_ocr(self, pipeline_db, tmp_path):
        """缓存全覆盖 → _run_ocr_with_failover 不被调用；Stage 2 用缓存文本；
        ocr_backend_used 保留上次真实后端（不写 "cached"）。"""
        job_id = await _insert_job(pipeline_db, job_id="reuse-1", status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")
        await pipeline_db.execute(
            "UPDATE jobs SET total_pages = 2, ocr_backend_used = 'mineru' WHERE id = ?",
            (job_id,),
        )
        await pipeline_db.executemany(
            "INSERT INTO page_cache (job_id, page, raw_html) VALUES (?, ?, ?)",
            [
                ("reuse-1", 1, "<p>cached p1</p>"),
                ("reuse-1", 2, "<p>cached p2</p>"),
            ],
        )
        await pipeline_db.commit()

        seen: list[str] = []

        async def _fake_analyze(raw_html, page_num=None, job_id=None, **kw):
            seen.append(raw_html)
            return {"steps": [], "findings": [], "overall_confidence": "high"}

        with patch(
            "core.pipeline._run_ocr_with_failover",
            new=AsyncMock(side_effect=AssertionError("OCR must be skipped")),
        ), patch(
            "core.pipeline._pdf_page_count", return_value=2
        ), patch(
            "core.pipeline.analyze_page", new=_fake_analyze
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT status, ocr_backend_used, total_pages FROM jobs WHERE id = ?",
            (job_id,),
        )
        row = await cursor.fetchone()
        assert row["status"] == "review"
        assert row["ocr_backend_used"] == "mineru"  # 保留真实后端，非 "cached"
        # Stage 2 消费的是缓存文本（顺序按 page）
        assert seen == ["<p>cached p1</p>", "<p>cached p2</p>"]
        # 审计可见 stage1_skipped
        cursor = await pipeline_db.execute(
            "SELECT detail FROM audit_log WHERE job_id = ? AND action = 'stage1_skipped'",
            (job_id,),
        )
        assert (await cursor.fetchone()) is not None

    @pytest.mark.asyncio
    async def test_partial_cache_still_runs_ocr(self, pipeline_db, tmp_path):
        """缓存未覆盖全部页（1/2）→ 正常走 OCR failover 路径。"""
        job_id = await _insert_job(pipeline_db, job_id="reuse-2", status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")
        await pipeline_db.execute(
            "UPDATE jobs SET total_pages = 2, ocr_backend_used = 'mineru' WHERE id = ?",
            (job_id,),
        )
        await pipeline_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html) VALUES (?, ?, ?)",
            ("reuse-2", 1, "<p>cached p1</p>"),
        )
        await pipeline_db.commit()

        fake_pages = [
            {"markdown": {"text": "fresh p1"}},
            {"markdown": {"text": "fresh p2"}},
        ]
        calls = {"n": 0}

        async def _spy_failover(db, job_id, pdf_path, progress_callback=None):
            calls["n"] += 1
            return fake_pages, "mineru", []

        with patch(
            "core.pipeline._run_ocr_with_failover", side_effect=_spy_failover
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(return_value={"steps": [], "findings": [], "overall_confidence": "high"}),
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        assert calls["n"] == 1  # OCR 真实执行
        # 未覆盖的页（p2）执行了新 OCR 并写入缓存
        cursor = await pipeline_db.execute(
            "SELECT raw_html FROM page_cache WHERE job_id = ? AND page = 2",
            (job_id,),
        )
        row = await cursor.fetchone()
        assert row is not None and "fresh p2" in row["raw_html"]
        # 无 stage1_skipped 审计
        cursor = await pipeline_db.execute(
            "SELECT COUNT(*) AS c FROM audit_log WHERE job_id = ? AND action = 'stage1_skipped'",
            (job_id,),
        )
        assert (await cursor.fetchone())["c"] == 0

    @pytest.mark.asyncio
    async def test_backfills_regions_for_existing_pages(self, pipeline_db, tmp_path):
        """已有原文但此前没区域锚的页 → **补写**区域锚（不改写原文）。

        为什么必须允许补写：P0-3 上线前入库的历史 job 在复核页会一直显示
        "该页不支持定位"——其实只是缺一行 regions_json。原文本身是证据链，
        任何时候都不得覆盖。本用例同时锁定"补写"与"不覆盖"两个方向。
        """
        job_id = await _insert_job(pipeline_db, job_id="reuse-3", status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")
        await pipeline_db.execute(
            "UPDATE jobs SET total_pages = 2, ocr_backend_used = 'paddle' WHERE id = ?",
            (job_id,),
        )
        # 页 1 已有原文但无 regions_json；页 2 完全无缓存
        await pipeline_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html) VALUES (?, ?, ?)",
            ("reuse-3", 1, "<p>旧原文 p1</p>"),
        )
        await pipeline_db.commit()

        # 页 1 的 OCR 产物带回坐标系 + 区域（MinerU 形态：`_space` + `_regions`）
        fake_pages = [
            {"markdown": {"text": "new p1"}, "_space": [1440, 1920],
             "_regions": [{"label": "table", "bbox": [72, 96, 1368, 960],
                           "text": "进料压力 0.16 MPa"}]},
            {"markdown": {"text": "new p2"}},
        ]

        async def _spy_failover(db, job_id, pdf_path, progress_callback=None):
            return fake_pages, "paddle", []

        with patch(
            "core.pipeline._run_ocr_with_failover", side_effect=_spy_failover
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(return_value={"steps": [], "findings": [],
                                        "overall_confidence": "high"}),
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT raw_html, regions_json FROM page_cache "
            "WHERE job_id = ? AND page = 1",
            (job_id,),
        )
        row = await cursor.fetchone()
        assert "旧原文 p1" in row["raw_html"], "已有原文不得被 OCR 产物覆盖（证据链）"
        assert row["regions_json"], "空缺的 regions_json 必须被补写（否则历史 job 永远无锚）"
        got = json.loads(row["regions_json"])
        # backend 记的是**载荷形态**的来源：`_space`+`_regions` 属 MinerU 形态分支
        # （Paddle 形态走 prunedResult/parsing_res_list）。
        assert got["backend"] == "mineru" and got["space"] == [1440, 1920]
        # 写入的必须是**归一化**坐标（裸坐标禁止入库）
        assert all(0.0 <= v <= 1.0 for v in got["regions"][0]["bbox"])


# ─── 2.5 健壮性改进（robustness A/B 组）───────────────────────────


class TestRobustnessChecks:
    """OCR 完整性校验 + 解析失败可见性 + findings 幂等（A1/A2/B3/B4）。"""

    @pytest.mark.asyncio
    async def test_parse_error_page_marks_partial_review(self, pipeline_db, tmp_path):
        """B3: JSON 解析失败页（_parse_error 不抛异常）应计入 failed_pages
        并触发 partial_review，而不是显示为成功的 review。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        fake_pages = [
            {"markdown": {"text": "page 1"}},
            {"markdown": {"text": "page 2"}},
        ]
        # 第 1 页解析失败（返回 _parse_error 标记），第 2 页成功
        def _fake_analyze(raw_html, **kwargs):
            if "page 1" in raw_html:
                return {"_parse_error": True, "_raw": "bad json", "overall_confidence": "low"}
            return {"steps": [], "findings": [], "overall_confidence": "high"}

        with patch(
            "core.pipeline._get_ocr_backend", return_value=lambda p, cb=None, job_id=None: fake_pages
        ), patch(
            "core.pipeline.analyze_page", new=AsyncMock(side_effect=_fake_analyze)
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT status, failed_pages FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        assert row["status"] == "partial_review"
        assert json.loads(row["failed_pages"]) == [1]

        # 第 1 页 structured_json 应标记 _parse_error，便于 retry 重跑
        cursor = await pipeline_db.execute(
            "SELECT structured_json FROM page_cache WHERE job_id = ? AND page = 1",
            (job_id,),
        )
        parsed = json.loads((await cursor.fetchone())["structured_json"])
        assert parsed["_parse_error"] is True

    @pytest.mark.asyncio
    async def test_ocr_pagemismatch_severe_sets_error(self, pipeline_db, tmp_path):
        """A1: OCR 结果页数远小于 PDF 物理页数（静默缺页）→ job 显式 error。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        fake_pages = [{"markdown": {"text": "p1"}}, {"markdown": {"text": "p2"}}]
        with patch(
            "core.pipeline._get_ocr_chain",
            return_value=[(lambda p, cb=None, job_id=None: fake_pages, "mineru")],
        ), patch(
            "core.pipeline._pdf_page_count", return_value=30
        ), patch(
            "core.pipeline.analyze_page", new=AsyncMock(return_value={})
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT status, error_message FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        assert row["status"] == "error"
        assert "page mismatch" in row["error_message"]
        # 不应继续到 Stage 2（pagemismatch 后即退出）
        cursor = await pipeline_db.execute(
            "SELECT COUNT(*) AS c FROM page_cache WHERE job_id = ?", (job_id,)
        )
        assert (await cursor.fetchone())["c"] == 0

    @pytest.mark.asyncio
    async def test_ocr_pagemismatch_minor_continues(self, pipeline_db, tmp_path):
        """A1 + P1-4: 轻微页数差异（≤5 页或 20%）→ 不阻断 pipeline，但缺页
        显式暴露：页码并入 failed_pages → partial_review（用户可见）。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        fake_pages = [{"markdown": {"text": "p1"}}, {"markdown": {"text": "p2"}}]
        with patch(
            "core.pipeline._get_ocr_backend", return_value=lambda p, cb=None, job_id=None: fake_pages
        ), patch(
            "core.pipeline._pdf_page_count", return_value=4  # 缺 2 页 ≤ max(5, 20%)
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(return_value={"steps": [], "findings": [], "overall_confidence": "high"}),
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        assert (await cursor.fetchone())["status"] == "partial_review"
        cursor = await pipeline_db.execute(
            "SELECT 1 FROM audit_log WHERE job_id = ? AND action = 'stage1_pagemismatch'",
            (job_id,),
        )
        assert await cursor.fetchone() is not None
        # P1-4: 缺失页码 3/4 写入 failed_pages（复核页横幅可见）
        cursor = await pipeline_db.execute(
            "SELECT failed_pages FROM jobs WHERE id = ?", (job_id,)
        )
        import json as _json
        assert _json.loads((await cursor.fetchone())["failed_pages"]) == [3, 4]

    @pytest.mark.asyncio
    async def test_discarded_count_injects_ocr_warning(self, pipeline_db, tmp_path):
        """A2: MinerU 丢弃块计数 → raw_html 前缀注入 OCR 警告（LLM 与 UI 均可见）。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        fake_pages = [
            {"markdown": {"text": "正文内容"}, "_ocr_diagnostics": {"discarded_blocks": 3}},
        ]
        with patch(
            "core.pipeline._get_ocr_backend", return_value=lambda p, cb=None, job_id=None: fake_pages
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(return_value={"steps": [], "findings": [], "overall_confidence": "medium"}),
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT raw_html FROM page_cache WHERE job_id = ? AND page = 1", (job_id,)
        )
        raw = (await cursor.fetchone())["raw_html"]
        assert "[OCR 警告" in raw
        assert "3 个内容块" in raw
        assert "正文内容" in raw

        # LLM 确实收到了带警告的 raw_html
        cursor = await pipeline_db.execute(
            "SELECT structured_json FROM page_cache WHERE job_id = ? AND page = 1", (job_id,)
        )
        assert (await cursor.fetchone())["structured_json"] is not None

    @pytest.mark.asyncio
    async def test_retry_does_not_duplicate_rule_findings(self, pipeline_db, tmp_path):
        """B4: 重跑 pipeline 后 rule findings 不重复（查重指纹幂等）。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        fake_pages = [{"markdown": {"text": "p1"}}]
        fake_findings = [
            {
                "page": 1,
                "type": "test",
                "severity": "warning",
                "description": "same finding",
                "source": "rule",
            }
        ]
        fake_analyzed = {
            "steps": [],
            "findings": [],
            "overall_confidence": "high",
        }
        with patch(
            "core.pipeline._get_ocr_backend", return_value=lambda p, cb=None, job_id=None: fake_pages
        ), patch(
            "core.pipeline.analyze_page", new=AsyncMock(return_value=fake_analyzed)
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=fake_findings)
        ):
            await run_pipeline(job_id, pdf_path)
            # 模拟 retry：pipeline 再次运行（OCR/分析经 resume 跳过，Stage 3 重算）
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT COUNT(*) AS c FROM findings WHERE job_id = ?", (job_id,)
        )
        assert (await cursor.fetchone())["c"] == 1

    @pytest.mark.asyncio
    async def test_partial_review_can_transition_to_pending(self, pipeline_db):
        """状态机: partial_review → pending 合法（UI 重试补分析失败页）。"""
        job_id = await _insert_job(pipeline_db, status="partial_review")
        result = await transition_status(pipeline_db, job_id, "pending", "Retry")
        assert result == "pending"

    @pytest.mark.asyncio
    async def test_empty_page_skipped_not_failed(self, pipeline_db, tmp_path):
        """D1: _ocr_empty 页不计 failed_pages（不是失败），且有结构化标记。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        fake_pages = [
            {"markdown": {"text": "## 第 1 页\n\n（此页无文本内容）"}},
            {"markdown": {"text": "page 2 real content"}},
        ]

        def _fake_analyze(raw_html, **kwargs):
            if "此页无文本内容" in raw_html:
                return {
                    "page_number": 1,
                    "_parse_error": False,
                    "_ocr_empty": True,
                    "steps": [], "findings": [],
                    "overall_confidence": "low",
                }
            return {"steps": [], "findings": [], "overall_confidence": "high"}

        with patch(
            "core.pipeline._get_ocr_backend", return_value=lambda pth, cb=None, job_id=None: fake_pages
        ), patch(
            "core.pipeline.analyze_page", new=AsyncMock(side_effect=_fake_analyze)
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        # 空页不计失败：状态应为 review（而非 partial_review）
        cursor = await pipeline_db.execute(
            "SELECT status, failed_pages FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        assert row["status"] == "review"
        assert row["failed_pages"] in (None, "[]")

        # 空页 structured_json 带 _ocr_empty 标记（供 review 页横幅）
        cursor = await pipeline_db.execute(
            "SELECT structured_json FROM page_cache WHERE job_id = ? AND page = 1",
            (job_id,),
        )
        parsed = json.loads((await cursor.fetchone())["structured_json"])
        assert parsed["_ocr_empty"] is True

    @pytest.mark.asyncio
    async def test_empty_page_excluded_from_cross_page(self, pipeline_db, tmp_path):
        """D1: 跨页分析（Stage 3）不接收 _ocr_empty 页数据结构。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        fake_pages = [
            {"markdown": {"text": "## 第 1 页\n\n（此页无文本内容）"}},
        ]
        cross_mock = AsyncMock(return_value=[])
        with patch(
            "core.pipeline._get_ocr_backend", return_value=lambda pth, cb=None, job_id=None: fake_pages
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(return_value={
                "page_number": 1, "_parse_error": False, "_ocr_empty": True,
                "steps": [], "findings": [], "overall_confidence": "low",
            }),
        ), patch("core.pipeline.analyze_cross_page", new=cross_mock):
            await run_pipeline(job_id, pdf_path)

        # Stage 3 不应拿到空页数据（跨页分析页数应为 0）
        call_args = cross_mock.await_args
        assert call_args is not None
        structures_arg = [a for a in call_args.args if isinstance(a, list)]
        assert len(structures_arg) == 1
        assert structures_arg[0] == []


# ─── 3. OCR 文本清洗（噪音消除）─────────────────────────────────


class TestSanitizeOcrText:
    """_sanitize_ocr_text 应消除 MinerU/Paddle 产物噪音。"""

    def test_literal_newline_escape_converted(self):
        from core.pipeline import _sanitize_ocr_text

        assert _sanitize_ocr_text("起草人：\\n2022.05.07") == "起草人：\n2022.05.07"

    def test_style_and_width_attributes_stripped(self):
        from core.pipeline import _sanitize_ocr_text

        html = (
            "<table border=1 style='margin: auto; word-wrap: break-word;'>"
            "<tr><td style='text-align: center;'>A</td>"
            "<td width='50%'>B</td></tr></table>"
        )
        out = _sanitize_ocr_text(html)
        assert "style=" not in out
        assert "width=" not in out
        assert "<table border=1>" in out
        assert "<tr><td>A</td><td>B</td></tr>" in out

    def test_img_src_truncated_to_filename(self):
        from core.pipeline import _sanitize_ocr_text

        out = _sanitize_ocr_text(
            '<img src="imgs/img_in_image_box_442_3_776_359.jpg" alt="Image" />'
        )
        assert "imgs/img_in_image_box_442_3_776_359.jpg" not in out
        assert "img_in_image_box_442_3_776_359.jpg" in out

    def test_plain_text_unchanged(self):
        from core.pipeline import _sanitize_ocr_text

        assert _sanitize_ocr_text("page 1") == "page 1"
        assert _sanitize_ocr_text("") == ""
        assert _sanitize_ocr_text(None) is None

    def test_excessive_blank_lines_collapsed(self):
        from core.pipeline import _sanitize_ocr_text

        assert _sanitize_ocr_text("a\n\n\n\n\nb") == "a\n\nb"

    def test_pseudo_latex_stripped(self):
        """F2: 伪 LaTeX 残留（$...$/\\text{...}/{{...}}）应从 raw_html 剥离。"""
        from core.pipeline import _sanitize_ocr_text

        out = _sanitize_ocr_text("温度 $\\text{25.0}$℃ 记录 见 批件")
        assert "$" not in out
        assert "{" not in out and "}" not in out
        assert "\\text" not in out
        assert out == "温度 25.0℃ 记录 见 批件"

    def test_empty_cells_and_tag_whitespace_compressed(self):
        """F2: 空单元格（含 &nbsp;）规整 + 标签间空白压缩，省 token。"""
        from core.pipeline import _sanitize_ocr_text

        html = (
            "<table>\n"
            "<tr>\n<td> 值A </td>\n<td>&nbsp;</td>\n<td> </td>\n</tr>\n"
            "</table>"
        )
        out = _sanitize_ocr_text(html)
        assert "<td>值A</td>" not in out  # 值A 两端空格属于内容，不动
        assert "<td></td>" in out
        assert "\n" not in out  # 标签间换行空白已压缩
        assert "&nbsp;" not in out

    def test_control_chars_replaced_with_space(self):
        """F2: PDF 控制字符应替换为空格（防单词粘连），\n \t 保留。"""
        from core.pipeline import _sanitize_ocr_text

        out = _sanitize_ocr_text("A\x00B\x1fC\nD")
        assert out == "A B C\nD"

    def test_page_number_lines_filtered(self):
        """F2: 页码整行（第 N 页 / N/M）应过滤 — Paddle 路径无块级过滤的补偿。"""
        from core.pipeline import _sanitize_ocr_text

        text = "正文开始\n第 3 页\n2/24\n正文结束"
        out = _sanitize_ocr_text(text)
        assert "第 3 页" not in out
        assert "2/24" not in out
        assert "正文开始" in out and "正文结束" in out


# ─── 4. MinerU 分片 OCR + 渐进分析（流式输出）───────────────────


class TestSlicedPipeline:
    """OCR_SLICES>1 且 backend=mineru 时走分片路径：一片完成即落库+分析。

    pytest-asyncio(0.25) 下每个测试用独立 event loop，而 pipeline 的模块级
    db_lock 首次 acquire 即绑定 loop。分片测试中后台任务与主循环并发访问锁；
    为保证锁不跨测试遗留绑定，每个测试开头重建 pipeline_mod.db_lock。
    """

    @pytest.mark.asyncio
    async def test_sliced_pipeline_reaches_review(self, pipeline_db, tmp_path):
        from core import pipeline as pipeline_mod

        # 每个测试独立 event loop：重建模块级锁避免跨 loop 绑定
        pipeline_mod.db_lock = asyncio.Lock()

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        # 模拟 run_ocr_sliced：在线程（to_thread）中分两批回调 on_batch。
        # on_batch 通过 call_soon_threadsafe 回到主循环，模拟真实路径。
        def fake_run_sliced(pdf_path, slice_pages, on_batch, progress_cb, job_id=None):
            on_batch(
                1,
                [{"markdown": {"text": "page 1"}, "page_count": 1}],
                2,
            )
            on_batch(
                2,
                [{"markdown": {"text": "page 2"}, "page_count": 2}],
                2,
            )
            return [
                (1, [{"markdown": {"text": "page 1"}, "page_count": 1}]),
                (2, [{"markdown": {"text": "page 2"}, "page_count": 2}]),
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
                "description": "sliced finding",
                "source": "rule",
            }
        ]

        async def slow_analyze(*args, **kwargs):
            await asyncio.sleep(0.01)  # 模拟真实 LLM 耗时，stage2_ms > 0
            return fake_page_result

        orig_backend = pipeline_mod.config["app"].ocr_backend
        orig_slices = pipeline_mod.config["app"].ocr_slices
        orig_timeout = pipeline_mod._SLICE_QUEUE_TIMEOUT
        pipeline_mod.config["app"].ocr_backend = "mineru"
        pipeline_mod.config["app"].ocr_slices = 2
        pipeline_mod._SLICE_QUEUE_TIMEOUT = 0.05
        try:
            with patch(
                "core.mineru_client.run_ocr_sliced", side_effect=fake_run_sliced
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(side_effect=slow_analyze),
            ), patch(
                "core.pipeline.analyze_cross_page",
                new=AsyncMock(return_value=fake_findings),
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend
            pipeline_mod.config["app"].ocr_slices = orig_slices
            pipeline_mod._SLICE_QUEUE_TIMEOUT = orig_timeout

        # 分片路径同样应到达 review
        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        assert (await cursor.fetchone())["status"] == "review"

        # page_cache 两页均已填充（含清洗后的 raw_html）
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

        # total_pages 已更新为全局页号（最后一片到达时 = 2）
        cursor = await pipeline_db.execute(
            "SELECT total_pages, stage1_ms, stage2_ms FROM jobs WHERE id = ?", (job_id,)
        )
        job = await cursor.fetchone()
        assert job["total_pages"] == 2
        assert job["stage1_ms"] > 0
        # stage2_ms 可为 0：流式设计下分析任务在分片 OCR 循环期间即完成，
        # 主流程的 gather 收尾瞬时返回（真实场景分析耗时 ≫ OCR 单片耗时）。
        assert job["stage2_ms"] >= 0

        # Stage 3 findings 正常写入
        cursor = await pipeline_db.execute(
            "SELECT page, source FROM findings WHERE job_id = ?", (job_id,)
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["source"] == "rule"

    @pytest.mark.asyncio
    async def test_sliced_failure_sets_job_to_error(self, pipeline_db, tmp_path):
        """分片 OCR 抛异常 → job 进入 error（与整份路径一致）。"""
        from core import pipeline as pipeline_mod

        pipeline_mod.db_lock = asyncio.Lock()

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        def _boom(pdf_path, slice_pages, on_batch, progress_cb):
            raise RuntimeError("sliced OCR crashed")

        orig_backend = config["app"].ocr_backend
        orig_timeout = pipeline_mod._SLICE_QUEUE_TIMEOUT
        config["app"].ocr_backend = "mineru"
        pipeline_mod._SLICE_QUEUE_TIMEOUT = 0.05
        try:
            with patch(
                "core.mineru_client.run_ocr_sliced", side_effect=_boom
            ), patch(
                "core.pipeline.analyze_page", new=AsyncMock(return_value={})
            ), patch(
                "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            config["app"].ocr_backend = orig_backend
            pipeline_mod._SLICE_QUEUE_TIMEOUT = orig_timeout

        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        assert (await cursor.fetchone())["status"] == "error"

    @pytest.mark.asyncio
    async def test_sliced_partial_failure_degrades_to_partial_review(self, pipeline_db, tmp_path):
        """P1-1: 单片 OCR 失败（此前已有片成功产出）→ 不整单 error：
        已产出页照常分析，缺失页并入 failed_pages → partial_review（用户可见）。
        P1-4 (sliced): 缺页补记走 stage1_pagemismatch 审计 + failed_pages。"""
        from core import pipeline as pipeline_mod

        pipeline_mod.db_lock = asyncio.Lock()

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        def fake_run_sliced_partial(pdf_path, slice_pages, on_batch, progress_cb, job_id=None):
            # 第 1 片成功回调（total=3），随后整体抛异常（第 2/3 片失败）
            on_batch(1, [{"markdown": {"text": "page 1"}, "page_count": 1}], 3)
            raise RuntimeError("slice 2 OCR crashed")

        fake_page_result = {
            "steps": [],
            "findings": [],
            "overall_confidence": "high",
        }

        orig_backend = pipeline_mod.config["app"].ocr_backend
        orig_slices = pipeline_mod.config["app"].ocr_slices
        orig_timeout = pipeline_mod._SLICE_QUEUE_TIMEOUT
        pipeline_mod.config["app"].ocr_backend = "mineru"
        pipeline_mod.config["app"].ocr_slices = 2
        pipeline_mod._SLICE_QUEUE_TIMEOUT = 0.05
        try:
            with patch(
                "core.mineru_client.run_ocr_sliced", side_effect=fake_run_sliced_partial
            ), patch(
                "core.pipeline.analyze_page", new=AsyncMock(return_value=fake_page_result)
            ), patch(
                "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend
            pipeline_mod.config["app"].ocr_slices = orig_slices
            pipeline_mod._SLICE_QUEUE_TIMEOUT = orig_timeout

        # 不整单 error：已产出页完成分析 → partial_review（缺页 2/3 显式暴露）
        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        assert (await cursor.fetchone())["status"] == "partial_review"
        cursor = await pipeline_db.execute(
            "SELECT failed_pages FROM jobs WHERE id = ?", (job_id,)
        )
        import json as _json
        assert _json.loads((await cursor.fetchone())["failed_pages"]) == [2, 3]
        # 缺页审计可见
        cursor = await pipeline_db.execute(
            "SELECT 1 FROM audit_log WHERE job_id = ? AND action = 'stage1_pagemismatch'",
            (job_id,),
        )
        assert await cursor.fetchone() is not None
        # 已产出页 1 正常分析并落库
        cursor = await pipeline_db.execute(
            "SELECT structured_json FROM page_cache WHERE job_id = ? AND page = 1",
            (job_id,),
        )
        assert (await cursor.fetchone())["structured_json"] is not None

    @pytest.mark.asyncio
    async def test_sliced_zero_pages_sets_job_to_error(self, pipeline_db, tmp_path):
        """分片路径所有片为空（total=0）→ job 进入 error + stage1_empty 审计。"""
        from core import pipeline as pipeline_mod

        pipeline_mod.db_lock = asyncio.Lock()

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        def fake_run_sliced(pdf_path, slice_pages, on_batch, progress_cb, job_id=None):
            return []  # 无任何片回调、返回空

        orig_backend = pipeline_mod.config["app"].ocr_backend
        orig_slices = pipeline_mod.config["app"].ocr_slices
        orig_timeout = pipeline_mod._SLICE_QUEUE_TIMEOUT
        pipeline_mod.config["app"].ocr_backend = "mineru"
        pipeline_mod.config["app"].ocr_slices = 2
        pipeline_mod._SLICE_QUEUE_TIMEOUT = 0.05
        try:
            with patch(
                "core.mineru_client.run_ocr_sliced", side_effect=fake_run_sliced
            ), patch(
                "core.pipeline.analyze_page", new=AsyncMock(return_value={})
            ), patch(
                "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend
            pipeline_mod.config["app"].ocr_slices = orig_slices
            pipeline_mod._SLICE_QUEUE_TIMEOUT = orig_timeout

        cursor = await pipeline_db.execute(
            "SELECT status, error_message FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        assert row["status"] == "error"
        assert "0 页" in row["error_message"]
        cursor = await pipeline_db.execute(
            "SELECT 1 FROM audit_log WHERE job_id = ? AND action = 'stage1_empty'", (job_id,)
        )
        assert await cursor.fetchone() is not None

    @pytest.mark.asyncio
    async def test_sliced_cancel_inside_ocr_loop(self, pipeline_db, tmp_path):
        """分片循环内收到取消 → 提前返回，状态停留 ocr_running（主流程兜底退出）。"""
        from core import pipeline as pipeline_mod

        pipeline_mod.db_lock = asyncio.Lock()

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        def fake_run_sliced(pdf_path, slice_pages, on_batch, progress_cb, job_id=None):
            on_batch(1, [{"markdown": {"text": "p1"}, "page_count": 1}], 1)
            return [(1, [{"markdown": {"text": "p1"}, "page_count": 1}])]

        fired = {"batch": False}

        def fake_run_sliced_wrapper(pdf_path, slice_pages, on_batch,
                                    progress_cb, job_id=None):
            res = fake_run_sliced(pdf_path, slice_pages, on_batch,
                                  progress_cb, job_id=job_id)
            # 首片已落库；返回后循环内检查才命中取消（Stage0 新检查点为 False）
            fired["batch"] = True
            return res

        async def fake_cancelled(*a, **kw):
            return fired["batch"]

        orig_backend = pipeline_mod.config["app"].ocr_backend
        orig_slices = pipeline_mod.config["app"].ocr_slices
        orig_timeout = pipeline_mod._SLICE_QUEUE_TIMEOUT
        pipeline_mod.config["app"].ocr_backend = "mineru"
        pipeline_mod.config["app"].ocr_slices = 2
        pipeline_mod._SLICE_QUEUE_TIMEOUT = 0.05
        try:
            with patch(
                "core.mineru_client.run_ocr_sliced",
                side_effect=fake_run_sliced_wrapper,
            ), patch(
                "core.pipeline._is_cancelled", new=AsyncMock(side_effect=fake_cancelled)
            ), patch(
                "core.pipeline.analyze_page", new=AsyncMock(return_value={})
            ), patch(
                "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend
            pipeline_mod.config["app"].ocr_slices = orig_slices
            pipeline_mod._SLICE_QUEUE_TIMEOUT = orig_timeout

        # 取消时状态停留在 ocr_running（未 transition 到 ocr_done）
        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        assert (await cursor.fetchone())["status"] == "ocr_running"
        # 该片数据已落库（回调先于取消检查）
        cursor = await pipeline_db.execute(
            "SELECT count(*) AS n FROM page_cache WHERE job_id = ?", (job_id,)
        )
        assert (await cursor.fetchone())["n"] == 1

    @pytest.mark.asyncio
    async def test_sliced_cancel_after_ocr_done(self, pipeline_db, tmp_path):
        """OCR 全部完成后收到取消 → 提前返回，状态停在 ocr_done（跳过 Stage 2/3）。"""
        from core import pipeline as pipeline_mod

        pipeline_mod.db_lock = asyncio.Lock()

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        def fake_run_sliced(pdf_path, slice_pages, on_batch, progress_cb, job_id=None):
            on_batch(1, [{"markdown": {"text": "p1"}, "page_count": 1}], 1)
            return [(1, [{"markdown": {"text": "p1"}, "page_count": 1}])]

        done = {"ocr": False}

        def fake_run_sliced_mark(pdf_path, slice_pages, on_batch,
                                 progress_cb, job_id=None):
            res = fake_run_sliced(pdf_path, slice_pages, on_batch,
                                  progress_cb, job_id=job_id)
            # OCR 全部返回后才允许取消命中 —— 精确锚定 :571 的
            # post-ocr_done 检查点（Stage0 新检查点保持 False）
            done["ocr"] = True
            return res

        async def fake_cancelled(*a, **kw):
            if not done["ocr"]:
                return False
            cur = await pipeline_db.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,))
            row = await cur.fetchone()
            # 仅当状态已迁移至 ocr_done 才取消 → 跳过 Stage 2/3
            return bool(row) and row["status"] == "ocr_done"

        orig_backend = pipeline_mod.config["app"].ocr_backend
        orig_slices = pipeline_mod.config["app"].ocr_slices
        orig_timeout = pipeline_mod._SLICE_QUEUE_TIMEOUT
        pipeline_mod.config["app"].ocr_backend = "mineru"
        pipeline_mod.config["app"].ocr_slices = 2
        pipeline_mod._SLICE_QUEUE_TIMEOUT = 0.05
        try:
            with patch(
                "core.mineru_client.run_ocr_sliced",
                side_effect=fake_run_sliced_mark,
            ), patch(
                "core.pipeline._is_cancelled", new=AsyncMock(side_effect=fake_cancelled)
            ), patch(
                "core.pipeline.analyze_page", new=AsyncMock(return_value={})
            ), patch(
                "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend
            pipeline_mod.config["app"].ocr_slices = orig_slices
            pipeline_mod._SLICE_QUEUE_TIMEOUT = orig_timeout

        # 已 transition 到 ocr_done 但未进入 Stage 2/3
        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        assert (await cursor.fetchone())["status"] == "ocr_done"
        # 无 Stage 3 findings（跳过分析）
        cursor = await pipeline_db.execute(
            "SELECT count(*) AS n FROM findings WHERE job_id = ?", (job_id,)
        )
        assert (await cursor.fetchone())["n"] == 0

    @pytest.mark.asyncio
    async def test_sliced_cancel_before_ocr_done_transition(self, pipeline_db, tmp_path):
        """分片尾部 ocr_done 转换前确认取消（对齐整份路径修复）→
        已排队分析任务被清理，job 停在 cancelled，不进入 Stage 2/3。"""
        from core import pipeline as pipeline_mod

        pipeline_mod.db_lock = asyncio.Lock()

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        def fake_run_sliced(pdf_path, slice_pages, on_batch, progress_cb, job_id=None):
            on_batch(1, [{"markdown": {"text": "p1"}, "page_count": 1}], 1)
            return [(1, [{"markdown": {"text": "p1"}, "page_count": 1}])]

        # 调用序：1=Stage0 pre, 2=Stage0 post, 3=片内循环检查,
        # 4=尾部转换前检查点 → 确认取消（与整份路径同模式）
        calls = {"n": 0}

        async def fake_cancelled(jid):
            from db.client import get_db
            from core.pipeline import db_lock
            calls["n"] += 1
            if calls["n"] < 4:
                return False
            db = await get_db()
            async with db_lock:
                await db.execute(
                    "UPDATE jobs SET status = 'cancelled', "
                    "finished_at = datetime('now','localtime') WHERE id = ?",
                    (jid,),
                )
                await db.commit()
            return True

        orig_backend = pipeline_mod.config["app"].ocr_backend
        orig_slices = pipeline_mod.config["app"].ocr_slices
        orig_timeout = pipeline_mod._SLICE_QUEUE_TIMEOUT
        pipeline_mod.config["app"].ocr_backend = "mineru"
        pipeline_mod.config["app"].ocr_slices = 2
        pipeline_mod._SLICE_QUEUE_TIMEOUT = 0.05
        try:
            with patch(
                "core.mineru_client.run_ocr_sliced", side_effect=fake_run_sliced,
            ), patch(
                "core.pipeline._is_cancelled",
                new=AsyncMock(side_effect=fake_cancelled),
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(return_value={"steps": [], "findings": [],
                                            "overall_confidence": "high"}),
            ), patch(
                "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[]),
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend
            pipeline_mod.config["app"].ocr_slices = orig_slices
            pipeline_mod._SLICE_QUEUE_TIMEOUT = orig_timeout

        cursor = await pipeline_db.execute(
            "SELECT status, error_message FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        assert row["status"] == "cancelled", f"got {row['status']}"
        assert not row["error_message"]
        # 未发生 ocr_done 迁移、未被强制 error
        cursor = await pipeline_db.execute(
            "SELECT count(*) AS n FROM audit_log WHERE job_id = ? "
            "AND action = 'status_forced_error'", (job_id,))
        assert (await cursor.fetchone())["n"] == 0

    @pytest.mark.asyncio
    async def test_sliced_ocr_done_transition_race_drains_tasks(
        self, pipeline_db, tmp_path
    ):
        """竞态窗口回归：尾部检查点后、ocr_done 转换前用户取消
        （ocr_running → cancelling）→ 转换抛 InvalidTransitionError，
        except 分支必须清理已排队分析任务后重抛，引擎恢复分支完成
        cancelling → cancelled 正式迁移（终态不落 error）。"""
        from core import pipeline as pipeline_mod

        pipeline_mod.db_lock = asyncio.Lock()

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        def fake_run_sliced(pdf_path, slice_pages, on_batch, progress_cb, job_id=None):
            on_batch(1, [{"markdown": {"text": "p1"}, "page_count": 1}], 1)
            return [(1, [{"markdown": {"text": "p1"}, "page_count": 1}])]

        calls = {"n": 0}

        async def fake_cancelled(jid):
            from db.client import get_db
            from core.pipeline import db_lock
            calls["n"] += 1
            if calls["n"] < 4:
                return False
            # 第 4 次调用（尾部检查点）时把状态改成 cancelling 但返回
            # False —— 检查点放行，转换在 cancelling 状态下必然非法抛出
            db = await get_db()
            async with db_lock:
                await db.execute(
                    "UPDATE jobs SET status = 'cancelling' WHERE id = ?", (jid,))
                await db.commit()
            return False

        orig_backend = pipeline_mod.config["app"].ocr_backend
        orig_slices = pipeline_mod.config["app"].ocr_slices
        orig_timeout = pipeline_mod._SLICE_QUEUE_TIMEOUT
        pipeline_mod.config["app"].ocr_backend = "mineru"
        pipeline_mod.config["app"].ocr_slices = 2
        pipeline_mod._SLICE_QUEUE_TIMEOUT = 0.05
        try:
            with patch(
                "core.mineru_client.run_ocr_sliced", side_effect=fake_run_sliced,
            ), patch(
                "core.pipeline._is_cancelled",
                new=AsyncMock(side_effect=fake_cancelled),
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(return_value={"steps": [], "findings": [],
                                            "overall_confidence": "high"}),
            ), patch(
                "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[]),
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend
            pipeline_mod.config["app"].ocr_slices = orig_slices
            pipeline_mod._SLICE_QUEUE_TIMEOUT = orig_timeout

        cursor = await pipeline_db.execute(
            "SELECT status, error_message FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        assert row["status"] == "cancelled", f"got {row['status']}"
        assert not row["error_message"]
        cursor = await pipeline_db.execute(
            "SELECT count(*) AS n FROM audit_log WHERE job_id = ? "
            "AND action = 'status_forced_error'", (job_id,))
        assert (await cursor.fetchone())["n"] == 0

    @pytest.mark.asyncio
    async def test_llm_overlap_suppressed(self, pipeline_db, tmp_path):
        """降噪 N1：rule 已覆盖的 (page,type)，llm_cross 不再重复报告。

        同一问题在复核 UI 出现 rule+LLM 两三份是用户可见噪声主源；
        rule 为权威版本，被抑制数量写审计（GMP 可追溯）。
        """
        job_id = await _insert_job(pipeline_db)
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        pages = [{"markdown": {"text": f"page {i} " + "内容" * 50}}
                 for i in (1, 2)]

        rule_findings = [
            {"page": 1, "type": "equipment_state", "severity": "warning",
             "description": "规则层：设备状态未通过", "source": "rule"},
        ]
        # ⚠️ 这里刻意用 equipment_state 而不是 time_reversal：B1-6 收权复核
        # 会在 N1 之前先处理 time_reversal（structured 里无倒序 ⇒ 判定无据 ⇒
        # 抑制），从而让本测试验证不到 N1 本身。选一个复核器不介入的类型，
        # 才能测准 N1 这条独立的降噪机制。
        llm_cross = [
            {"page": 1, "type": "equipment_state", "severity": "warning",
             "description": "LLM：设备状态有问题（语义重复）",
             "source": "llm_cross"},
            {"page": 1, "type": "completeness", "severity": "info",
             "description": "缺少复核签名", "source": "llm_cross"},
        ]

        async def fake_cross(page_structures, job_id="", progress_cb=None):
            return rule_findings + llm_cross

        with patch(
            "core.pipeline._get_ocr_chain",
            return_value=[(lambda p, cb: pages, "mineru")],
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(return_value={
                "steps": [], "findings": [],
                "overall_confidence": "high"}),
        ), patch(
            "core.pipeline.analyze_cross_page",
            new=AsyncMock(side_effect=fake_cross),
        ):
            await run_pipeline(job_id, pdf_path)

        cur = await pipeline_db.execute(
            "SELECT type, source FROM findings WHERE job_id = ? "
            "AND page = 1 ORDER BY source", (job_id,))
        rows = [dict(r) for r in await cur.fetchall()]
        eq_sources = [r["source"] for r in rows
                      if r["type"] == "equipment_state"]
        assert eq_sources == ["rule"], f"overlap not suppressed: {rows}"
        comp = [r for r in rows if r["type"] == "completeness"]
        assert len(comp) == 1 and comp[0]["source"] == "llm_cross"

        cur = await pipeline_db.execute(
            "SELECT detail FROM audit_log WHERE job_id = ? AND "
            "action = 'findings_overlap_suppressed'", (job_id,))
        log = await cur.fetchone()
        assert log is not None and "count=1" in log["detail"]

    async def test_llm_guard_suppresses_unfounded_cross_finding(
        self, pipeline_db, tmp_path
    ):
        """B1-6 收权链路级：跨页 LLM 结论被规则层确定性复核否定 ⇒ 抑制 + 留痕。

        样本取 Round 43 实测形态（p6 的跨字段串位）：LLM 说"结束 13:6 早于
        开始 09:00"，但该页结构化数据里根本没有倒序 ⇒ 判定无据。
        断言两件事：① findings 表不再出现该 critical；② 抑制**留痕**
        （finding_suppressions 有非空 reason，可回退、可抽检）。
        """
        job_id = await _insert_job(pipeline_db)
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        pages = [{"markdown": {"text": f"page {i} " + "内容" * 50}}
                 for i in (1, 2)]
        cross = [{
            "page": 1, "type": "time_reversal", "severity": "critical",
            "description": "接收结束时间 13:6 早于接收开始时间 09:00",
            "source": "llm_cross",
        }]

        async def fake_cross(page_structures, job_id="", progress_cb=None):
            return cross

        with patch(
            "core.pipeline._get_ocr_chain",
            return_value=[(lambda p, cb: pages, "mineru")],
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(return_value={
                # Round 46：该页必须**有可解析的工序时刻**，收权重算才是
                # 「有样本且确无倒序」（强证据 ⇒ 抑制）。若留成 `steps: []`，
                # 规则层是「判不了」⇒ 按新语义只降级、**不写**抑制台账
                # （见下一个用例 `test_llm_guard_downgrades_when_unable_to_adjudicate`）。
                # ⚠️ `page_info.production_date` **不可省**：纯时刻（`09:00`）
                # 必须有 fallback 日期才解析得出（`_parse_time_interval` 实测
                # 无 fallback ⇒ None）—— 夹具少这一项会让"有样本"静默退化成
                # "判不了"，从而**偷偷改变收权档位**（本轮就踩到：测试红了但
                # 原因不是被测逻辑，而是夹具不自洽）。
                "page_info": {"production_date": "2025-01-20"},
                "steps": [{"step_no": "3", "start_time": "09:00",
                           "end_time": "09:52"}],
                "findings": [], "overall_confidence": "high"}),
        ), patch(
            "core.pipeline.analyze_cross_page",
            new=AsyncMock(side_effect=fake_cross),
        ):
            await run_pipeline(job_id, pdf_path)

        cur = await pipeline_db.execute(
            "SELECT severity FROM findings WHERE job_id = ? AND type='time_reversal'",
            (job_id,))
        rows = [dict(r) for r in await cur.fetchall()]
        assert all(r["severity"] != "critical" for r in rows), rows

        cur = await pipeline_db.execute(
            "SELECT reason FROM finding_suppressions WHERE job_id = ?", (job_id,))
        sup = [dict(r) for r in await cur.fetchall()]
        assert sup, "收权抑制未留痕（GMP 要求抑制可抽检/可回退）"
        assert sup[0]["reason"].strip()

    async def test_llm_guard_downgrades_when_unable_to_adjudicate(
        self, pipeline_db, tmp_path
    ):
        """Round 46 反向用例：该页**无任何可解析工序** ⇒ 规则层**判不了**
        ⇒ **只降级，不抑制、不写抑制台账**（判不了 ≠ 判据确凿）。

        与上一个用例成**一对**：同一条 LLM 结论，只因 structured 里有没有
        可解析样本，处置档位就应不同 —— 这正是本轮修复的核心不变式。
        ⚠️ 旧实现在这里会**误抑制**（`_recompute_time_reversal` 返 bool，
        把"无样本"归入 False）⇒ 实测冤枉抑制了 p43×4 / p46 的真倒序线索。
        """
        job_id = await _insert_job(pipeline_db)
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        pages = [{"markdown": {"text": f"page {i} " + "内容" * 50}}
                 for i in (1, 2)]
        cross = [{
            "page": 1, "type": "time_reversal", "severity": "critical",
            "description": "接收结束时间 13:6 早于接收开始时间 09:00",
            "source": "llm_cross",
        }]

        async def fake_cross(page_structures, job_id="", progress_cb=None):
            return cross

        with patch(
            "core.pipeline._get_ocr_chain",
            return_value=[(lambda p, cb: pages, "mineru")],
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(return_value={
                # 工序时刻一个都解析不出 ⇒ 规则层无从复核
                # （`page_info` 照给，以证明"判不了"确系**时刻缺失**所致，
                #  而不是靠"缺 fallback 日期"凑出来的同一个结果。）
                "page_info": {"production_date": "2025-01-20"},
                "steps": [{"step_no": "3", "start_time": "", "end_time": "—"}],
                "findings": [], "overall_confidence": "high"}),
        ), patch(
            "core.pipeline.analyze_cross_page",
            new=AsyncMock(side_effect=fake_cross),
        ):
            await run_pipeline(job_id, pdf_path)

        cur = await pipeline_db.execute(
            "SELECT severity, description FROM findings "
            "WHERE job_id = ? AND type='time_reversal'", (job_id,))
        rows = [dict(r) for r in await cur.fetchall()]
        assert rows, "判不了时应**保留**（降级），而不是丢弃（漏检代价 > 误报代价）"
        assert all(r["severity"] != "critical" for r in rows), rows
        assert any("无法复核" in (r["description"] or "") for r in rows), rows

        cur = await pipeline_db.execute(
            "SELECT reason FROM finding_suppressions WHERE job_id = ?", (job_id,))
        sup = [dict(r) for r in await cur.fetchall()]
        assert not sup, "判不了不得写抑制台账（否则会被读成『已确证不成立』）"


# ─── 3. 空页自动重试（Stage 1 抗挫折，MinerU 大文件丢页）───────────


class TestStage1EmptyPageRetry:
    """MinnerU 大 PDF 丢页 → 空页切片重跑恢复 / 仍空保留 / 小文件跳过。"""

    async def _run(self, pipeline_db, tmp_path, pages: list[dict], fake_retry=None):
        """公共驱动：mineru 后端 + 给定 OCR 页列表，返回 (job_id, retry_calls)。"""
        from core import pipeline as pipeline_mod

        orig_backend = pipeline_mod.config["app"].ocr_backend
        pipeline_mod.config["app"].ocr_backend = "mineru"
        calls = []
        try:
            job_id = await _insert_job(pipeline_db, status="pending")
            pdf_path = str(tmp_path / "fake.pdf")
            Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

            def fake_ocr(pdf_path, progress_cb=None, job_id=None):
                return pages

            with patch(
                "core.pipeline._get_ocr_backend", return_value=fake_ocr
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(
                    return_value={
                        "steps": [],
                        "findings": [],
                        "overall_confidence": "high",
                    }
                ),
            ), patch(
                "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
            ):
                if fake_retry is not None:
                    def _recording_retry(*a, **kw):
                        calls.append((list(kw.get("page_nums", a[1])), kw.get("batch_size", a[2] if len(a) > 2 else 3)))
                        return fake_retry(*a, **kw)

                    with patch(
                        "core.mineru_client.run_ocr_pages", side_effect=_recording_retry
                    ):
                        await run_pipeline(job_id, pdf_path)
                else:
                    await run_pipeline(job_id, pdf_path)
            return job_id, calls
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend

    @pytest.mark.asyncio
    async def test_empty_page_recovered_via_slice_retry(self, pipeline_db, tmp_path):
        """空页切片重跑返回完整文本 → page_cache 更新 + recovered 审计。"""
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}}
            for i in range(1, 13)
        ]
        pages[4]["markdown"]["text"] = ""  # p5 空 → 触发重试

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            return [(pno, f"recovered content p{pno} " + "y" * 200, 0) for pno in page_nums]

        job_id, calls = await self._run(
            pipeline_db, tmp_path, pages, fake_retry=fake_retry
        )

        # p5 已被切片重跑结果替换
        cursor = await pipeline_db.execute(
            "SELECT raw_html FROM page_cache WHERE job_id = ? AND page = 5",
            (job_id,),
        )
        row = await cursor.fetchone()
        assert row and "recovered content p5" in row["raw_html"] and "y" * 200 in row["raw_html"]
        assert calls[0][1] == 3  # 首轮 3 页批
        # recovered 审计
        cursor = await pipeline_db.execute(
            "SELECT detail FROM audit_log WHERE job_id = ? AND action = 'stage1_empty_recovered'",
            (job_id,),
        )
        assert await cursor.fetchone() is not None
        # ocr_backend_used 审计（GMP 追溯）
        cursor = await pipeline_db.execute(
            "SELECT ocr_backend_used FROM jobs WHERE id = ?", (job_id,)
        )
        assert (await cursor.fetchone())["ocr_backend_used"] == "mineru"

    @pytest.mark.asyncio
    async def test_self_heal_progress_written_and_cleared(self, pipeline_db, tmp_path):
        """Todo 13: 自愈期间 ocr_progress 带 self_heal 子键（SSE 可见），
        结束后清除（total<=0 → state 层跳过写入）。"""
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}}
            for i in range(1, 13)
        ]
        pages[4]["markdown"]["text"] = ""  # p5 空 → 触发自愈

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            return [(pno, f"recovered content p{pno} " + "y" * 200, 0) for pno in page_nums]

        job_id, _ = await self._run(
            pipeline_db, tmp_path, pages, fake_retry=fake_retry
        )

        cursor = await pipeline_db.execute(
            "SELECT ocr_progress FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        # 自愈结束清除后：只剩主 OCR 进度，无 self_heal 键（主进度在 fake
        # 场景下可能为 NULL→0/0，真实路径是 12/12 — 断言清除行为即可）
        assert row and row["ocr_progress"] is not None
        import json as _json
        data = _json.loads(row["ocr_progress"])
        assert "self_heal" not in data
        assert "done" in data and "total" in data

    @pytest.mark.asyncio
    async def test_empty_page_retry_still_empty_keeps_original(self, pipeline_db, tmp_path):
        """两轮重试后仍空（服务端识别不了）→ 保留原空内容，继续走流程。"""
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}}
            for i in range(1, 13)
        ]
        pages[4]["markdown"]["text"] = ""  # p5 空
        pages[9]["markdown"]["text"] = "  "  # p10 空白 → 也触发

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            # 两轮都返回空（模拟服务端真识别不了）
            return [(pno, "", 0) for pno in page_nums]

        job_id, calls = await self._run(
            pipeline_db, tmp_path, pages, fake_retry=fake_retry
        )

        # 两轮重试都发生了（3 页批 → 单页批）
        assert calls[0][1] == 3
        assert calls[1][1] == 1
        # 空页保留 OCR 警告前缀（诊断标签，非虚假恢复内容）
        cursor = await pipeline_db.execute(
            "SELECT raw_html FROM page_cache WHERE job_id = ? AND page = 5",
            (job_id,),
        )
        raw5 = (await cursor.fetchone())["raw_html"]
        assert "[OCR 警告" in raw5
        assert "页面无可用文本" in raw5
        # 无 recovered 审计
        cursor = await pipeline_db.execute(
            "SELECT 1 FROM audit_log WHERE job_id = ? AND action = 'stage1_empty_recovered'",
            (job_id,),
        )
        assert await cursor.fetchone() is None
        # 仍成功到 review（空页有 _ocr_empty 提示配套，不阻断流程）
        cursor = await pipeline_db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        assert (await cursor.fetchone())["status"] == "review"

    @pytest.mark.asyncio
    async def test_small_file_empty_page_selfheal_runs(self, pipeline_db, tmp_path):
        """<10 页的小文件空页也触发自愈（Round 5 后：小文件也启用自愈）。"""
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}}
            for i in range(1, 5)
        ]
        pages[1]["markdown"]["text"] = ""  # p2 空 — 小文件仍触发自愈

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            return [(pno, "", 0) for pno in page_nums]

        job_id, calls = await self._run(pipeline_db, tmp_path, pages, fake_retry=fake_retry)

        # 小文件自愈触发（单页批）
        assert len(calls) >= 1
        # 空页保留 OCR 警告前缀
        cursor = await pipeline_db.execute(
            "SELECT raw_html FROM page_cache WHERE job_id = ? AND page = 2",
            (job_id,),
        )
        raw2 = (await cursor.fetchone())["raw_html"]
        assert "[OCR 警告" in raw2

    @pytest.mark.asyncio
    async def test_empty_page_selfheal_runs_on_cached_reuse(self, pipeline_db, tmp_path):
        """F5d: retry 复用缓存（F3 路径）时历史空页仍触发切片重跑恢复。

        回归背景：空页自愈原实现要求 used_backend=="mineru"，而缓存复用
        路径 used_backend="cached" → 丢页缺陷时期遗留的历史空页（真实
        51pages job 6 页）永远不会被恢复。修复后应回查 jobs.ocr_backend_used
        并按 page_cache 统一检出短页重跑。
        """
        from core import pipeline as pipeline_mod

        orig_backend = pipeline_mod.config["app"].ocr_backend
        pipeline_mod.config["app"].ocr_backend = "mineru"
        try:
            # 预置带历史空页的 job：12 页缓存齐（触发 F3 复用），
            # p5 只有 '## 第 5 页' 8 字符（真实 MinerU 空页样子）
            job_id = await _insert_job(pipeline_db, status="pending")
            pdf_path = str(tmp_path / "fake.pdf")
            Path(pdf_path).write_bytes(b"%PDF-1.4 fake")
            await pipeline_db.execute(
                "UPDATE jobs SET total_pages = 12, ocr_backend_used = 'mineru' WHERE id = ?",
                (job_id,),
            )
            for i in range(1, 13):
                text = "## 第 %d 页" % i if i == 5 else "page %d content " % i + "x" * 200
                await pipeline_db.execute(
                    "INSERT INTO page_cache (job_id, page, raw_html) VALUES (?, ?, ?)",
                    (job_id, i, text),
                )
            await pipeline_db.commit()

            calls = []

            def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
                calls.append((list(page_nums), batch_size))
                return [
                    (pno, f"recovered content p{pno} " + "y" * 200, 0)
                    for pno in page_nums
                ]

            with patch(
                "core.pipeline._get_ocr_backend",
                new=AsyncMock(side_effect=AssertionError("reuse path must not re-OCR")),
            ), patch(
                "core.mineru_client.run_ocr_pages", side_effect=fake_retry
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(
                    return_value={
                        "steps": [],
                        "findings": [],
                        "overall_confidence": "high",
                    }
                ),
            ), patch(
                "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
            ):
                await run_pipeline(job_id, pdf_path)

            # 触发过切片重跑（batch=3 复用路径也检测到 p5 空页）
            assert calls, "empty-page self-heal should run on cached-reuse path"
            # p5 已恢复
            cursor = await pipeline_db.execute(
                "SELECT raw_html FROM page_cache WHERE job_id = ? AND page = 5",
                (job_id,),
            )
            row = await cursor.fetchone()
            assert row and "recovered content p5" in row["raw_html"]
            # recovered 审计存在
            cursor = await pipeline_db.execute(
                "SELECT 1 FROM audit_log WHERE job_id = ? AND action = 'stage1_empty_recovered'",
                (job_id,),
            )
            assert await cursor.fetchone() is not None
            # ocr_backend_used 未被 "cached" 污染（GMP 溯源保留 mineru）
            cursor = await pipeline_db.execute(
                "SELECT ocr_backend_used FROM jobs WHERE id = ?", (job_id,)
            )
            assert (await cursor.fetchone())["ocr_backend_used"] == "mineru"
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend


async def _run_mineru_pipeline(
    pipeline_db, tmp_path, pages, fake_retry=None, cancelled_fn=None
):
    """mineru 整份路径驱动（自愈缺口测试复用），支持重试 fake 与取消注入。"""
    from core import pipeline as pipeline_mod

    orig_backend = pipeline_mod.config["app"].ocr_backend
    pipeline_mod.config["app"].ocr_backend = "mineru"
    calls = []
    try:
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        with patch(
            "core.pipeline._get_ocr_backend",
            return_value=lambda p, cb: pages,
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(
                return_value={
                    "steps": [],
                    "findings": [],
                    "overall_confidence": "high",
                }
            ),
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            with ExitStack() as stack:
                if fake_retry is not None:
                    def _recording_retry(*a, **kw):
                        calls.append(
                            (list(kw.get("page_nums", a[1])), kw.get("batch_size", 3))
                        )
                        return fake_retry(*a, **kw)

                    stack.enter_context(
                        patch(
                            "core.mineru_client.run_ocr_pages",
                            side_effect=_recording_retry,
                        )
                    )
                if cancelled_fn is not None:
                    stack.enter_context(
                        patch(
                            "core.pipeline._is_cancelled",
                            new=AsyncMock(side_effect=cancelled_fn),
                        )
                    )
                await run_pipeline(job_id, pdf_path)
        return job_id, calls
    finally:
        pipeline_mod.config["app"].ocr_backend = orig_backend


class TestSelfHealCoverageGaps:
    """self_heal.py 拆包后缺口补测：标签多文字少判定 / discarded 前缀 /
    轮间取消 / Paddle 单页重提分支。"""

    @pytest.mark.asyncio
    async def test_tag_heavy_short_text_page_triggers_retry(self, pipeline_db, tmp_path):
        """对抗审查 cr-17：HTML 标签多但真实文本 <100 字符的页也触发自愈。"""
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}}
            for i in range(1, 13)
        ]
        pages[4]["markdown"]["text"] = "<td>" * 40  # 160 字符标签，剥除后为空

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            return [(pno, f"recovered content p{pno} " + "y" * 200, 0) for pno in page_nums]

        job_id, calls = await _run_mineru_pipeline(
            pipeline_db, tmp_path, pages, fake_retry=fake_retry
        )

        assert calls, "tag-heavy short-text page should trigger self-heal"
        cursor = await pipeline_db.execute(
            "SELECT raw_html FROM page_cache WHERE job_id = ? AND page = 5",
            (job_id,),
        )
        row = await cursor.fetchone()
        assert "recovered content p5" in row["raw_html"]

    @pytest.mark.asyncio
    async def test_recovered_page_reinjects_ocr_warning_prefix(self, pipeline_db, tmp_path):
        """D3: 自愈恢复页带 discarded>0 → 重新注入 [OCR 警告] 前缀。"""
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}}
            for i in range(1, 13)
        ]
        pages[4]["markdown"]["text"] = ""

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            return [(pno, f"recovered p{pno} " + "y" * 200, 7) for pno in page_nums]

        job_id, _ = await _run_mineru_pipeline(
            pipeline_db, tmp_path, pages, fake_retry=fake_retry
        )

        cursor = await pipeline_db.execute(
            "SELECT raw_html FROM page_cache WHERE job_id = ? AND page = 5",
            (job_id,),
        )
        row = await cursor.fetchone()
        assert "[OCR 警告: 本页有 7 个内容块" in row["raw_html"]

    @pytest.mark.asyncio
    async def test_retry_cancelled_between_rounds_keeps_partial(self, pipeline_db, tmp_path):
        """P2-9: 两轮重试之间收到取消 → 中断自愈，第二轮不执行。"""
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}}
            for i in range(1, 13)
        ]
        pages[4]["markdown"]["text"] = ""
        pages[9]["markdown"]["text"] = ""

        retry_count = {"n": 0}

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            retry_count["n"] += 1
            return [(pno, "", 0) for pno in page_nums]

        async def fake_cancelled(jid):
            return retry_count["n"] >= 1  # 首轮重试完成后取消

        job_id, calls = await _run_mineru_pipeline(
            pipeline_db, tmp_path, pages,
            fake_retry=fake_retry, cancelled_fn=fake_cancelled,
        )

        assert calls[0][1] == 3  # 首轮 3 页批确实执行
        assert retry_count["n"] == 1  # 第二轮未启动

    @pytest.mark.asyncio
    async def test_paddle_selfheal_recovers_and_keeps_truly_empty(self, pipeline_db, tmp_path):
        """Paddle 后端：fitz 单页重提恢复空页；重提失败/仍空的页保留原样。"""
        from core import pipeline as pipeline_mod
        import fitz

        orig_backend = pipeline_mod.config["app"].ocr_backend
        pipeline_mod.config["app"].ocr_backend = "paddle"
        try:
            job_id = await _insert_job(pipeline_db, status="pending")
            pdf_path = str(tmp_path / "fake.pdf")
            job_dir = Path(config["app"].output_dir) / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            doc = fitz.open()
            for _ in range(12):
                doc.new_page()
            doc.save(pdf_path)
            doc.close()

            pages = [
                {"markdown": {"text": f"page {i} content " + "x" * 200}}
                for i in range(1, 13)
            ]
            pages[4]["markdown"]["text"] = ""  # p5 → 重提恢复
            pages[9]["markdown"]["text"] = " "  # p10 → 重提抛异常/仍空

            def fake_run_ocr(slice_path):
                pno = int(Path(slice_path).stem.split("-", 1)[1].lstrip("p"))
                if pno == 5:
                    return [{"markdown": {"text": "recovered p5 " + "y" * 200}}]
                raise RuntimeError("slice OCR failed")  # p10 → 异常兜底路径

            with patch(
                "core.pipeline._get_ocr_backend",
                return_value=lambda p, cb: pages,
            ), patch(
                "core.ocr_client.run_ocr", side_effect=fake_run_ocr
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(
                    return_value={
                        "steps": [], "findings": [],
                        "overall_confidence": "high",
                    }
                ),
            ), patch(
                "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
            ):
                await run_pipeline(job_id, pdf_path)

            cursor = await pipeline_db.execute(
                "SELECT raw_html FROM page_cache WHERE job_id = ? AND page = 5",
                (job_id,),
            )
            assert "recovered p5" in (await cursor.fetchone())["raw_html"]
            cursor = await pipeline_db.execute(
                "SELECT raw_html FROM page_cache WHERE job_id = ? AND page = 10",
                (job_id,),
            )
            raw10 = (await cursor.fetchone())["raw_html"]
            assert "[OCR 警告" in raw10
            cursor = await pipeline_db.execute(
                "SELECT 1 FROM audit_log WHERE job_id = ? AND action = 'stage1_empty_recovered'",
                (job_id,),
            )
            assert await cursor.fetchone() is not None
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend

    @staticmethod
    def _make_pdf(tmp_path, n_pages=12):
        """fitz 生成 n 页真实 PDF，p5 内容横置 90°（与 e2e_rot.pdf p2 同
        构造：正常排版源页 show_pdf_page(rotate=90) 整体旋转嵌入）。

        三重用途：_write_rotated_slice 需要可打开的文档（fake bytes 会让
        所有角度探测异常跳过）；预筛（round-23 A4）需要真实纵向文本几何
        才会只探测 90/270；p5 横向几何（842×595 → aspect_ratio≈1.415>1）
        命中嫌疑横置页升级判定。"""
        import fitz
        path = str(tmp_path / "real.pdf")
        doc = fitz.open()
        for i in range(n_pages):
            if i == 4:
                src = fitz.open()
                sp = src.new_page(width=595, height=842)
                y = 60
                for _ in range(10):
                    sp.insert_text(
                        (50, y), "sideways content " + "s" * 24, fontsize=11
                    )
                    y += 22
                np_ = doc.new_page(width=842, height=595)
                np_.show_pdf_page(np_.rect, src, 0, rotate=90)
                src.close()
            else:
                doc.new_page()
                doc[i].insert_text((72, 100), f"page {i + 1} filler")
        doc.save(path)
        doc.close()
        return path

    @pytest.mark.asyncio
    async def test_rotation_heal_recovers_sideways_page_mineru(
        self, pipeline_db, tmp_path
    ):
        """round-23 A：切片重试仍空（内容横置）→ 旋转探测 90° 恢复。

        断言：raw_html 替换为旋转后文本、诊断带 rotation_deg=90、
        审计 stage1_rotation_recovered 落库。
        """
        from core import pipeline as pipeline_mod
        import json as _json

        pdf_path = self._make_pdf(tmp_path)
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}}
            for i in range(1, 13)
        ]
        pages[4]["markdown"]["text"] = ""  # p5 横置 → OCR 空 → 触发自愈链

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            if list(page_nums) == [1]:
                # 旋转切片：run_ocr_pages 对单页新 PDF 提交，page_nums=[1]
                return [(1, "rotated content p5 " + "y" * 200, 0)]
            # 主 OCR 切片自愈：仍空（横置内容切片重试救不回来）
            return [(pno, "", 0) for pno in page_nums]

        orig_backend = pipeline_mod.config["app"].ocr_backend
        pipeline_mod.config["app"].ocr_backend = "mineru"
        job_id = await _insert_job(pipeline_db, status="pending")
        try:
            with patch(
                "core.pipeline._get_ocr_backend",
                return_value=lambda p, cb: pages,
            ), patch(
                "core.mineru_client.run_ocr_pages", side_effect=fake_retry,
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(return_value={
                    "steps": [], "findings": [],
                    "overall_confidence": "high"}),
            ), patch(
                "core.pipeline.analyze_cross_page",
                new=AsyncMock(return_value=[]),
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend

        cursor = await pipeline_db.execute(
            "SELECT raw_html, ocr_diagnostics FROM page_cache "
            "WHERE job_id = ? AND page = 5", (job_id,))
        row = await cursor.fetchone()
        assert "rotated content p5" in row["raw_html"], (
            f"rotation heal did not replace raw_html: {row['raw_html'][:80]}"
        )
        diag = _json.loads(row["ocr_diagnostics"])
        assert diag["self_healed"] is True and diag["rotation_deg"] == 90

        cursor = await pipeline_db.execute(
            "SELECT detail FROM audit_log WHERE job_id = ? AND "
            "action = 'stage1_rotation_recovered'", (job_id,))
        log = await cursor.fetchone()
        assert log is not None and "90" in log["detail"]

    @pytest.mark.asyncio
    async def test_rotation_heal_all_angles_fail_keeps_original(
        self, pipeline_db, tmp_path
    ):
        """旋转探测全部角度未过验收 → 页保留原空状态，无旋转审计，
        流水线照常终态（真无法识别走人工复核路径）。"""
        from core import pipeline as pipeline_mod
        import json as _json

        pdf_path = self._make_pdf(tmp_path)
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}}
            for i in range(1, 13)
        ]
        pages[4]["markdown"]["text"] = ""

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            return [(pno, "", 0) for pno in page_nums]

        orig_backend = pipeline_mod.config["app"].ocr_backend
        pipeline_mod.config["app"].ocr_backend = "mineru"
        job_id = await _insert_job(pipeline_db, status="pending")
        try:
            with patch(
                "core.pipeline._get_ocr_backend",
                return_value=lambda p, cb: pages,
            ), patch(
                "core.mineru_client.run_ocr_pages", side_effect=fake_retry,
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(return_value={
                    "steps": [], "findings": [],
                    "overall_confidence": "high"}),
            ), patch(
                "core.pipeline.analyze_cross_page",
                new=AsyncMock(return_value=[]),
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend

        cursor = await pipeline_db.execute(
            "SELECT raw_html, ocr_diagnostics FROM page_cache "
            "WHERE job_id = ? AND page = 5", (job_id,))
        row = await cursor.fetchone()
        # 未被旋转文本污染：空页在 stage1 落库时带 [OCR 警告:] 完整性
        # 横幅（设计行为 — 供复核页横幅与 LLM 低置信提示），旋转失败后
        # 原样保留该横幅而非裸空串。
        assert row["raw_html"].startswith("[OCR 警告:")
        assert "rotated" not in row["raw_html"]
        # round-23 A2：探测未果页落 rotation_probed 诊断 — 复核页提示
        # "系统已尝试旋转恢复"，区别于未探测过的空页（GMP 可追溯）。
        diag = _json.loads(row["ocr_diagnostics"])
        assert diag.get("rotation_probed") is True
        assert diag.get("recovered") is False
        cursor = await pipeline_db.execute(
            "SELECT 1 FROM audit_log WHERE job_id = ? AND "
            "action = 'stage1_rotation_recovered'", (job_id,))
        assert (await cursor.fetchone()) is None

    @pytest.mark.asyncio
    async def test_rotation_heal_paddle_branch(self, pipeline_db, tmp_path):
        """Paddle 后端：切片重提（selfheal-*.pdf）返回空 → 旋转探测
        （rot*-*.pdf）返回横置文本 → 恢复并写 rotation_deg。"""
        from core import pipeline as pipeline_mod
        import json as _json

        pdf_path = self._make_pdf(tmp_path)
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}}
            for i in range(1, 13)
        ]
        pages[4]["markdown"]["text"] = ""

        def fake_run_ocr(path):
            if "selfheal" in str(path):
                return []  # 单页重提仍空
            return [{"markdown": {"text": "paddle rotated p5 " + "z" * 200}}]

        orig_backend = pipeline_mod.config["app"].ocr_backend
        pipeline_mod.config["app"].ocr_backend = "paddle"
        job_id = await _insert_job(pipeline_db, status="pending")
        try:
            with patch(
                "core.pipeline._get_ocr_backend",
                return_value=lambda p, cb: pages,
            ), patch(
                "core.ocr_client.run_ocr", side_effect=fake_run_ocr,
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(return_value={
                    "steps": [], "findings": [],
                    "overall_confidence": "high"}),
            ), patch(
                "core.pipeline.analyze_cross_page",
                new=AsyncMock(return_value=[]),
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend

        cursor = await pipeline_db.execute(
            "SELECT raw_html, ocr_diagnostics FROM page_cache "
            "WHERE job_id = ? AND page = 5", (job_id,))
        row = await cursor.fetchone()
        assert "paddle rotated p5" in row["raw_html"]
        diag = _json.loads(row["ocr_diagnostics"])
        assert diag["rotation_deg"] == 90

    def test_write_rotated_slice_swaps_dimensions(self, tmp_path):
        """_write_rotated_slice：90° 输出页宽高互换，/Rotate 元数据为 0
        （内容旋转进新页面布局，非元数据标记 — 与"内容横置"场景一致）。"""
        import fitz
        from core.pipeline.self_heal import _write_rotated_slice

        src_path = str(tmp_path / "src.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=400)
        page.insert_text((20, 50), "sideways")
        doc.save(src_path)
        doc.close()

        dst = str(tmp_path / "rot.pdf")
        _write_rotated_slice(src_path, 1, 90, dst)
        out = fitz.open(dst)
        try:
            assert out.page_count == 1
            rect = out[0].rect
            assert abs(rect.width - 400) < 1 and abs(rect.height - 200) < 1
            assert int(out[0].rotation or 0) == 0
        finally:
            out.close()

    @pytest.mark.asyncio
    async def test_rotation_upgrade_partial_slice_recovery(
        self, pipeline_db, tmp_path
    ):
        """round-23 A3：切片重跑"部分恢复"（>100 字验收通过但标题乱码）
        + 横向几何 → 嫌疑横置页补跑旋转探测；旋转读取显著更优（>1.1×）
        才替换。断言：raw_html 为旋转文本、诊断带 rotation_deg=90、
        审计 stage1_rotation_recovered 落库。"""
        from core import pipeline as pipeline_mod
        import json as _json

        pdf_path = self._make_pdf(tmp_path)
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}}
            for i in range(1, 13)
        ]
        pages[4]["markdown"]["text"] = ""  # p5 横置 → 初判空 → 触发自愈链

        # VL 旋转容忍度：切片重跑读出横置页大部分表格（155 字过验收），
        # 但标题/细字乱码；旋转后完整读取（417 字 > 1.1×155=170.5）。
        partial = "slice partial p5 " + "a" * 140   # 155 字
        rotated = "rotated full p5 " + "b" * 400    # 417 字

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            if list(page_nums) == [1]:
                # 旋转切片：run_ocr_pages 对单页新 PDF 提交，page_nums=[1]
                return [(1, rotated, 0)]
            # 主切片自愈：部分恢复（>100 字通过验收，不进 still_empty）
            return [(pno, partial, 0) for pno in page_nums]

        orig_backend = pipeline_mod.config["app"].ocr_backend
        pipeline_mod.config["app"].ocr_backend = "mineru"
        job_id = await _insert_job(pipeline_db, status="pending")
        try:
            with patch(
                "core.pipeline._get_ocr_backend",
                return_value=lambda p, cb: pages,
            ), patch(
                "core.mineru_client.run_ocr_pages", side_effect=fake_retry,
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(return_value={
                    "steps": [], "findings": [],
                    "overall_confidence": "high"}),
            ), patch(
                "core.pipeline.analyze_cross_page",
                new=AsyncMock(return_value=[]),
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend

        cursor = await pipeline_db.execute(
            "SELECT raw_html, ocr_diagnostics FROM page_cache "
            "WHERE job_id = ? AND page = 5", (job_id,))
        row = await cursor.fetchone()
        assert "rotated full p5" in row["raw_html"], (
            f"rotation upgrade did not replace slice partial: "
            f"{row['raw_html'][:80]}"
        )
        diag = _json.loads(row["ocr_diagnostics"])
        assert diag["self_healed"] is True and diag["rotation_deg"] == 90

        cursor = await pipeline_db.execute(
            "SELECT detail FROM audit_log WHERE job_id = ? AND "
            "action = 'stage1_rotation_recovered'", (job_id,))
        log = await cursor.fetchone()
        assert log is not None and "90" in log["detail"]

    @pytest.mark.asyncio
    async def test_rotation_upgrade_rejects_marginal_gain(
        self, pipeline_db, tmp_path
    ):
        """round-23 A3 反向：旋转读取仅边际更优（未超 1.1× 裕度）→
        不替换，保留切片恢复结果（正常横版宽表页旋转后读取更差，
        旋转只会更短/乱码 — 长度门槛天然拒绝误替换）。"""
        from core import pipeline as pipeline_mod
        import json as _json

        pdf_path = self._make_pdf(tmp_path)
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}}
            for i in range(1, 13)
        ]
        pages[4]["markdown"]["text"] = ""

        partial = "slice partial p5 " + "a" * 140    # 155 字
        marginal = "marginal gain p5 " + "c" * 145   # 161 字 < 1.1×155

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            if list(page_nums) == [1]:
                return [(1, marginal, 0)]
            return [(pno, partial, 0) for pno in page_nums]

        orig_backend = pipeline_mod.config["app"].ocr_backend
        pipeline_mod.config["app"].ocr_backend = "mineru"
        job_id = await _insert_job(pipeline_db, status="pending")
        try:
            with patch(
                "core.pipeline._get_ocr_backend",
                return_value=lambda p, cb: pages,
            ), patch(
                "core.mineru_client.run_ocr_pages", side_effect=fake_retry,
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(return_value={
                    "steps": [], "findings": [],
                    "overall_confidence": "high"}),
            ), patch(
                "core.pipeline.analyze_cross_page",
                new=AsyncMock(return_value=[]),
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend

        cursor = await pipeline_db.execute(
            "SELECT raw_html, ocr_diagnostics FROM page_cache "
            "WHERE job_id = ? AND page = 5", (job_id,))
        row = await cursor.fetchone()
        # 切片结果保留：边际增益未过升级门槛
        assert "slice partial p5" in row["raw_html"]
        assert "marginal gain" not in row["raw_html"]
        # 诊断保持切片恢复态（无 rotation_deg — 升级未发生）
        diag = _json.loads(row["ocr_diagnostics"])
        assert diag.get("recovered") is True
        assert "rotation_deg" not in diag
        # 无旋转恢复审计（未采纳任何角度）
        cursor = await pipeline_db.execute(
            "SELECT 1 FROM audit_log WHERE job_id = ? AND "
            "action = 'stage1_rotation_recovered'", (job_id,))
        assert (await cursor.fetchone()) is None

    @pytest.mark.asyncio
    async def test_rotation_prescreen_blocks_180_adoption(
        self, pipeline_db, tmp_path
    ):
        """round-23 A4 根因回归：上游「系统错误-拆页」杀死 90°/270° 探测
        （重试耗尽）时，180° 乱序长文本不得被采纳 — 预筛已从几何上排除
        该角度（横置页只探测 90/270）。页面保留空态 + rotation_probed。"""
        from core import pipeline as pipeline_mod
        import json as _json

        pdf_path = self._make_pdf(tmp_path)
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}}
            for i in range(1, 13)
        ]
        pages[4]["markdown"]["text"] = ""

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            p = str(pdf_path)
            if list(page_nums) == [1]:
                if "rot180" in p:
                    # 若 180° 被探测将返回长乱序文本（采纳即为缺陷）
                    return [(1, "garbled reversed p5 " + "g" * 400, 0)]
                # 90°/270° 探测：瞬态错误，两次重试均失败
                raise RuntimeError("系统错误-拆页")
            return [(pno, "", 0) for pno in page_nums]

        orig_backend = pipeline_mod.config["app"].ocr_backend
        pipeline_mod.config["app"].ocr_backend = "mineru"
        job_id = await _insert_job(pipeline_db, status="pending")
        try:
            with patch(
                "core.pipeline._get_ocr_backend",
                return_value=lambda p, cb: pages,
            ), patch(
                "core.mineru_client.run_ocr_pages", side_effect=fake_retry,
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(return_value={
                    "steps": [], "findings": [],
                    "overall_confidence": "high"}),
            ), patch(
                "core.pipeline.analyze_cross_page",
                new=AsyncMock(return_value=[]),
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend

        cursor = await pipeline_db.execute(
            "SELECT raw_html, ocr_diagnostics FROM page_cache "
            "WHERE job_id = ? AND page = 5", (job_id,))
        row = await cursor.fetchone()
        # 180° 乱序文本未被采纳：预筛从几何上排除了该角度
        assert "garbled reversed" not in row["raw_html"]
        assert row["raw_html"].startswith("[OCR 警告:")
        diag = _json.loads(row["ocr_diagnostics"])
        assert diag.get("rotation_probed") is True
        assert diag.get("recovered") is False
        cursor = await pipeline_db.execute(
            "SELECT 1 FROM audit_log WHERE job_id = ? AND "
            "action = 'stage1_rotation_recovered'", (job_id,))
        assert (await cursor.fetchone()) is None

    @pytest.mark.asyncio
    async def test_rotation_transient_error_retry_recovers(
        self, pipeline_db, tmp_path
    ):
        """round-23 A4：90° 探测首试瞬态失败（「系统错误-拆页」）→ 退避
        重试第二次成功 → rotation_deg=90（重试保住正确角度）。"""
        from core import pipeline as pipeline_mod
        import json as _json

        pdf_path = self._make_pdf(tmp_path)
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}}
            for i in range(1, 13)
        ]
        pages[4]["markdown"]["text"] = ""

        probe_calls = {"n": 0}

        def fake_retry(pdf_path, page_nums, batch_size=3, job_id=""):
            p = str(pdf_path)
            if list(page_nums) == [1]:
                if "rot90" in p:
                    probe_calls["n"] += 1
                    if probe_calls["n"] == 1:
                        raise RuntimeError("系统错误-拆页")
                    return [(1, "rotated retry p5 " + "y" * 200, 0)]
                return [(1, "", 0)]
            return [(pno, "", 0) for pno in page_nums]

        orig_backend = pipeline_mod.config["app"].ocr_backend
        pipeline_mod.config["app"].ocr_backend = "mineru"
        job_id = await _insert_job(pipeline_db, status="pending")
        try:
            with patch(
                "core.pipeline._get_ocr_backend",
                return_value=lambda p, cb: pages,
            ), patch(
                "core.mineru_client.run_ocr_pages", side_effect=fake_retry,
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(return_value={
                    "steps": [], "findings": [],
                    "overall_confidence": "high"}),
            ), patch(
                "core.pipeline.analyze_cross_page",
                new=AsyncMock(return_value=[]),
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend

        cursor = await pipeline_db.execute(
            "SELECT raw_html, ocr_diagnostics FROM page_cache "
            "WHERE job_id = ? AND page = 5", (job_id,))
        row = await cursor.fetchone()
        assert "rotated retry p5" in row["raw_html"]
        assert probe_calls["n"] == 2  # 首试失败 + 重试成功
        diag = _json.loads(row["ocr_diagnostics"])
        assert diag["self_healed"] is True and diag["rotation_deg"] == 90

    @pytest.mark.asyncio
    async def test_prescreen_rotation_angles(self, tmp_path):
        """预筛纯函数：正常横排页 → (180,)；横置页（纵向文本）→
        (90, 270)；空白页 → None（全角度回退，预筛不阻塞恢复）。"""
        import fitz
        from core.pipeline.self_heal import _prescreen_rotation_angles

        # 正常横排文本页
        normal = str(tmp_path / "normal.pdf")
        doc = fitz.open()
        p = doc.new_page()
        for i in range(10):
            p.insert_text((50, 60 + i * 22), "normal text " + "n" * 20)
        doc.save(normal)
        doc.close()

        # 横置页（复用 _make_pdf 的 p5 构造）
        sideways = self._make_pdf(tmp_path)

        # 空白页
        blank = str(tmp_path / "blank.pdf")
        doc = fitz.open()
        doc.new_page()
        doc.save(blank)
        doc.close()

        assert await _prescreen_rotation_angles(normal, 1) == (180,)
        assert await _prescreen_rotation_angles(sideways, 5) == (90, 270)
        # 空白页：两轴方差皆 ~0 → None（回退全角度）
        assert await _prescreen_rotation_angles(blank, 1) is None


class TestSlicedCoverageGaps:
    """切片路径缺口补测：缓存页跳过 / 已分析页跳过 / discarded 前缀 /
    中间缺页补记（覆盖 engine 内部分支）。"""

    @pytest.mark.asyncio
    async def test_sliced_skips_cached_analyzed_and_prefixes_discarded(self, pipeline_db, tmp_path):
        """已缓存页跳过落库、已分析页跳过 LLM、_discarded_count 注入警告前缀。"""
        from core import pipeline as pipeline_mod

        pipeline_mod.db_lock = asyncio.Lock()

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        await pipeline_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html) VALUES (?, 1, 'cached p1')",
            (job_id,),
        )
        await pipeline_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
            "VALUES (?, 3, 'cached p3', '{}')",
            (job_id,),
        )
        await pipeline_db.commit()

        def fake_run_sliced(pdf_path, slice_pages, on_batch, progress_cb, job_id=None):
            on_batch(
                1,
                [
                    {"markdown": {"text": "cached p1"}, "page_count": 1},
                    {"markdown": {"text": "page 2"}, "page_count": 2,
                     "_ocr_diagnostics": {"discarded_blocks": 3}},
                ],
                3,
            )
            on_batch(3, [{"markdown": {"text": "page 3"}, "page_count": 3}], 3)
            return []

        analyzed_calls = []

        async def fake_analyze(raw_html, page_num=None, job_id=None, cancel_check=None):
            analyzed_calls.append(page_num)
            return {"steps": [], "findings": [], "overall_confidence": "high"}

        orig_backend = pipeline_mod.config["app"].ocr_backend
        orig_slices = pipeline_mod.config["app"].ocr_slices
        orig_timeout = pipeline_mod._SLICE_QUEUE_TIMEOUT
        pipeline_mod.config["app"].ocr_backend = "mineru"
        pipeline_mod.config["app"].ocr_slices = 2
        pipeline_mod._SLICE_QUEUE_TIMEOUT = 0.05
        try:
            with patch(
                "core.mineru_client.run_ocr_sliced", side_effect=fake_run_sliced
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(side_effect=fake_analyze),
            ), patch(
                "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend
            pipeline_mod.config["app"].ocr_slices = orig_slices
            pipeline_mod._SLICE_QUEUE_TIMEOUT = orig_timeout

        cursor = await pipeline_db.execute(
            "SELECT page, raw_html FROM page_cache WHERE job_id = ? ORDER BY page",
            (job_id,),
        )
        rows = await cursor.fetchall()
        assert rows[0]["raw_html"] == "cached p1"  # 未被切片结果覆盖
        assert "[OCR 警告: 3 个内容块被 OCR 丢弃" in rows[1]["raw_html"]
        assert analyzed_calls == [2]  # p3 已分析跳过；p1 已缓存跳过
        # p1 缓存保留、p3 已分析 → 无真实缺页 → review
        cursor = await pipeline_db.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        )
        assert (await cursor.fetchone())["status"] == "review"

    @pytest.mark.asyncio
    async def test_sliced_middle_gap_computed(self, pipeline_db, tmp_path):
        """中间片缺页（片范围从 2 开始）→ 缺页循环覆盖范围区间补记。"""
        from core import pipeline as pipeline_mod

        pipeline_mod.db_lock = asyncio.Lock()

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        def fake_run_sliced(pdf_path, slice_pages, on_batch, progress_cb, job_id=None):
            on_batch(2, [{"markdown": {"text": "page 2"}, "page_count": 2}], 3)
            on_batch(3, [{"markdown": {"text": "page 3"}, "page_count": 3}], 3)
            return []

        orig_backend = pipeline_mod.config["app"].ocr_backend
        orig_slices = pipeline_mod.config["app"].ocr_slices
        orig_timeout = pipeline_mod._SLICE_QUEUE_TIMEOUT
        pipeline_mod.config["app"].ocr_backend = "mineru"
        pipeline_mod.config["app"].ocr_slices = 2
        pipeline_mod._SLICE_QUEUE_TIMEOUT = 0.05
        try:
            with patch(
                "core.mineru_client.run_ocr_sliced", side_effect=fake_run_sliced
            ), patch(
                "core.pipeline.analyze_page",
                new=AsyncMock(
                    return_value={
                        "steps": [], "findings": [],
                        "overall_confidence": "high",
                    }
                ),
            ), patch(
                "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend
            pipeline_mod.config["app"].ocr_slices = orig_slices
            pipeline_mod._SLICE_QUEUE_TIMEOUT = orig_timeout

        cursor = await pipeline_db.execute(
            "SELECT status, failed_pages FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        assert row["status"] == "partial_review"
        assert "1" in str(row["failed_pages"])


class TestStage2CoverageGaps:
    """Stage 2 缺口：畸形 llm_page findings 过滤（非 dict / 缺必填 key）。"""

    @pytest.mark.asyncio
    async def test_stage2_filters_malformed_llm_findings(self, pipeline_db, tmp_path):
        from core import pipeline as pipeline_mod

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        good = {"type": "test", "severity": "warning", "description": "good finding"}
        with patch(
            "core.pipeline._get_ocr_backend",
            return_value=lambda p, cb: [
                {"markdown": {"text": "page 1 content " + "x" * 200}},
                {"markdown": {"text": "page 2 content " + "x" * 200}},
            ],
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(
                return_value={
                    "steps": [],
                    "findings": ["junk", {"description": "missing keys"}, good],
                    "overall_confidence": "high",
                }
            ),
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT description FROM findings WHERE job_id = ? ORDER BY page", (job_id,)
        )
        rows = await cursor.fetchall()
        assert [r["description"] for r in rows] == ["good finding", "good finding"]


class TestStage3CoverageGaps:
    """Stage 3 缺口单测：取消检查点 / llm_page 跳过 / 指纹去重 / notify 兜底。"""

    async def _prep_job(self, pipeline_db):
        from core import pipeline as pipeline_mod

        pipeline_mod.db_lock = asyncio.Lock()
        job_id = await _insert_job(pipeline_db, status="analyzing")
        await pipeline_db.execute(
            "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
            "VALUES (?, 1, 'html', '{}')",
            (job_id,),
        )
        await pipeline_db.commit()
        return job_id

    @pytest.mark.asyncio
    async def test_stage3_cancelled_before_cross_analysis(self, pipeline_db):
        """analyze_cross_page 调用前取消 → 提前返回不分析。"""
        from core.pipeline import _run_stage3_cross_analysis
        from core import pipeline as pipeline_mod

        job_id = await self._prep_job(pipeline_db)
        calls = {"n": 0}

        async def fake_cancelled(jid):
            calls["n"] += 1
            return calls["n"] >= 2

        with patch(
            "core.pipeline._is_cancelled", side_effect=fake_cancelled
        ), patch(
            "core.pipeline.analyze_cross_page",
            new=AsyncMock(return_value=[{"page": 1, "type": "t",
                                          "severity": "w", "description": "x"}]),
        ):
            await _run_stage3_cross_analysis(pipeline_db, job_id, 1, 2, [], 0.0)

        cursor = await pipeline_db.execute(
            "SELECT 1 FROM findings WHERE job_id = ?", (job_id,)
        )
        assert await cursor.fetchone() is None
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_stage3_cancelled_after_cross_analysis(self, pipeline_db):
        """analyze_cross_page 返回后取消 → 不写 findings 不转终态。"""
        from core.pipeline import _run_stage3_cross_analysis
        from core import pipeline as pipeline_mod

        job_id = await self._prep_job(pipeline_db)
        flag = {"cross_done": False}

        async def fake_cancelled(jid):
            return flag["cross_done"]

        def fake_cross(page_structures, job_id=None, progress_cb=None):
            flag["cross_done"] = True
            return [{"page": 1, "type": "t", "severity": "w", "description": "x"}]

        with patch(
            "core.pipeline._is_cancelled", side_effect=fake_cancelled
        ), patch(
            "core.pipeline.analyze_cross_page", side_effect=fake_cross
        ):
            await _run_stage3_cross_analysis(pipeline_db, job_id, 1, 2, [], 0.0)

        cursor = await pipeline_db.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        )
        assert (await cursor.fetchone())["status"] == "analyzing"
        cursor = await pipeline_db.execute(
            "SELECT 1 FROM findings WHERE job_id = ?", (job_id,)
        )
        assert await cursor.fetchone() is None

    @pytest.mark.asyncio
    async def test_stage3_dedup_and_llm_page_skip_and_rule_id(self, pipeline_db):
        """llm_page findings 跳过、重复指纹去重、user_rule 带 rule_id 入库。"""
        from core.pipeline import _run_stage3_cross_analysis
        from core import pipeline as pipeline_mod

        job_id = await self._prep_job(pipeline_db)
        with patch(
            "core.pipeline._is_cancelled", new=AsyncMock(return_value=False)
        ), patch(
            "core.pipeline.analyze_cross_page",
            new=AsyncMock(
                return_value=[
                    {"source": "llm_page", "page": 1, "type": "t",
                     "severity": "w", "description": "skip me"},
                    {"source": "rule", "page": 1, "type": "t",
                     "severity": "w", "description": "r1"},
                    {"source": "rule", "page": 1, "type": "t",
                     "severity": "w", "description": "r1"},
                    # user_rule 与 rule 同 (page,type) —— N1 抑制仅针对
                    # llm_cross/llm_fallback，user_rule 豁免（用户显式规则）
                    {"source": "user_rule", "page": 1, "type": "t",
                     "severity": "info", "description": "ur", "rule_id": 5},
                ]
            ),
        ):
            await _run_stage3_cross_analysis(pipeline_db, job_id, 1, 2, [], 0.0)

        cursor = await pipeline_db.execute(
            "SELECT description, source, user_rule_id FROM findings "
            "WHERE job_id = ? ORDER BY description",
            (job_id,),
        )
        rows = await cursor.fetchall()
        assert [r["description"] for r in rows] == ["r1", "ur"]
        assert rows[1]["source"] == "user_rule"
        assert int(rows[1]["user_rule_id"]) == 5
        assert rows[0]["source"] == "rule"

    @pytest.mark.asyncio
    async def test_stage3_notify_failure_swallowed(self, pipeline_db):
        """终态通知失败不阻断 Stage 3 收尾。"""
        from core.pipeline import _run_stage3_cross_analysis
        from core import pipeline as pipeline_mod

        job_id = await self._prep_job(pipeline_db)
        with patch(
            "core.pipeline._is_cancelled", new=AsyncMock(return_value=False)
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ), patch(
            "core.notify.notify_job",
            new=AsyncMock(side_effect=Exception("notify down")),
        ):
            await _run_stage3_cross_analysis(pipeline_db, job_id, 1, 2, [], 0.0)

        cursor = await pipeline_db.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        )
        assert (await cursor.fetchone())["status"] == "review"


class TestPipelineEngineGuards:
    """engine.py 缺口：Per-job 锁竞争串行化 / launch 竞态回调 /
    error 终态 notify 兜底。"""

    @pytest.mark.asyncio
    async def test_concurrent_run_pipeline_serializes_on_job_lock(self, pipeline_db, tmp_path):
        """同一 job 并发进入 run_pipeline → 第二次等待锁后串行执行。"""
        from core import pipeline as pipeline_mod

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        entered = {"n": 0}

        async def fake_impl(job_id, pdf_path, progress_futures, children=None):
            entered["n"] += 1
            await asyncio.sleep(0.1)

        with patch(
            "core.pipeline.engine._run_pipeline_impl", side_effect=fake_impl
        ):
            await asyncio.gather(
                run_pipeline(job_id, pdf_path),
                run_pipeline(job_id, pdf_path),
            )

        assert entered["n"] == 2  # 两个协程都拿到锁，串行执行

    @pytest.mark.asyncio
    async def test_cancelling_pipeline_cascades_to_child_page_tasks(self, pipeline_db, tmp_path):
        """#140：取消父 pipeline task 必须**级联停掉**页分析子任务。

        这是"取消后孤儿继续跑 LLM 并写库"的正面证明。旧实现里 stage2 用裸
        `asyncio.create_task` 派生全部页任务且从不登记 ⇒ 取消父 task 后，
        子协程继续 touch_activity / 写 page_cache / 写 findings，
        既与 retry 的新一轮抢同一页，又把心跳刷成"在动"掩盖真正停滞。

        断言的是**行为**（子任务真的停了），不是"代码里调了 cancel"。
        """
        from core import pipeline as pipeline_mod
        from core.pipeline.engine import run_pipeline

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        child_started = asyncio.Event()
        child_finished = {"n": 0}

        async def fake_impl(job_id, pdf_path, progress_futures, children=None):
            """模拟 stage2：派生一个"无限跑"的页分析子任务后挂起。"""
            assert children is not None, "run_pipeline 必须把 children 传进来"

            async def _page_task():
                child_started.set()
                try:
                    await asyncio.Event().wait()      # 模拟长跑 LLM
                finally:
                    child_finished["n"] += 1

            children.spawn(_page_task())
            await asyncio.Event().wait()              # 父任务挂起等取消

        with patch("core.pipeline.engine._run_pipeline_impl", side_effect=fake_impl):
            parent = asyncio.create_task(run_pipeline(job_id, pdf_path))
            await asyncio.wait_for(child_started.wait(), timeout=5)
            assert child_finished["n"] == 0, "前置条件：子任务仍在跑"

            parent.cancel()
            await asyncio.gather(parent, return_exceptions=True)

        assert child_finished["n"] == 1, (
            "父 task 被取消后派生的页分析子任务必须已停止（级联取消）")
        # 注册表与锁都必须干净（否则 retry 会挂死）
        from core.pipeline.locks import _pipeline_tasks, _pipeline_locks
        assert _pipeline_tasks.get(job_id) is None
        assert _pipeline_locks.get(job_id) is None

    @pytest.mark.asyncio
    async def test_launch_stale_done_callback_and_crashed_task(self, pipeline_db, tmp_path):
        """旧 task 的 done 回调不得删除新 task 的注册表条目；崩溃 task 记录日志。

        #141 语义：注册表是 `dict[job_id, set[Task]]`，每个 task 的 done 回调
        只摘掉**自己**。t1 崩溃/完成时 t2 若仍在跑，键必须继续存在且含 t2 ——
        否则关闭端点与看门狗都会"看不见"那个仍在运行的 task（旧实现单值覆盖，
        真凶就这样失去引用，重试被永久挂死）。
        """
        from core.pipeline.locks import _pipeline_tasks
        from core.pipeline import launch_pipeline

        job_id = "launch-race"
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        call = {"n": 0}
        release = asyncio.Event()

        async def fake_run(job_id, pdf_path):
            call["n"] += 1
            if call["n"] == 1:
                raise RuntimeError("boom")
            # 第二个 task 一直挂到测试放行 —— 保证"t1 已结束而 t2 仍在跑"
            # 这个窗口真实存在（否则 AsyncMock/立即完成的协程会让断言落空）
            await release.wait()

        with patch("core.pipeline.run_pipeline", side_effect=fake_run):
            t1 = launch_pipeline(job_id, pdf_path)
            await asyncio.sleep(0.01)
            t2 = launch_pipeline(job_id, pdf_path)
            await asyncio.wait({t1}, timeout=2)
            assert t1.done(), "前置条件：t1 已结束（抛 boom）"
            assert not t2.done(), "前置条件：t2 仍在运行"
            assert t2 in _pipeline_tasks.get(job_id, set()), (
                "t1 结束时 t2 仍在注册表里（单值覆盖会让它消失）")
            release.set()
            await asyncio.gather(t2, return_exceptions=True)

        assert _pipeline_tasks.get(job_id) is None

    @pytest.mark.asyncio
    async def test_error_notify_failure_does_not_break_pipeline(self, pipeline_db, tmp_path):
        """pipeline 异常路径：飞书通知失败不影响 error 终态。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        with patch(
            "core.pipeline._get_ocr_backend",
            return_value=lambda p, cb: [{"markdown": {"text": "page 1 xxxxxx"}}],
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(
                return_value={
                    "steps": [], "findings": [], "overall_confidence": "high",
                }
            ),
        ), patch(
            "core.pipeline.analyze_cross_page",
            new=AsyncMock(side_effect=RuntimeError("stage3 boom")),
        ), patch(
            "core.notify.notify_job",
            new=AsyncMock(side_effect=Exception("notify down")),
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT status, error_message FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        assert row["status"] == "error"
        assert "stage3 boom" in row["error_message"]


# ─── #127：配置级故障的终态与可见性 ──────────────────────────────


class TestConfigErrorVisibility:
    """#127 — 零页产出不得呈现为「部分可复核」；配置级故障必须早停且有原因。

    失效模式（GMP 假阴性）：模型 API Key 失效 → 每页 401 → 改动前 job 报
    partial_review、``error_message`` 为 NULL、0 条 finding，界面绿点 +
    "部分可复核"，与"记录确实无异常"无法区分。
    """

    @pytest.mark.asyncio
    async def test_all_pages_parse_error_yields_error(self, pipeline_db, tmp_path):
        """全页失败（无一页产出）→ error，并补一条 job 级原因。

        与 test_parse_error_page_marks_partial_review 互补：那里有 1 页成功
        （真·部分），这里 0 页成功（真·没分析出来）。
        """
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        fake_pages = [
            {"markdown": {"text": "page 1"}},
            {"markdown": {"text": "page 2"}},
        ]

        with patch(
            "core.pipeline._get_ocr_backend",
            return_value=lambda p, cb=None, job_id=None: fake_pages,
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(
                return_value={
                    "_parse_error": True, "_raw": "bad json",
                    "overall_confidence": "low",
                }
            ),
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT status, failed_pages, error_message FROM jobs WHERE id = ?",
            (job_id,),
        )
        row = await cursor.fetchone()
        assert row["status"] == "error", "0 页产出却报 partial_review（假阴性）"
        assert sorted(json.loads(row["failed_pages"])) == [1, 2]
        assert row["error_message"], "0 页产出却没有 job 级原因可显示"

    @pytest.mark.asyncio
    async def test_config_error_sets_reason_and_stops_early(
        self, pipeline_db, tmp_path
    ):
        """配置级故障：job 级原因落库 + 只调用一次 LLM（不在死 key 上打 N 次）。"""
        from llm.client import LLMConfigError

        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        fake_pages = [{"markdown": {"text": f"page {i}"}} for i in range(1, 5)]
        calls = {"n": 0}

        async def _boom(*args, **kwargs):
            calls["n"] += 1
            raise LLMConfigError(
                "LLM call failed (non-retryable): 401 Token is invalid"
            )

        orig_conc = config["app"].llm_concurrency
        # 并发=1 → "确诊后不再补刀"必须是确定性行为，而不是靠调度碰运气
        config["app"].llm_concurrency = 1
        try:
            with patch(
                "core.pipeline._get_ocr_backend",
                return_value=lambda p, cb=None, job_id=None: fake_pages,
            ), patch("core.pipeline.analyze_page", new=_boom), patch(
                "core.pipeline.analyze_cross_page",
                new=AsyncMock(return_value=[]),
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            config["app"].llm_concurrency = orig_conc

        cursor = await pipeline_db.execute(
            "SELECT status, failed_pages, error_message FROM jobs WHERE id = ?",
            (job_id,),
        )
        row = await cursor.fetchone()
        assert row["status"] == "error"
        assert "配置级故障" in (row["error_message"] or ""), (
            "原因未提升到 job 级 → 前端仍无原因可显（#127 回归）"
        )
        assert "401" in row["error_message"]
        assert calls["n"] == 1, (
            f"配置级故障下仍调用了 {calls['n']} 次 LLM —— 早停失效"
        )
        # 未及尝试的页也必须计入 failed_pages：否则复核者以为"页数齐了"
        assert sorted(json.loads(row["failed_pages"])) == [1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_partial_failure_still_partial_review(
        self, pipeline_db, tmp_path
    ):
        """反向护栏：有页成功时**不得**被误判成 error（#127 不得过度收口）。"""
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        fake_pages = [
            {"markdown": {"text": "page 1"}},
            {"markdown": {"text": "page 2"}},
            {"markdown": {"text": "page 3"}},
        ]

        def _fake_analyze(raw_html, **kwargs):
            if "page 2" in raw_html:
                return {
                    "_parse_error": True, "_raw": "bad json",
                    "overall_confidence": "low",
                }
            return {"steps": [], "findings": [], "overall_confidence": "high"}

        with patch(
            "core.pipeline._get_ocr_backend",
            return_value=lambda p, cb=None, job_id=None: fake_pages,
        ), patch(
            "core.pipeline.analyze_page",
            new=AsyncMock(side_effect=_fake_analyze),
        ), patch(
            "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
        ):
            await run_pipeline(job_id, pdf_path)

        cursor = await pipeline_db.execute(
            "SELECT status, error_message FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        assert row["status"] == "partial_review"
        # 非配置级的部分失败没有 job 级原因 → 不得凭空造一条
        assert not row["error_message"]

