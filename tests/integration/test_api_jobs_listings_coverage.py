"""api/jobs/listings.py 终态快照缓存分支补测（M1b）。

补全 test_api_jobs_coverage.py 未覆盖：
- _live_jobs_snapshot 终态快照缓存命中直接复用（97-98）
- 缓存容量超 100 时清空重建（102-104）
"""
from __future__ import annotations

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def clean_cache():
    import api.jobs.listings as listings_mod
    listings_mod._terminal_snap_cache.clear()
    yield listings_mod
    listings_mod._terminal_snap_cache.clear()


class TestTerminalSnapshotCache:
    @pytest.mark.asyncio
    async def test_cache_hit_reuses_snapshot(self, test_db, clean_cache):
        """第二次调用终态 job → 命中缓存直接复用（覆盖 97-98）。"""
        from api.jobs import _live_jobs_snapshot

        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, finished_at) "
            "VALUES (?, ?, ?, ?, datetime('now','localtime'))",
            ("term-1", "b.pdf", "/tmp/b.pdf", "review"),
        )
        await test_db.commit()

        first = await _live_jobs_snapshot(test_db)
        assert any(s["id"] == "term-1" for s in first), first
        assert "term-1" in clean_cache._terminal_snap_cache

        second = await _live_jobs_snapshot(test_db)  # 命中缓存
        assert any(s["id"] == "term-1" for s in second)

    @pytest.mark.asyncio
    async def test_cache_overflow_is_cleared(self, test_db, clean_cache):
        """缓存已满（>100）→ 清空后写入新快照（覆盖 102-104）。"""
        from api.jobs import _live_jobs_snapshot

        for i in range(101):
            clean_cache._terminal_snap_cache[f"x{i}"] = (("review", ""), {"id": f"x{i}"})
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, finished_at) "
            "VALUES (?, ?, ?, ?, datetime('now','localtime'))",
            ("term-2", "c.pdf", "/tmp/c.pdf", "error"),
        )
        await test_db.commit()

        await _live_jobs_snapshot(test_db)
        assert "term-2" in clean_cache._terminal_snap_cache
        assert len(clean_cache._terminal_snap_cache) == 1  # 旧缓存被清空
