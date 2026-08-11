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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
            error_message TEXT
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

    def test_error_can_transition_to_pending_for_retry(self):
        assert "pending" in VALID_TRANSITIONS["error"]

    def test_archived_can_transition_to_review_for_unarchive(self):
        assert "review" in VALID_TRANSITIONS["archived"]

    def test_all_terminal_states_have_empty_or_limited_transitions(self):
        """终态（cancelled）应有受限的转换。"""
        assert "pending" in VALID_TRANSITIONS["cancelled"]  # 允许重试


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

        assert "pending" in str(exc_info.value)
        assert "review" in str(exc_info.value)

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
