"""Pipeline 状态机单元测试。

覆盖：
- VALID_TRANSITIONS 完整性
- transition_status 合法转换
- transition_status 非法转换（应抛 InvalidTransitionError）
- audit_log 记录
"""
import pytest
import pytest_asyncio
import sqlite3

from core.pipeline import (
    VALID_TRANSITIONS,
    InvalidTransitionError,
    transition_status,
)


@pytest_asyncio.fixture
async def test_db_for_state_machine(tmp_path):
    """提供带 schema 的测试数据库。"""
    db_path = tmp_path / "state_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # 创建最小 schema（jobs + audit_log）
    conn.executescript("""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            filename TEXT,
            pdf_path TEXT,
            status TEXT,
            total_pages INTEGER,
            created_at TEXT,
            finished_at TEXT,
            stage1_ms INTEGER,
            stage2_ms INTEGER,
            stage3_ms INTEGER,
            failed_pages TEXT,
            error_message TEXT,
            last_activity_at TIMESTAMP  -- v12 心跳：transition_status 会写它
        );
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            action TEXT,
            detail TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 插入测试 job
    conn.execute(
        "INSERT INTO jobs (id, filename, status) VALUES (?, ?, ?)",
        ("test-job", "test.pdf", "pending"),
    )
    conn.commit()
    conn.close()

    # 用 aiosqlite 包装
    import aiosqlite
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    yield db
    await db.close()


class TestValidTransitions:
    """VALID_TRANSITIONS 状态转换表完整性。"""

    def test_pending_can_transition_to_ocr_running(self):
        assert "ocr_running" in VALID_TRANSITIONS["pending"]

    def test_pending_can_transition_to_error(self):
        assert "error" in VALID_TRANSITIONS["pending"]

    def test_pending_can_transition_to_cancelling(self):
        assert "cancelling" in VALID_TRANSITIONS["pending"]

    def test_ocr_running_can_transition_to_ocr_done(self):
        assert "ocr_done" in VALID_TRANSITIONS["ocr_running"]

    def test_ocr_done_can_transition_to_analyzing(self):
        assert "analyzing" in VALID_TRANSITIONS["ocr_done"]

    def test_analyzing_can_transition_to_review(self):
        assert "review" in VALID_TRANSITIONS["analyzing"]

    def test_analyzing_can_transition_to_partial_review(self):
        assert "partial_review" in VALID_TRANSITIONS["analyzing"]

    def test_cancelling_can_transition_to_cancelled_or_error(self):
        """cancelling 应能转向 cancelled（正常取消）或 error（LLM 卡死时自救）。"""
        assert VALID_TRANSITIONS["cancelling"] == {"cancelled", "error"}

    def test_review_can_transition_to_archived(self):
        assert "archived" in VALID_TRANSITIONS["review"]

    def test_review_can_transition_to_pending_for_reanalysis(self):
        """P1-6: review → pending 合法 — 全量重新分析（retry 语义）。"""
        assert "pending" in VALID_TRANSITIONS["review"]

    def test_error_can_transition_to_pending_for_retry(self):
        assert "pending" in VALID_TRANSITIONS["error"]

    def test_archived_can_transition_to_review_for_unarchive(self):
        assert "review" in VALID_TRANSITIONS["archived"]

    def test_all_terminal_states_have_empty_or_limited_transitions(self):
        """终态（cancelled）应有受限的转换。"""
        assert "pending" in VALID_TRANSITIONS["cancelled"]  # 允许重试


class TestNoJobCanGetStuckForever:
    """每个**非终态**都必须能走到 `error`（否则任务会永久卡死）。

    这是 `recover_stuck_jobs` 崩溃恢复路径的**合法性前提**，也是对
    `core/pipeline/state.py` 里那段被修正过的注释的机检：该注释曾声称
    "ocr_done→error 不在 VALID_TRANSITIONS 中"，据此论证恢复路径"必须"
    绕过状态机 —— 但实测五个非终态**都**允许 → error。真正的绕过理由是
    "需要一并写 error_message/finished_at + 条件 UPDATE"，不是"转换非法"。
    若哪天有人真的移除了某个 → error 边，任务将无法被恢复，本护栏会立刻红。
    """

    def test_every_stuck_status_can_transition_to_error(self):
        from core.pipeline.state import _STUCK_STATUSES

        missing = [s for s in _STUCK_STATUSES if "error" not in VALID_TRANSITIONS.get(s, set())]
        assert not missing, (
            f"这些非终态无法迁移到 error ⇒ 崩溃恢复会失败、任务永久卡死：{missing}"
        )

    def test_stuck_statuses_are_all_real_states(self):
        """防空转：_STUCK_STATUSES 必须都出现在 VALID_TRANSITIONS 里。"""
        from core.pipeline.state import _STUCK_STATUSES

        assert _STUCK_STATUSES, "_STUCK_STATUSES 为空 —— 提取器/常量失效"
        unknown = [s for s in _STUCK_STATUSES if s not in VALID_TRANSITIONS]
        assert not unknown, f"_STUCK_STATUSES 含未定义状态：{unknown}"

    def test_stuck_statuses_are_non_terminal(self):
        """被恢复的必须是**非终态** —— 终态不该被启动恢复误标 error。"""
        from core.pipeline.state import _STUCK_STATUSES

        for terminal in ("review", "partial_review", "error", "cancelled", "archived"):
            assert terminal not in _STUCK_STATUSES, (
                f"终态 {terminal} 混进了 _STUCK_STATUSES —— 启动时会被误标 error"
            )


class TestStatusPartitionIsSingleTruth:
    """状态分区必须**单点定义**，且恰好划分状态机的键集。

    背景（本轮对抗性审查）：`api/jobs/__init__.py` 与 `core/pipeline/state.py`
    曾**各存一份逐元素相同**的元组（`("pending", "ocr_running", ...)`），靠人工
    同步。新增状态时必然漏改一处，而漏改的后果是**静默的**：
      - 并发额度少算 ⇒ 超额启动流水线（3×200MB PDF 的 OCR 结果可吃 ~2GB）；
      - 崩溃恢复少算 ⇒ 该状态的 job 永不收敛，成为"无终态黑洞"。
    现已收敛为单一真值，本护栏锁住三件事。
    """

    def test_api_jobs_reexports_the_same_objects(self):
        """必须是**同一对象**（`is`），不是内容相同的副本。"""
        from api.jobs import _ACTIVE_STATUSES, _TERMINAL_STATUSES
        from core.pipeline.state import ACTIVE_STATUSES, TERMINAL_STATUSES

        assert _ACTIVE_STATUSES is ACTIVE_STATUSES, (
            "api.jobs._ACTIVE_STATUSES 不是 core.pipeline.state 里那个对象 —— "
            "又出现了第二份副本（单一真值被破坏）"
        )
        assert _TERMINAL_STATUSES is TERMINAL_STATUSES, (
            "api.jobs._TERMINAL_STATUSES 是副本而非同一对象"
        )

    def test_partition_covers_the_state_machine_exactly(self):
        """防空转 + 完整性：两集不相交，且并集**恰好等于** VALID_TRANSITIONS 键集。"""
        from core.pipeline.state import ACTIVE_STATUSES, TERMINAL_STATUSES

        states = set(VALID_TRANSITIONS)
        assert states, "VALID_TRANSITIONS 为空 —— 提取器失效"
        active, terminal = set(ACTIVE_STATUSES), set(TERMINAL_STATUSES)
        assert not (active & terminal), (
            f"状态同时被归为终态与非终态：{sorted(active & terminal)}"
        )
        assert active | terminal == states, (
            f"分区与状态机键集不一致 —— "
            f"漏: {sorted(states - (active | terminal))}，"
            f"多: {sorted((active | terminal) - states)}"
        )

    def test_stuck_alias_stays_in_sync_with_the_active_set(self):
        """`_STUCK_STATUSES` 必须与 `ACTIVE_STATUSES` **内容一致**。

        ⚠️ 这里刻意**不**用 `is` 断言"是同一对象"：CPython 会对同一 code object
        内**相等的常量元组做去重**（`_STUCK_STATUSES = (...)` 与
        `ACTIVE_STATUSES = (...)` 写在同一模块里会拿到同一个对象），
        于是 `is` 断言无论是否写成语义别名都通过 —— 那是一条**无法失败**的
        断言（实测变异 M4 抓不到）。真正要防的是**漂移**：有人往
        `ACTIVE_STATUSES` 里加了状态却忘了 `_STUCK_STATUSES`（或反之），
        该状态的 job 崩溃后永不收敛。内容相等才是那条不变量。
        """
        from core.pipeline.state import ACTIVE_STATUSES, _STUCK_STATUSES

        assert _STUCK_STATUSES, "_STUCK_STATUSES 为空 —— 提取器/常量失效"
        assert set(_STUCK_STATUSES) == set(ACTIVE_STATUSES), (
            f"_STUCK_STATUSES 与 ACTIVE_STATUSES 漂移 —— "
            f"仅前者有: {sorted(set(_STUCK_STATUSES) - set(ACTIVE_STATUSES))}，"
            f"仅后者有: {sorted(set(ACTIVE_STATUSES) - set(_STUCK_STATUSES))}"
        )


class TestRecoveryBypassIsAudited:
    """崩溃恢复绕过状态机校验，但**必须**留下审计（GMP 可追溯）。"""

    def test_recover_writes_stuck_recovery_audit(self):
        import inspect

        from core.pipeline import state as state_mod

        src = inspect.getsource(state_mod.recover_stuck_jobs)
        assert "stuck_recovery" in src, (
            "recover_stuck_jobs 绕过状态机却不写 stuck_recovery 审计 —— "
            "GMP 追溯要求记录该变更"
        )
        assert "status IN (" in src, (
            "恢复 UPDATE 缺少 `status IN (...)` 条件 —— 与并发 retry 竞态时"
            "会把新状态打回 error"
        )


class TestTransitionStatus:
    """transition_status 函数行为。"""

    @pytest.mark.asyncio
    async def test_valid_transition_updates_status(self, test_db_for_state_machine):
        """合法转换应更新 status 并写 audit_log。"""
        db = test_db_for_state_machine
        result = await transition_status(db, "test-job", "ocr_running", "Stage 1 start")
        assert result == "ocr_running"

        # 验证数据库已更新
        cursor = await db.execute("SELECT status FROM jobs WHERE id = ?", ("test-job",))
        row = await cursor.fetchone()
        assert row["status"] == "ocr_running"

        # 验证 audit_log 已记录
        cursor = await db.execute(
            "SELECT action, detail FROM audit_log WHERE job_id = ? ORDER BY id DESC LIMIT 1",
            ("test-job",),
        )
        log = await cursor.fetchone()
        assert log["action"] == "status_transition"
        assert "pending" in log["detail"]
        assert "ocr_running" in log["detail"]

    @pytest.mark.asyncio
    async def test_invalid_transition_raises_error(self, test_db_for_state_machine):
        """非法转换应抛 InvalidTransitionError，不更新状态。"""
        db = test_db_for_state_machine
        # test-job 当前是 pending，pending -> review 是非法的
        with pytest.raises(InvalidTransitionError) as exc_info:
            await transition_status(db, "test-job", "review")

        # 中文化后消息携带中文状态名（用户可见错误提示）
        assert "待处理" in str(exc_info.value)
        assert "待复核" in str(exc_info.value)

        # 验证状态未变
        cursor = await db.execute("SELECT status FROM jobs WHERE id = ?", ("test-job",))
        row = await cursor.fetchone()
        assert row["status"] == "pending"  # 未变

    @pytest.mark.asyncio
    async def test_transition_nonexistent_job_raises(self, test_db_for_state_machine):
        """不存在的 job 应抛 InvalidTransitionError。"""
        db = test_db_for_state_machine
        with pytest.raises(InvalidTransitionError):
            await transition_status(db, "nonexistent-job", "ocr_running")

    @pytest.mark.asyncio
    async def test_transition_to_same_state_raises(self, test_db_for_state_machine):
        """转换到当前相同状态应抛错（pending 不在 pending 的 allowed 集合中）。"""
        db = test_db_for_state_machine
        with pytest.raises(InvalidTransitionError):
            await transition_status(db, "test-job", "pending")

    @pytest.mark.asyncio
    async def test_chained_transitions(self, test_db_for_state_machine):
        """链式转换：pending -> ocr_running -> ocr_done -> analyzing -> review。"""
        db = test_db_for_state_machine
        await transition_status(db, "test-job", "ocr_running", "Stage 1")
        await transition_status(db, "test-job", "ocr_done", "Stage 1 done")
        await transition_status(db, "test-job", "analyzing", "Stage 2")
        await transition_status(db, "test-job", "review", "Pipeline complete")

        cursor = await db.execute("SELECT status FROM jobs WHERE id = ?", ("test-job",))
        row = await cursor.fetchone()
        assert row["status"] == "review"

        # 验证 4 条 audit_log
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM audit_log WHERE job_id = ?", ("test-job",)
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 4

    @pytest.mark.asyncio
    async def test_archive_from_review(self, test_db_for_state_machine):
        """从 review 归档应成功。"""
        db = test_db_for_state_machine
        await transition_status(db, "test-job", "ocr_running")
        await transition_status(db, "test-job", "ocr_done")
        await transition_status(db, "test-job", "analyzing")
        await transition_status(db, "test-job", "review")
        await transition_status(db, "test-job", "archived", "User archived")

        cursor = await db.execute("SELECT status FROM jobs WHERE id = ?", ("test-job",))
        row = await cursor.fetchone()
        assert row["status"] == "archived"

    @pytest.mark.asyncio
    async def test_unarchive_from_archived(self, test_db_for_state_machine):
        """从 archived 取消归档应成功。"""
        db = test_db_for_state_machine
        # 先到 review 再到 archived
        await transition_status(db, "test-job", "ocr_running")
        await transition_status(db, "test-job", "ocr_done")
        await transition_status(db, "test-job", "analyzing")
        await transition_status(db, "test-job", "review")
        await transition_status(db, "test-job", "archived")
        # 取消归档
        await transition_status(db, "test-job", "review", "User unarchived")

        cursor = await db.execute("SELECT status FROM jobs WHERE id = ?", ("test-job",))
        row = await cursor.fetchone()
        assert row["status"] == "review"

    @pytest.mark.asyncio
    async def test_cannot_archive_from_ocr_running(self, test_db_for_state_machine):
        """处理中状态不能归档（修复归档按钮 bug）。"""
        db = test_db_for_state_machine
        await transition_status(db, "test-job", "ocr_running")
        with pytest.raises(InvalidTransitionError):
            await transition_status(db, "test-job", "archived")
