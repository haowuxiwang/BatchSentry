"""SSE 终态快照缓存补测（M1b → M6/T6.2 收敛后）。

S2 把终态快照缓存从 listings.py 收敛到 status.py::_get_job_progress
（单一来源，活跃/终态两条 SSE 路径共用）。本文件锁定：
- 终态 job 首轮写入缓存、次轮命中复用（不再重算）
- 活跃 job 不写缓存（快照每轮变化）
- (status, finished_at) 变化即失效（retry 复用同一 job 行）
- 缓存容量超上限时整体清空重建
- 命中返回浅拷贝（调用方改坏缓存节点不影响后续读取）
"""
from __future__ import annotations

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def clean_cache():
    from api.jobs import _reset_terminal_snap_cache
    import api.jobs.status as status_mod

    _reset_terminal_snap_cache()
    yield status_mod
    _reset_terminal_snap_cache()


class TestTerminalSnapshotCache:
    @pytest.mark.asyncio
    async def test_terminal_snapshot_cached_then_reused(self, test_db, clean_cache):
        """首轮计算并写入缓存；次轮命中（不再产生 DB 查询）。"""
        from api.jobs import _get_job_progress

        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, finished_at) "
            "VALUES (?, ?, ?, ?, datetime('now','localtime'))",
            ("term-1", "b.pdf", "/tmp/b.pdf", "review"),
        )
        await test_db.commit()

        first = await _get_job_progress(test_db, "term-1")
        assert first["status"] == "review"
        assert "term-1" in clean_cache._TERMINAL_SNAP_CACHE

        # 命中路径：把缓存值改成哨兵，若真的复用则次轮读到哨兵
        key, snap = clean_cache._TERMINAL_SNAP_CACHE["term-1"]
        clean_cache._TERMINAL_SNAP_CACHE["term-1"] = (key, {"id": "SENTINEL", "status": "review"})
        second = await _get_job_progress(test_db, "term-1")
        assert second["id"] == "SENTINEL", "终态快照未命中缓存（仍走了 DB 全量计算）"

    @pytest.mark.asyncio
    async def test_cache_hit_returns_shallow_copy(self, test_db, clean_cache):
        """命中返回浅拷贝 —— 调用方写入不得污染缓存。"""
        from api.jobs import _get_job_progress

        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, finished_at) "
            "VALUES (?, ?, ?, ?, datetime('now','localtime'))",
            ("term-copy", "c.pdf", "/tmp/c.pdf", "review"),
        )
        await test_db.commit()

        first = await _get_job_progress(test_db, "term-copy")
        first["status"] = "TAMPERED"
        second = await _get_job_progress(test_db, "term-copy")
        assert second["status"] == "review", "缓存节点被调用方改坏"

    @pytest.mark.asyncio
    async def test_active_job_never_cached(self, test_db, clean_cache):
        """活跃 job 快照每轮变化，不得进缓存。"""
        from api.jobs import _get_job_progress

        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) VALUES (?, ?, ?, ?)",
            ("act-1", "a.pdf", "/tmp/a.pdf", "analyzing"),
        )
        await test_db.commit()

        await _get_job_progress(test_db, "act-1")
        assert "act-1" not in clean_cache._TERMINAL_SNAP_CACHE

    @pytest.mark.asyncio
    async def test_status_change_invalidates(self, test_db, clean_cache):
        """同一 job 行被复用（retry）→ status 变化必须失效重算。"""
        from api.jobs import _get_job_progress

        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, finished_at) "
            "VALUES (?, ?, ?, ?, datetime('now','localtime'))",
            ("re-1", "r.pdf", "/tmp/r.pdf", "error"),
        )
        await test_db.commit()
        assert (await _get_job_progress(test_db, "re-1"))["status"] == "error"

        await test_db.execute(
            "UPDATE jobs SET status = 'analyzing', finished_at = NULL WHERE id = 're-1'"
        )
        await test_db.commit()
        assert (await _get_job_progress(test_db, "re-1"))["status"] == "analyzing"

    @pytest.mark.asyncio
    async def test_finished_at_change_invalidates(self, test_db, clean_cache):
        """同一状态但 finished_at 前进（重跑完成）→ 必须失效重算。"""
        from api.jobs import _get_job_progress

        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, finished_at) "
            "VALUES (?, ?, ?, ?, '2026-01-01 00:00:00')", ("re-2", "s.pdf", "/tmp/s.pdf", "review"),
        )
        await test_db.commit()
        await _get_job_progress(test_db, "re-2")

        await test_db.execute(
            "UPDATE jobs SET finished_at = '2026-01-02 00:00:00' WHERE id = 're-2'"
        )
        await test_db.commit()
        key, _snap = clean_cache._TERMINAL_SNAP_CACHE["re-2"]
        assert key[1] == "2026-01-01 00:00:00"

        await _get_job_progress(test_db, "re-2")
        key2, _snap2 = clean_cache._TERMINAL_SNAP_CACHE["re-2"]
        assert key2[1] == "2026-01-02 00:00:00", "finished_at 变化未使缓存失效"

    @pytest.mark.asyncio
    async def test_cache_overflow_is_cleared(self, test_db, clean_cache):
        """缓存达上限 → 整体清空后写入新快照（覆盖容量兜底分支）。"""
        from api.jobs import _get_job_progress
        from api.jobs.status import _TERMINAL_SNAP_CACHE_MAX

        for i in range(_TERMINAL_SNAP_CACHE_MAX):
            clean_cache._TERMINAL_SNAP_CACHE[f"x{i}"] = (("review", ""), {"id": f"x{i}"})
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, finished_at) "
            "VALUES (?, ?, ?, ?, datetime('now','localtime'))",
            ("term-2", "c.pdf", "/tmp/c.pdf", "error"),
        )
        await test_db.commit()

        await _get_job_progress(test_db, "term-2")
        assert "term-2" in clean_cache._TERMINAL_SNAP_CACHE
        assert len(clean_cache._TERMINAL_SNAP_CACHE) == 1  # 旧缓存被清空


class TestLiveSnapshotStillCoversTerminal:
    @pytest.mark.asyncio
    async def test_live_snapshot_includes_recent_terminal(self, test_db, clean_cache):
        """聚合流仍需带上近 10 分钟完成的终态 job（缓存收敛后行为不变）。"""
        from api.jobs import _live_jobs_snapshot

        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, finished_at) "
            "VALUES (?, ?, ?, ?, datetime('now','localtime'))",
            ("term-3", "d.pdf", "/tmp/d.pdf", "review"),
        )
        await test_db.commit()

        snaps = await _live_jobs_snapshot(test_db)
        assert any(s["id"] == "term-3" for s in snaps), snaps
        assert "term-3" in clean_cache._TERMINAL_SNAP_CACHE

    @pytest.mark.asyncio
    async def test_steady_state_query_count_drops(self, test_db, clean_cache, monkeypatch):
        """S2 验收「稳态 QPS 可测下降」：终态 job 的快照查询数 5 → 0。

        终态 job 每轮仍参与聚合流推送，但快照查询（1 行 + 3 COUNT + 1 分组）
        第二轮起全部命中缓存 —— 只剩列出候选 job 的那 1 条查询。
        """
        from api.jobs import _live_jobs_snapshot

        for i in range(3):
            await test_db.execute(
                "INSERT INTO jobs (id, filename, pdf_path, status, finished_at) "
                "VALUES (?, ?, ?, ?, datetime('now','localtime'))",
                (f"term-q{i}", f"q{i}.pdf", f"/tmp/q{i}.pdf", "review"),
            )
        await test_db.commit()

        orig = test_db.execute
        counter = {"n": 0}

        async def counting_execute(*a, **k):
            counter["n"] += 1
            return await orig(*a, **k)

        monkeypatch.setattr(test_db, "execute", counting_execute)

        await _live_jobs_snapshot(test_db)
        first = counter["n"]
        counter["n"] = 0
        await _live_jobs_snapshot(test_db)  # 命中缓存
        second = counter["n"]

        # 首轮：1（列候选）+ 3 终态 job × 5 条 = 16
        assert first == 16, f"首轮查询数异常：{first}"
        # 稳态：只剩 1 条候选列举，终态快照全部复用
        assert second == 1, f"稳态未复用缓存：{second} 条查询"
