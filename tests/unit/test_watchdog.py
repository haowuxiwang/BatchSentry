"""运行时看门狗（v12）回归测试。

三层覆盖：
1. **纯函数契约**（阈值分级 / 时间解析）—— 数值本身即契约，锁死防被悄悄放宽；
2. **DB 判定与收敛**（只读扫描 / 条件更新 / 审计 / 通知 / 并发跳过 / 不可判定跳过）；
3. **心跳写入点**（状态迁移 / 进度更新 / 显式 touch）+ schema 单一真值 + 迁移。

背景与阈值依据见 `docs/RUNTIME_WATCHDOG.md`；模块契约见 `core/watchdog.py`。
"""

from datetime import datetime, timedelta

import aiosqlite
import pytest

from core import watchdog
from core.pipeline.state import _STUCK_STATUSES


def _stale(seconds: float = 999_999) -> str:
    return (datetime.now() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _fresh() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def _insert_job(db, job_id="job-1", status="ocr_running",
                      total_pages=51, last_activity_at=None):
    await db.execute(
        "INSERT INTO jobs (id, filename, status, total_pages, last_activity_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, "x.pdf", status, total_pages, last_activity_at),
    )
    await db.commit()


class TestStallLimitContract:
    """阈值分级是看门狗唯一的"判据"，数值必须锁死。"""

    def test_pending_is_never_watched(self):
        """运行期间 pending 是合法排队态 —— 默认必须永不判定（否则误杀排队上传）。

        第二轮回审细化了契约：**唯一例外**是"pending 且已被 pipeline 接管"
        （注册表里有未完成 task）；合法排队永远没有注册 task，见
        ``TestPendingTakeover``。
        """
        assert not watchdog.is_watched("pending")
        assert watchdog.stall_limit_seconds("pending", 51) == float("inf")

    def test_pending_with_live_task_is_watched(self, monkeypatch):
        """被接管的 pending 必须可判定 —— 否则 retry 卡在 per-job 锁上时永久无声。"""
        monkeypatch.delenv("PBC_WATCHDOG_SCALE", raising=False)
        assert watchdog.is_watched("pending", has_live_task=True)
        assert watchdog.stall_limit_seconds(
            "pending", 1, has_live_task=True) == watchdog._PENDING_TAKEOVER_STALL_S
        # 未接管仍不判（同一状态，两个相反结论 —— 防"顺手把 pending 加进监视集"）
        assert watchdog.stall_limit_seconds("pending", 1) == float("inf")

    def test_unknown_status_unbounded(self):
        assert watchdog.stall_limit_seconds("nonsense", 10) == float("inf")
        assert watchdog.stall_limit_seconds("review", 10) == float("inf")

    def test_watchdog_statuses_are_stuck_minus_pending(self):
        """监视集必须由 _STUCK_STATUSES 派生（本项目明令禁止重复硬编码状态串）。"""
        assert set(watchdog._WATCHDOG_STATUSES) <= set(_STUCK_STATUSES)
        assert set(_STUCK_STATUSES) - set(watchdog._WATCHDOG_STATUSES) == {"pending"}
        assert "pending" not in watchdog._WATCHDOG_STATUSES

    def test_ocr_running_scales_with_pages(self, monkeypatch):
        monkeypatch.delenv("PBC_WATCHDOG_SCALE", raising=False)
        # 基准 4200 = POLL_TIMEOUT_MAX(3600) + 余量 600（不变式见
        # TestThresholdsAboveUpstreamCaps）
        assert watchdog.stall_limit_seconds("ocr_running", 4) == 4200 + 120 * 4
        assert watchdog.stall_limit_seconds("ocr_running", 51) == 4200 + 120 * 51

    def test_analyzing_hits_cap_on_large_docs(self, monkeypatch):
        monkeypatch.delenv("PBC_WATCHDOG_SCALE", raising=False)
        assert watchdog.stall_limit_seconds("analyzing", 4) == 1800 + 180 * 4
        # 51 页裸算 10980 > 封顶 10800
        assert watchdog.stall_limit_seconds("analyzing", 51) == 10800.0

    def test_transitional_and_cancel_limits(self, monkeypatch):
        monkeypatch.delenv("PBC_WATCHDOG_SCALE", raising=False)
        assert watchdog.stall_limit_seconds("ocr_done", 51) == 1800.0
        # 2400 = CPU 重活封顶 1800 + 余量 600（不变式见下一条用例）
        assert watchdog.stall_limit_seconds("cancelling", 999) == 2400.0

    def test_missing_or_zero_pages_uses_base(self, monkeypatch):
        monkeypatch.delenv("PBC_WATCHDOG_SCALE", raising=False)
        assert watchdog.stall_limit_seconds("ocr_running", None) == 4200.0
        assert watchdog.stall_limit_seconds("ocr_running", 0) == 4200.0

    def test_scale_env_multiplies_all(self, monkeypatch):
        monkeypatch.setenv("PBC_WATCHDOG_SCALE", "2")
        assert watchdog.stall_limit_seconds("ocr_running", 4) == (4200 + 480) * 2

    def test_scale_env_garbage_falls_back_to_one(self, monkeypatch):
        monkeypatch.setenv("PBC_WATCHDOG_SCALE", "abc")
        assert watchdog.stall_limit_seconds("ocr_running", 0) == 4200.0

    def test_thresholds_leave_room_for_measured_real_runs(self, monkeypatch):
        """实测基准守护：51 页真实件 OCR 644s / 逐页 LLM 825-1209s。

        阈值必须比实测值大一个量级 —— 否则一次上游拥堵就会误杀真实长任务。
        """
        monkeypatch.delenv("PBC_WATCHDOG_SCALE", raising=False)
        assert watchdog.stall_limit_seconds("ocr_running", 51) > 644 * 5
        assert watchdog.stall_limit_seconds("analyzing", 51) > 1209 * 5


class TestThresholdsAboveUpstreamCaps:
    """**阈值不变式**：基准必须 ≥ 它覆盖的上游调用自己的封顶。

    违反后果是**假阳性**（比晚判几分钟糟得多）：看门狗会抢在上游超时之前，
    把"上游还在正常等待"判成停滞并杀掉一个健康任务。

    这些数值全部**从真值源推导**，不写死 —— 上游封顶改了，这里自动跟着走；
    若看门狗基准被改小到封顶之下，本用例立刻失败。
    （2026-09-16 第二轮回审的来由：OCR 基准 1800s < POLL_TIMEOUT_MAX 3600s。）
    """

    def test_ocr_base_covers_upstream_poll_cap(self):
        from core.ocr_client import POLL_TIMEOUT_MAX

        assert watchdog._BASE_STALL_S["ocr_running"] >= POLL_TIMEOUT_MAX, (
            "OCR 基准不得低于上游轮询封顶 —— 否则会抢在上游超时前误判停滞"
        )

    def test_ocr_base_covers_cpu_task_cap(self):
        """Stage 0 规范化在 ocr_running 状态下跑，其超时封顶也要被覆盖。"""
        from core.procpool import cpu_task_timeout_seconds

        assert watchdog._BASE_STALL_S["ocr_running"] >= cpu_task_timeout_seconds()

    def test_cancel_base_covers_cpu_task_cap(self):
        """**取消语义不得被看门狗破坏**（第二轮回审第五条）。

        取消检查点只在 `run_cpu` **前后**（`stage1.py:49/54`），所以一个正在
        Stage 0 规范化的大文档会合法地停在 `cancelling` 上长达 CPU 封顶。
        阈值若低于它，看门狗会把"已请求取消"的 job 改成 `error` —— 而
        `engine.py` 明确禁止覆盖取消语义（会破坏取消审计链 + 重发通知）。
        """
        from core.procpool import cpu_task_timeout_seconds

        assert watchdog._BASE_STALL_S["cancelling"] >= cpu_task_timeout_seconds()
        # 余量必须真实存在（不是"刚好等于封顶"—— 那样边界抖动仍会误判）
        assert (watchdog._BASE_STALL_S["cancelling"]
                - cpu_task_timeout_seconds()) >= watchdog._STALL_MARGIN_S

    def test_ocr_limit_covers_single_page_remediation(self):
        """单页补救期间**没有**心跳（逐页上报），缺口上界必须落在阈值内。

        上界 = 候选角数 × 单页探测封顶（`poll_timeout_for` 对 1 页）
             = 3 × 630 = 1890s，再加该页重分析的余量。
        """
        from core.ocr_client import POLL_TIMEOUT, POLL_TIMEOUT_PER_PAGE, POLL_TIMEOUT_MAX
        from core.pipeline.self_heal import _ROTATION_CANDIDATES

        single_page_probe_cap = min(
            POLL_TIMEOUT + POLL_TIMEOUT_PER_PAGE * 1, POLL_TIMEOUT_MAX)
        worst_gap = single_page_probe_cap * len(_ROTATION_CANDIDATES)

        assert worst_gap > 0
        assert watchdog.stall_limit_seconds("ocr_running", 1) > worst_gap, (
            f"1 页文档的阈值 {watchdog.stall_limit_seconds('ocr_running', 1):.0f}s "
            f"不足以覆盖单页补救缺口 {worst_gap}s —— 会误杀"
        )

    def test_analyzing_base_covers_single_llm_call(self):
        """逐页 LLM 单次调用超时（适配器默认参数）必须被基准覆盖。"""
        import inspect

        from llm.adapters.base import LLMAdapter

        llm_timeout = inspect.signature(LLMAdapter.chat).parameters["timeout"].default
        assert watchdog._BASE_STALL_S["analyzing"] >= llm_timeout
        assert watchdog._PER_PAGE_S["analyzing"] >= llm_timeout


class TestElapsedSeconds:
    def test_valid_timestamp(self):
        got = watchdog.elapsed_seconds(
            _stale(600), now=datetime.now())
        assert 590 < got < 610

    def test_none_and_empty_return_none(self):
        assert watchdog.elapsed_seconds(None) is None
        assert watchdog.elapsed_seconds("") is None

    def test_unparseable_returns_none(self):
        assert watchdog.elapsed_seconds("not-a-date") is None
        assert watchdog.elapsed_seconds("2026-09-16T10:00:00") is None


class TestFindStalledJobs:
    @pytest.mark.asyncio
    async def test_stale_watched_job_is_reported(self, test_db):
        await _insert_job(test_db, "s1", status="analyzing", total_pages=4,
                          last_activity_at=_stale(10_000))
        got = await watchdog.find_stalled_jobs(now=datetime.now())
        assert [r["id"] for r in got] == ["s1"]
        assert got[0]["limit_s"] == 1800 + 180 * 4

    @pytest.mark.asyncio
    async def test_fresh_job_not_reported(self, test_db):
        await _insert_job(test_db, "f1", status="ocr_running",
                          last_activity_at=_fresh())
        assert await watchdog.find_stalled_jobs(now=datetime.now()) == []

    @pytest.mark.asyncio
    async def test_null_activity_is_never_stalled(self, test_db):
        """不可判定 → 跳过。绝不用 created_at 兜底（会误杀长文档）。"""
        await _insert_job(test_db, "n1", status="ocr_running",
                          last_activity_at=None)
        assert await watchdog.find_stalled_jobs(now=datetime.now()) == []

    @pytest.mark.asyncio
    async def test_unparseable_activity_is_never_stalled(self, test_db):
        await _insert_job(test_db, "u1", status="ocr_running",
                          last_activity_at="garbage")
        assert await watchdog.find_stalled_jobs(now=datetime.now()) == []

    @pytest.mark.asyncio
    async def test_pending_excluded_even_when_very_stale(self, test_db):
        await _insert_job(test_db, "p1", status="pending", total_pages=1,
                          last_activity_at=_stale(999_999))
        assert await watchdog.find_stalled_jobs(now=datetime.now()) == []

    @pytest.mark.asyncio
    async def test_terminal_statuses_not_scanned(self, test_db):
        for st in ("review", "error", "cancelled", "archived", "partial_review"):
            await _insert_job(test_db, f"t-{st}", status=st,
                              last_activity_at=_stale(999_999))
        assert await watchdog.find_stalled_jobs(now=datetime.now()) == []


class TestRecoverStalledJobs:
    @pytest.mark.asyncio
    async def test_marks_error_audits_and_notifies(self, test_db, monkeypatch):
        notified = []
        monkeypatch.setattr("core.notify.notify_job",
                            lambda jid, st: notified.append((jid, st)))
        await _insert_job(test_db, "r1", status="ocr_running", total_pages=4,
                          last_activity_at=_stale(10_000))

        assert await watchdog.recover_stalled_jobs(now=datetime.now()) == 1

        cur = await test_db.execute(
            "SELECT status, error_message, finished_at FROM jobs WHERE id = 'r1'")
        row = await cur.fetchone()
        assert row["status"] == "error"
        assert "看门狗" in row["error_message"]
        assert row["finished_at"] is not None

        cur = await test_db.execute(
            "SELECT action, detail FROM audit_log WHERE job_id = 'r1'")
        audits = await cur.fetchall()
        actions = [a["action"] for a in audits]
        assert watchdog.AUDIT_ACTION in actions
        detail = [a["detail"] for a in audits if a["action"] == watchdog.AUDIT_ACTION][0]
        assert "ocr_running" in detail and "limit" in detail

        assert notified == [("r1", "error")]

    @pytest.mark.asyncio
    async def test_no_stalled_is_noop(self, test_db, monkeypatch):
        notified = []
        monkeypatch.setattr("core.notify.notify_job",
                            lambda jid, st: notified.append(jid))
        await _insert_job(test_db, "ok", status="analyzing",
                          last_activity_at=_fresh())
        assert await watchdog.recover_stalled_jobs(now=datetime.now()) == 0
        assert notified == []

    @pytest.mark.asyncio
    async def test_concurrent_status_change_is_skipped(self, test_db, monkeypatch):
        """快照与 UPDATE 之间状态被改（用户 retry）→ 条件更新影响 0 行 → 不误收敛、不误通知。

        用"快照状态与库内不一致"来构造这个竞态：把 DB 里的 job 改成 review，
        却让 find 返回 ocr_running 的快照。
        """
        notified = []
        monkeypatch.setattr("core.notify.notify_job",
                            lambda jid, st: notified.append(jid))
        await _insert_job(test_db, "race", status="review",
                          last_activity_at=_stale(10_000))

        async def _fake_find(*, now=None):
            return [{"id": "race", "status": "ocr_running", "filename": "x.pdf",
                     "total_pages": 4, "last_activity_at": _stale(10_000),
                     "elapsed_s": 10_000.0, "limit_s": 2280.0}]

        monkeypatch.setattr(watchdog, "find_stalled_jobs", _fake_find)
        assert await watchdog.recover_stalled_jobs() == 0
        assert notified == []

        cur = await test_db.execute("SELECT status FROM jobs WHERE id = 'race'")
        assert (await cur.fetchone())["status"] == "review"

    @pytest.mark.asyncio
    async def test_notify_failure_does_not_break_recovery(self, test_db, monkeypatch):
        def _boom(jid, st):
            raise RuntimeError("feishu down")

        monkeypatch.setattr("core.notify.notify_job", _boom)
        await _insert_job(test_db, "n2", status="analyzing", total_pages=1,
                          last_activity_at=_stale(999_999))
        assert await watchdog.recover_stalled_jobs(now=datetime.now()) == 1
        cur = await test_db.execute("SELECT status FROM jobs WHERE id = 'n2'")
        assert (await cur.fetchone())["status"] == "error"


class TestOrphanTaskTermination:
    """第二轮回审：收敛必须**同时**终止孤儿 pipeline task，只改状态不够。

    死结是这样形成的：`run_pipeline` 用 per-job 锁串行同一 job，而 retry
    端点把 `error → pending` 后 `launch_pipeline`。孤儿仍持锁 → retry 永远
    停在 `async with lock`（无上限）→ 而 `pending` 默认不被监视 → 用户照
    看门狗的提示点"重试"，得到的是一个永久无声的 pending。
    """

    @pytest.mark.asyncio
    async def test_orphan_task_is_cancelled_and_audited(self, test_db, monkeypatch):
        import asyncio

        from core.pipeline import locks

        monkeypatch.setattr("core.notify.notify_job", lambda jid, st: None)
        orphan = asyncio.create_task(asyncio.Event().wait())
        locks._pipeline_tasks["orphan"] = orphan
        try:
            await _insert_job(test_db, "orphan", status="ocr_running",
                              total_pages=4, last_activity_at=_stale(10_000))

            assert await watchdog.recover_stalled_jobs(now=datetime.now()) == 1

            assert orphan.cancelled() or orphan.done(), "孤儿 task 必须被终止"
            cur = await test_db.execute(
                "SELECT detail FROM audit_log WHERE job_id = 'orphan' "
                "AND action = ?", (watchdog.AUDIT_ACTION,))
            detail = (await cur.fetchone())["detail"]
            assert "orphan_task=cancelled" in detail, (
                f"审计必须留下终止结论（GMP 追溯 + 排障），实得: {detail}")
        finally:
            orphan.cancel()
            await asyncio.gather(orphan, return_exceptions=True)
            locks._pipeline_tasks.pop("orphan", None)

    @pytest.mark.asyncio
    async def test_terminating_orphan_releases_per_job_lock(self, test_db, monkeypatch):
        """**这条是死结的正面证明**：孤儿持锁 → 收敛后锁必须已释放。

        用一个真的按住 per-job 锁且永不退出的 task 冒充孤儿（与
        `run_pipeline` 里 `async with lock` 的结构一致）。
        """
        import asyncio

        from core.pipeline import locks

        monkeypatch.setattr("core.notify.notify_job", lambda jid, st: None)
        holding = asyncio.Event()

        async def _hold_lock_then_hang(job_id: str):
            entry = locks._pipeline_locks.setdefault(
                job_id, {"lock": asyncio.Lock(), "refs": 0})
            entry["refs"] += 1
            try:
                async with entry["lock"]:
                    holding.set()
                    await asyncio.Event().wait()      # 永不主动释放
            finally:
                entry["refs"] -= 1

        task = asyncio.create_task(_hold_lock_then_hang("held"))
        locks._pipeline_tasks["held"] = task
        try:
            await asyncio.wait_for(holding.wait(), timeout=5)
            lock = locks._pipeline_locks["held"]["lock"]
            assert lock.locked(), "前置条件：孤儿确实持有 per-job 锁"

            await _insert_job(test_db, "held", status="ocr_running",
                              total_pages=4, last_activity_at=_stale(10_000))
            assert await watchdog.recover_stalled_jobs(now=datetime.now()) == 1

            # 锁已释放 → retry（launch_pipeline → run_pipeline）能拿到锁继续跑
            assert not lock.locked(), "收敛后 per-job 锁必须释放，否则 retry 永久阻塞"
            await asyncio.wait_for(lock.acquire(), timeout=5)
            lock.release()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            locks._pipeline_tasks.pop("held", None)
            locks._pipeline_locks.pop("held", None)

    @pytest.mark.asyncio
    async def test_no_registered_task_is_reported_not_crashed(self):
        assert await watchdog._terminate_pipeline_task("never-registered") == "no-task"


class TestPendingTakeover:
    """pending 的唯一例外：已被 pipeline 接管（注册表里有未完成 task）。

    合法排队永远没有注册 task（`launch_pipeline` 才登记）→ 不会误杀排队。
    """

    @pytest.mark.asyncio
    async def test_stale_pending_without_task_is_ignored(self, test_db):
        """纯排队（无注册 task）→ 永不判定，哪怕心跳已烂了几十年。"""
        await _insert_job(test_db, "queue", status="pending", total_pages=1,
                          last_activity_at=_stale(999_999))
        assert await watchdog.find_stalled_jobs(now=datetime.now()) == []

    @pytest.mark.asyncio
    async def test_stale_pending_with_live_task_is_recovered(self, test_db, monkeypatch):
        import asyncio

        from core.pipeline import locks

        monkeypatch.setattr("core.notify.notify_job", lambda jid, st: None)
        task = asyncio.create_task(asyncio.Event().wait())
        locks._pipeline_tasks["taken"] = task
        try:
            await _insert_job(test_db, "taken", status="pending", total_pages=1,
                              last_activity_at=_stale(999_999))

            got = await watchdog.find_stalled_jobs(now=datetime.now())
            assert [r["id"] for r in got] == ["taken"]
            assert got[0]["has_live_task"] is True
            assert got[0]["limit_s"] == watchdog._PENDING_TAKEOVER_STALL_S

            assert await watchdog.recover_stalled_jobs(now=datetime.now()) == 1
            cur = await test_db.execute("SELECT status FROM jobs WHERE id = 'taken'")
            assert (await cur.fetchone())["status"] == "error"
            assert task.done(), "被接管的 pending 也必须终止孤儿，否则锁不释放"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            locks._pipeline_tasks.pop("taken", None)

    @pytest.mark.asyncio
    async def test_fresh_pending_with_live_task_is_ignored(self, test_db):
        """刚被接管的 pending（心跳新鲜）→ 不判 —— 正常只在毫秒级停留。"""
        import asyncio

        from core.pipeline import locks

        task = asyncio.create_task(asyncio.Event().wait())
        locks._pipeline_tasks["fresh"] = task
        try:
            await _insert_job(test_db, "fresh", status="pending", total_pages=1,
                              last_activity_at=_fresh())
            assert await watchdog.find_stalled_jobs(now=datetime.now()) == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            locks._pipeline_tasks.pop("fresh", None)


class TestWatchdogLoop:
    @pytest.mark.asyncio
    async def test_loop_scans_then_stops_on_event(self, test_db, monkeypatch):
        import asyncio

        calls = []

        async def _count(*, now=None):
            calls.append(1)
            return 0

        monkeypatch.setattr(watchdog, "recover_stalled_jobs", _count)
        monkeypatch.setattr(watchdog, "scan_interval_seconds", lambda: 0.05)

        stop = asyncio.Event()
        task = asyncio.create_task(watchdog.watchdog_loop(stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        assert len(calls) >= 2  # 至少扫过两轮 → 循环真的在跑
        assert task.done() and not task.cancelled()  # 由 stop event 正常退出

    @pytest.mark.asyncio
    async def test_loop_survives_scan_exception(self, monkeypatch):
        """看门狗自己挂掉比 job 卡死更糟（用户以为有兜底）→ 必须继续下一轮。"""
        import asyncio

        calls = []

        async def _boom(*, now=None):
            calls.append(1)
            raise RuntimeError("scan blew up")

        monkeypatch.setattr(watchdog, "recover_stalled_jobs", _boom)
        monkeypatch.setattr(watchdog, "scan_interval_seconds", lambda: 0.05)

        task = asyncio.create_task(watchdog.watchdog_loop())
        await asyncio.sleep(0.3)
        assert len(calls) >= 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_loop_is_cancellable(self, monkeypatch):
        import asyncio

        monkeypatch.setattr(watchdog, "scan_interval_seconds", lambda: 5.0)
        task = asyncio.create_task(watchdog.watchdog_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestConfigSwitches:
    def test_enabled_default_true_and_off_values(self, monkeypatch):
        monkeypatch.delenv("PBC_WATCHDOG_ENABLED", raising=False)
        assert watchdog.enabled() is True
        for off in ("0", "false", "FALSE", "no", "off"):
            monkeypatch.setenv("PBC_WATCHDOG_ENABLED", off)
            assert watchdog.enabled() is False

    def test_scan_interval_floor_and_garbage(self, monkeypatch):
        monkeypatch.setenv("PBC_WATCHDOG_INTERVAL_S", "0")
        assert watchdog.scan_interval_seconds() == 5.0
        monkeypatch.setenv("PBC_WATCHDOG_INTERVAL_S", "abc")
        assert watchdog.scan_interval_seconds() == 60.0
        monkeypatch.setenv("PBC_WATCHDOG_INTERVAL_S", "30")
        assert watchdog.scan_interval_seconds() == 30.0


class TestStatusSnapshot:
    """看门狗自述（`/api/health/watchdog`）。

    为什么必须可见：看门狗**自己挂掉**比 job 卡死更糟 —— 用户会以为
    "有兜底"而不再手动重试。`last_scan_at` 停滞即说明巡检已死。
    """

    def test_snapshot_exposes_calibration_and_liveness(self, monkeypatch):
        from core.ocr_client import POLL_TIMEOUT_MAX

        monkeypatch.delenv("PBC_WATCHDOG_ENABLED", raising=False)
        monkeypatch.delenv("PBC_WATCHDOG_SCALE", raising=False)
        snap = watchdog.status_snapshot()

        assert snap["enabled"] is True
        assert snap["interval_s"] == 60.0
        assert snap["scale"] == 1.0
        assert snap["ocr_upstream_cap_s"] == float(POLL_TIMEOUT_MAX)
        assert "pending" not in snap["watch_statuses"]
        assert snap["pending_watch_requires_live_task"] is True
        assert snap["stall_limits_s"]["ocr_running"] == watchdog._BASE_STALL_S["ocr_running"]
        for k in ("running", "last_scan_at", "last_scan_error", "total_scans",
                  "total_recovered"):
            assert k in snap

    def test_snapshot_leaks_no_secret(self, monkeypatch):
        """健康端点无鉴权 → 绝不能带出密钥/连接串。"""
        monkeypatch.setenv("PADDLE_OCR_TOKEN", "sk-should-never-appear")
        blob = repr(watchdog.status_snapshot()).lower()
        for bad in ("token", "secret", "password", "sk-"):
            assert bad not in blob

    @pytest.mark.asyncio
    async def test_loop_updates_liveness(self, test_db, monkeypatch):
        import asyncio

        async def _count(*, now=None):
            return 0

        monkeypatch.setattr(watchdog, "recover_stalled_jobs", _count)
        monkeypatch.setattr(watchdog, "scan_interval_seconds", lambda: 0.05)
        # 清掉上一轮的残留，让断言只反映本轮
        watchdog._STATE.update({"total_scans": 0, "last_scan_at": None,
                                "last_scan_error": None})

        stop = asyncio.Event()
        task = asyncio.create_task(watchdog.watchdog_loop(stop))
        await asyncio.sleep(0.3)
        assert watchdog._STATE["running"] is True
        assert watchdog._STATE["total_scans"] >= 2
        assert watchdog._STATE["last_scan_at"] is not None

        stop.set()
        await asyncio.wait_for(task, timeout=5)
        assert not task.cancelled()
        assert watchdog._STATE["running"] is False

    @pytest.mark.asyncio
    async def test_scan_error_is_recorded_not_swallowed(self, monkeypatch):
        """单轮失败要留痕：否则"看门狗在跑但一直失败"无法与正常区分。"""
        import asyncio

        async def _boom(*, now=None):
            raise RuntimeError("db gone")

        monkeypatch.setattr(watchdog, "recover_stalled_jobs", _boom)
        monkeypatch.setattr(watchdog, "scan_interval_seconds", lambda: 0.05)
        watchdog._STATE["last_scan_error"] = None

        task = asyncio.create_task(watchdog.watchdog_loop())
        await asyncio.sleep(0.2)
        assert "db gone" in (watchdog._STATE["last_scan_error"] or "")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestHeartbeatWiring:
    """心跳必须写在"真实推进"处 —— 这是看门狗唯一信号源，断了就等于没有看门狗。"""

    @pytest.mark.asyncio
    async def test_touch_activity_updates_column(self, test_db):
        from core.pipeline.state import touch_activity

        await _insert_job(test_db, "h1", last_activity_at=_stale(5000))
        await touch_activity(test_db, "h1")
        await test_db.commit()
        cur = await test_db.execute(
            "SELECT last_activity_at FROM jobs WHERE id = 'h1'")
        assert watchdog.elapsed_seconds(
            (await cur.fetchone())["last_activity_at"]) < 60

    @pytest.mark.asyncio
    async def test_touch_activity_never_raises(self, test_db):
        """心跳失败绝不能打断管线。"""
        from core.pipeline.state import touch_activity

        await touch_activity(test_db, "does-not-exist")  # 不应抛

    @pytest.mark.asyncio
    async def test_transition_status_pushes_heartbeat(self, test_db):
        from core.pipeline.state import transition_status

        await _insert_job(test_db, "h2", status="pending",
                          last_activity_at=_stale(5000))
        await transition_status(test_db, "h2", "ocr_running", "test")
        cur = await test_db.execute(
            "SELECT last_activity_at FROM jobs WHERE id = 'h2'")
        assert watchdog.elapsed_seconds(
            (await cur.fetchone())["last_activity_at"]) < 60

    @pytest.mark.asyncio
    async def test_ocr_progress_update_pushes_heartbeat(self, test_db):
        from core.pipeline.state import _update_ocr_progress

        await _insert_job(test_db, "h3", last_activity_at=_stale(5000))
        await _update_ocr_progress("h3", 3, 51)
        cur = await test_db.execute(
            "SELECT ocr_progress, last_activity_at FROM jobs WHERE id = 'h3'")
        row = await cur.fetchone()
        assert row["ocr_progress"] == '{"done": 3, "total": 51}'
        assert watchdog.elapsed_seconds(row["last_activity_at"]) < 60

    @pytest.mark.asyncio
    async def test_self_heal_progress_update_pushes_heartbeat(self, test_db):
        from core.pipeline.state import _update_self_heal_progress

        await _insert_job(test_db, "h4", last_activity_at=_stale(5000))
        await _update_self_heal_progress("h4", 4, 4, 1, 3, [1, 2, 3])
        cur = await test_db.execute(
            "SELECT last_activity_at FROM jobs WHERE id = 'h4'")
        assert watchdog.elapsed_seconds(
            (await cur.fetchone())["last_activity_at"]) < 60

    @pytest.mark.asyncio
    async def test_cross_progress_update_pushes_heartbeat(self, test_db):
        from core.pipeline.state import _update_cross_progress

        await _insert_job(test_db, "h5", last_activity_at=_stale(5000))
        await _update_cross_progress("h5", 1, 4, "规则校验")
        cur = await test_db.execute(
            "SELECT last_activity_at FROM jobs WHERE id = 'h5'")
        assert watchdog.elapsed_seconds(
            (await cur.fetchone())["last_activity_at"]) < 60


class TestSchemaSingleSourceOfTruth:
    """schema.sql 是 jobs DDL 的唯一声明处；迁移体只负责"给已有表加列"。"""

    def test_schema_sql_declares_last_activity_at(self):
        from pathlib import Path

        sql = (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text(
            encoding="utf-8")
        assert "last_activity_at" in sql

    def test_every_migration_version_has_its_own_guard(self):
        """每个版本号都必须有**自己的** `if current_version < N:` 守卫。

        踩过的坑（v12 引入时）：把 `await _migrate_v12(db)` 直接贴在
        `await _migrate_v11(db)` 后面 —— 于是它落在 `if current_version < 11:`
        块内，**v11 库会整段跳过**，而末尾 `PRAGMA user_version = SCHEMA_VERSION`
        仍无条件执行 → 库被标成 v12 却缺列（静默 schema 漂移；表现是看门狗
        永久失明，且不会报任何错）。被 `test_migrate_v11_db_adds_column` 当场抓出。

        这条护栏把"每个版本既有守卫、且守卫指向同号迁移函数"变成机检 ——
        以后加 v13 时若漏写守卫，会在这里立刻失败。
        """
        import re
        from pathlib import Path

        from db.client import SCHEMA_VERSION

        src = (Path(__file__).resolve().parents[2] / "db" / "client.py").read_text(
            encoding="utf-8")

        # 守卫与其紧邻调用成对出现（顺序也必须同号，防复制粘贴错号）
        pairs = re.findall(
            r"if current_version < (\d+):\s*\n\s*await _migrate_v(\d+)\(db\)", src)
        guards = {int(g) for g, _ in pairs}
        for guard, called in pairs:
            assert guard == called, (
                f"`if current_version < {guard}:` 调用了 `_migrate_v{called}` —— 号不一致")

        expected = set(range(1, SCHEMA_VERSION + 1))
        assert expected <= guards, (
            f"缺少守卫/调用点的版本: {sorted(expected - guards)}；"
            f"每个版本必须写 `if current_version < N:` + `await _migrate_vN(db)` "
            f"（不得把新迁移贴在上一版 if 块里）")

    def test_schema_version_is_12(self):
        from db.client import SCHEMA_VERSION

        assert SCHEMA_VERSION == 12

    @pytest.mark.asyncio
    async def test_migrate_v11_db_adds_column(self, tmp_path):
        """v11 库（无 last_activity_at）→ migrate 必须补列，否则看门狗永久失明。"""
        from db.client import migrate, SCHEMA_VERSION

        db_path = str(tmp_path / "v11.db")
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("""
                CREATE TABLE jobs (
                    id TEXT PRIMARY KEY,
                    filename TEXT,
                    status TEXT,
                    total_pages INTEGER,
                    ocr_progress TEXT
                )
            """)
            await db.execute("PRAGMA user_version = 11")
            await db.commit()

            await migrate(db)
            cur = await db.execute("PRAGMA table_info(jobs)")
            cols = {row["name"] for row in await cur.fetchall()}
            assert "last_activity_at" in cols
            cur = await db.execute("PRAGMA user_version")
            assert (await cur.fetchone())[0] == SCHEMA_VERSION

    @pytest.mark.asyncio
    async def test_fresh_schema_has_column(self, test_db):
        cur = await test_db.execute("PRAGMA table_info(jobs)")
        cols = {row["name"] for row in await cur.fetchall()}
        assert "last_activity_at" in cols
