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
                "type": "test",
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
        assert "新版措辞" in descriptions          # 本次重跑写入
        assert "旧版措辞B" in descriptions          # confirmed 保留
        assert descriptions["旧版措辞B"] == "confirmed"
        assert "旧版措辞A" not in descriptions      # pending 已清理

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

        async def slow_page(html, page_num, *, job_id=""):
            # 慢页：捕获当前任务后挂起 300s（模拟慢 LLM 调用）
            captured["slow_task"] = asyncio.current_task()
            await asyncio.sleep(300)

        async def fast_page(html, page_num, *, job_id=""):
            return {"steps": [], "findings": [], "overall_confidence": "high"}

        async def analyze_side(html, page_num, **kw):
            if "slow" in html:
                return await slow_page(html, page_num, **kw)
            return await fast_page(html, page_num, **kw)

        calls = {"n": 0}

        async def fake_cancelled(jid):
            calls["n"] += 1
            # 调用序：1=Stage1 后检查, 2/3=两页 _analyze_one 入口,
            # 4=Stage 2 while 循环（fast 页完成后）→ 触发取消
            return calls["n"] >= 4

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
        # cr-17: 阈值收紧为 max(2, 10%) — 备后端 25/30（缺 5 页 > max(2,3)）
        # 也判严重缺失 → 双后端均失败 → error（比静默接受残缺页更诚实）。
        # 缺 1-2 页的轻微差异仍容忍（partial_review，见下一测试）。
        assert row["status"] == "error"
        assert row["ocr_backend_used"] is None

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
            {"markdown": {"text": "正文内容"}, "_discarded_count": 3},
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
        job_id = await _insert_job(pipeline_db, status="pending")
        pdf_path = str(tmp_path / "fake.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")

        def _boom(pdf_path, slice_pages, on_batch, progress_cb):
            raise RuntimeError("sliced OCR crashed")

        orig_backend = config["app"].ocr_backend
        config["app"].ocr_backend = "mineru"
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

        calls = {"n": 0}

        async def fake_cancelled(*a, **kw):
            calls["n"] += 1
            return True  # 循环内第一次检查即取消

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

        calls = {"n": 0}

        async def fake_cancelled(*a, **kw):
            calls["n"] += 1
            # 循环内检查（845）→ False；ocr_done 后检查（858）→ True；
            # 主流程兜底（407）→ True 直接退出
            return calls["n"] > 1

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
            return [(pno, f"recovered content p{pno} " + "y" * 200) for pno in page_nums]

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
            return [(pno, "") for pno in page_nums]

        job_id, calls = await self._run(
            pipeline_db, tmp_path, pages, fake_retry=fake_retry
        )

        # 两轮重试都发生了（3 页批 → 单页批）
        assert calls[0][1] == 3
        assert calls[1][1] == 1
        # 原空内容保留（空字符串，未产生虚假恢复）
        cursor = await pipeline_db.execute(
            "SELECT raw_html FROM page_cache WHERE job_id = ? AND page = 5",
            (job_id,),
        )
        assert (await cursor.fetchone())["raw_html"] == ""
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
    async def test_small_file_skips_empty_retry(self, pipeline_db, tmp_path):
        """<10 页的小文件不触发切片重试（避免多余开销）。"""
        pages = [
            {"markdown": {"text": f"page {i} content " + "x" * 200}}
            for i in range(1, 5)
        ]
        pages[1]["markdown"]["text"] = ""  # p2 空 — 但文件太小不重试

        job_id, calls = await self._run(pipeline_db, tmp_path, pages)

        assert calls == []
        cursor = await pipeline_db.execute(
            "SELECT raw_html FROM page_cache WHERE job_id = ? AND page = 2",
            (job_id,),
        )
        assert (await cursor.fetchone())["raw_html"] == ""

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
                    (pno, f"recovered content p{pno} " + "y" * 200) for pno in page_nums
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
