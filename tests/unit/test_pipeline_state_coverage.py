"""core/pipeline/state.py 边界分支补测。

补全 test_pipeline.py / test_pipeline_state_machine.py 未覆盖的：
- 被拒状态转换的审计写库失败（吞掉告警）
- recover_stuck_jobs 条件更新影响 0 行（并发推进竞态）/ 通知异常
- _is_cancelled 取消转换竞态 / 通知异常
- is_job_stopping_sync 只读探针（真值 / 缺行 / 连接失败）
- _update_ocr_progress / _update_self_heal_progress / _update_cross_progress
  的畸形 JSON 与写库失败分支
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

import core.pipeline.state as state_mod
from core.pipeline import InvalidTransitionError
from core.pipeline.state import (
    _is_cancelled,
    _transition_status_unlocked,
    _update_cross_progress,
    _update_ocr_progress,
    _update_self_heal_progress,
    is_job_stopping_sync,
    recover_stuck_jobs,
)


async def _insert(test_db, job_id: str, status: str, **cols):
    fields = ["id", "filename", "status", "pdf_path"]
    values = [job_id, f"{job_id}.pdf", status, f"/tmp/{job_id}.pdf"]
    for k, v in cols.items():
        fields.append(k)
        values.append(v)
    placeholders = ",".join("?" * len(fields))
    await test_db.execute(
        f"INSERT INTO jobs ({','.join(fields)}) VALUES ({placeholders})", values
    )
    await test_db.commit()


class _FailingDb:
    """execute/commit 均抛异常 — 模拟写库不可用。"""

    async def execute(self, *a, **k):
        raise RuntimeError("db down")

    async def commit(self):
        raise RuntimeError("db down")


class _ZeroRowcountDb:
    """SELECT 正常返回，UPDATE 影响 0 行 — 模拟快照与写入之间状态被并发改写。

    UPDATE 必须**不落到真实连接**：并发改写意味着发起方本该影响 0 行、
    底层行保持原状。若照常执行再伪造 rowcount=0，行会被真实改写成 error，
    测试就假阳性了。这里直接短路 UPDATE，仅返回 rowcount=0 的游标。
    """

    def __init__(self, real):
        self._real = real

    async def execute(self, sql, params=()):
        if sql.strip().upper().startswith("UPDATE"):
            return type("C", (), {"rowcount": 0})()
        return await self._real.execute(sql, params)

    async def commit(self):
        await self._real.commit()


class TestBlockedTransitionAuditFailure:
    @pytest.mark.asyncio
    async def test_audit_write_failure_is_swallowed(self, test_db):
        """非法转换写审计失败 → 仍抛 InvalidTransitionError（审计是附属动作）。"""
        await _insert(test_db, "blocked-1", "review")
        await test_db.execute("DROP TABLE audit_log")
        await test_db.commit()
        with pytest.raises(InvalidTransitionError):
            await _transition_status_unlocked(test_db, "blocked-1", "ocr_running")


class TestRecoverStuckJobsBranches:
    @pytest.mark.asyncio
    async def test_conditional_update_zero_rows_skips(self, test_db, monkeypatch):
        await _insert(test_db, "race-1", "ocr_running")
        monkeypatch.setattr(state_mod, "get_db", lambda: _async(_ZeroRowcountDb(test_db)))
        count = await recover_stuck_jobs()
        # 仍计入 stuck 数量（SELECT 命中），但 UPDATE 影响 0 行 → 跳过通知
        assert count == 1
        cur = await test_db.execute("SELECT status FROM jobs WHERE id = 'race-1'")
        assert (await cur.fetchone())["status"] == "ocr_running"

    @pytest.mark.asyncio
    async def test_notify_exception_is_swallowed(self, test_db, monkeypatch):
        await _insert(test_db, "stuck-n", "analyzing")

        async def boom(job_id, status):
            raise RuntimeError("notify down")

        monkeypatch.setattr("core.notify.notify_job", boom)
        assert await recover_stuck_jobs() == 1


class TestIsCancelledBranches:
    @pytest.mark.asyncio
    async def test_cancel_transition_race_is_tolerated(self, test_db, monkeypatch):
        """cancelling → cancelled 转换抛 InvalidTransitionError → 仍视为已取消。"""
        await _insert(test_db, "cancel-race", "cancelling")

        async def boom(db, job_id, new_status, detail=""):
            raise InvalidTransitionError("race")

        monkeypatch.setattr(state_mod, "_transition_status_unlocked", boom)
        assert await _is_cancelled("cancel-race") is True

    @pytest.mark.asyncio
    async def test_cancel_notify_exception_swallowed(self, test_db, monkeypatch):
        await _insert(test_db, "cancel-n", "cancelling")

        async def boom(job_id, status):
            raise RuntimeError("notify down")

        monkeypatch.setattr("core.notify.notify_job", boom)
        assert await _is_cancelled("cancel-n") is True


class TestIsJobStoppingSync:
    @pytest.mark.asyncio
    async def test_true_for_cancelling(self, test_db):
        await _insert(test_db, "stop-1", "cancelling")
        assert is_job_stopping_sync("stop-1") is True

    @pytest.mark.asyncio
    async def test_false_for_active(self, test_db):
        await _insert(test_db, "stop-2", "analyzing")
        assert is_job_stopping_sync("stop-2") is False

    @pytest.mark.asyncio
    async def test_false_for_missing_job(self, test_db):
        # 未插入的 job → row is None → False（不抛异常）
        assert is_job_stopping_sync("never-existed") is False

    def test_false_on_connection_failure(self, monkeypatch, tmp_path):
        """数据库路径不可用 → 探针失败静默返回 False（不阻断 OCR）。"""
        from config import config as _cfg
        orig = _cfg["app"].database_path
        _cfg["app"].database_path = str(tmp_path / "nope" / "x.db")
        try:
            assert is_job_stopping_sync("any") is False
        finally:
            _cfg["app"].database_path = orig


class TestProgressUpdateBranches:
    @pytest.mark.asyncio
    async def test_ocr_progress_failure_swallowed(self, monkeypatch):
        monkeypatch.setattr(state_mod, "get_db", lambda: _async(_FailingDb()))
        await _update_ocr_progress("j", 1, 2)  # 不抛异常

    @pytest.mark.asyncio
    async def test_self_heal_progress_failure_swallowed(self, monkeypatch):
        monkeypatch.setattr(state_mod, "get_db", lambda: _async(_FailingDb()))
        await _update_self_heal_progress("j", 1, 2, 1, 3, [1])  # 不抛异常

    @pytest.mark.asyncio
    async def test_self_heal_total_zero_clears_subkey(self, test_db):
        await _insert(test_db, "sh-1", "ocr_running", ocr_progress='{"done":5,"total":5,"self_heal":{"done":1,"total":2}}')
        await _update_self_heal_progress("sh-1", 5, 5, 0, 0, [])
        cur = await test_db.execute("SELECT ocr_progress FROM jobs WHERE id = 'sh-1'")
        data = json.loads((await cur.fetchone())["ocr_progress"])
        assert "self_heal" not in data
        assert data == {"done": 5, "total": 5}

    @pytest.mark.asyncio
    async def test_cross_progress_non_dict_recovers(self, test_db):
        await _insert(test_db, "cr-1", "analyzing", ocr_progress="[1, 2, 3]")
        await _update_cross_progress("cr-1", 1, 3, "规则校验")
        cur = await test_db.execute("SELECT ocr_progress FROM jobs WHERE id = 'cr-1'")
        data = json.loads((await cur.fetchone())["ocr_progress"])
        assert data["cross"]["label"] == "规则校验"

    @pytest.mark.asyncio
    async def test_cross_progress_invalid_json_recovers(self, test_db):
        await _insert(test_db, "cr-2", "analyzing", ocr_progress="{bad json")
        await _update_cross_progress("cr-2", 2, 3, "LLM 语义分析")
        cur = await test_db.execute("SELECT ocr_progress FROM jobs WHERE id = 'cr-2'")
        data = json.loads((await cur.fetchone())["ocr_progress"])
        assert data["cross"]["done"] == 2

    @pytest.mark.asyncio
    async def test_cross_progress_total_zero_pops_key(self, test_db):
        await _insert(
            test_db, "cr-3", "analyzing",
            ocr_progress='{"cross":{"done":3,"total":3,"label":"完成"}}',
        )
        await _update_cross_progress("cr-3", 3, 0, "")
        cur = await test_db.execute("SELECT ocr_progress FROM jobs WHERE id = 'cr-3'")
        data = json.loads((await cur.fetchone())["ocr_progress"])
        assert "cross" not in data

    @pytest.mark.asyncio
    async def test_cross_progress_failure_swallowed(self, monkeypatch):
        monkeypatch.setattr(state_mod, "get_db", lambda: _async(_FailingDb()))
        await _update_cross_progress("j", 1, 3, "x")  # 不抛异常


# ── 辅助：把实例包装成 awaitable（state.py 内部用 `await get_db()`）──


async def _async(obj):
    return obj
