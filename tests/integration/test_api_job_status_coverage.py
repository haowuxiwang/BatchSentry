"""GET /api/jobs/{id} 与 /stream 的边界分支补测。

补全 test_api_jobs_coverage.py 未覆盖的：
- 两个端点的非本地 403 守卫
- 状态端点 404
- SSE「进度查询失败」帧分支（DB 抖动 → error 帧 → 恢复终态）
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(test_db):
    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as c:
        yield c


class TestJobStatusGuards:
    @pytest.mark.asyncio
    async def test_status_non_local_rejected(self, test_db):
        from main import app
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.com:8000"
        ) as c:
            r = await c.get("/api/jobs/any")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_status_missing_job_returns_404(self, client):
        r = await client.get("/api/jobs/does-not-exist")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_stream_non_local_rejected(self, test_db):
        from main import app
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.com:8000"
        ) as c:
            r = await c.get("/api/jobs/any/stream")
        assert r.status_code == 403


class TestStreamQueryFailure:
    @pytest.mark.asyncio
    async def test_progress_query_failure_emits_error_then_recovers(
        self, client, test_db, monkeypatch
    ):
        """首轮查询抛异常 → 推送 error 帧；次轮恢复终态 → done 帧收尾。"""
        import api.jobs.status as status_mod

        calls = {"n": 0}

        async def flaky(db, job_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db hiccup")
            return {"id": job_id, "status": "review"}

        monkeypatch.setattr(status_mod, "_get_job_progress", flaky)
        # 短路错误分支的退避 sleep（跳过 SSE 轮询常量对应的 sleep，其余透传）
        import asyncio
        from api.jobs import _SSE_POLL_SECONDS
        real_sleep = asyncio.sleep

        async def fast_sleep(d, *a, **k):
            if d == _SSE_POLL_SECONDS:
                return None
            return await real_sleep(d, *a, **k)

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)

        body = ""
        async with client.stream("GET", "/api/jobs/flaky-job/stream") as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                body += chunk
                if "event: done" in body:
                    break
        assert "进度查询失败" in body
        assert "event: done" in body
        assert calls["n"] >= 2
