"""R125 / TODO B2-9：可观测性的两个洞 —— 审计写失败静默、自述不含"卡的是哪个 job"。

## ① 审计写失败只留一条 warning

`_audit_log` 与"被拒转换"的审计都 `except Exception: logger.warning(...)`。
后果不是"少一条日志"：GMP 追溯链出现**空洞且无信号**时，事后无法分辨
"这件事没发生"与"这件事发生了但没记上"。处置三条口径（见
`core.pipeline.state.record_audit_failure` 的注释）：error 级 + 进程内计数 +
**不重试**（execute/commit 失败时无法判断 INSERT 是否已进入事务，重试可能双写）。

## ② 自述只回聚合计数

`status_snapshot()` 能回答"有几个 job 卡了"，不能回答"**是哪个**"。
`find_stalled_jobs()` 本来就能给出，只是没人用。`runtime_report()` 把两者拼上，
并给清单加上限（`find_stalled_jobs` 是全表扫，响应体不能随库增长）。

## 不得读成已通过的部分

- 计数是**进程内**的：进程重启即归零（`uvicorn` 强制 `workers=1`，故进程内
  计数就是全局计数；重启后归零这件事本身**没有**告警）。
- 本文件不验证"审计表真的坏了"的端到端形态，只验证**失败路径的可见性**。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from core.pipeline import state as pipeline_state


class _BoomDB:
    """所有写都失败的 db —— 只用来打审计失败路径。"""

    async def execute(self, *_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    async def commit(self):  # pragma: no cover - 失败发生在 execute
        raise AssertionError("execute 已抛，不应走到 commit")


class _FailAuditInsert:
    """代理真连接，但让"被拒转换"那条审计 INSERT 失败。"""

    def __init__(self, inner):
        self._inner = inner

    async def execute(self, sql, params=()):
        if "INSERT INTO audit_log" in sql and "status_transition_blocked" in sql:
            raise sqlite3.OperationalError("audit_log is locked")
        return await self._inner.execute(sql, params)

    async def commit(self):
        return await self._inner.commit()


@pytest.fixture(autouse=True)
def _reset_audit_counter():
    """计数是全局的 —— 用例之间必须隔离（并原样恢复，避免污染其他文件）。"""
    saved = dict(pipeline_state._AUDIT_FAILURES)
    pipeline_state._AUDIT_FAILURES.update(
        count=0, last_action=None, last_error=None, last_at=None
    )
    yield
    pipeline_state._AUDIT_FAILURES.clear()
    pipeline_state._AUDIT_FAILURES.update(saved)


@pytest_asyncio.fixture
async def client(test_db):
    from main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as c:
        yield c


async def _seed_job(test_db, job_id: str, *, status: str = "analyzing",
                    minutes_ago: int = 180, total_pages: int = 1) -> str:
    ts = (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    await test_db.execute(
        "INSERT INTO jobs (id, filename, status, total_pages, last_activity_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (job_id, f"{job_id}.pdf", status, total_pages, ts),
    )
    await test_db.commit()
    return ts


# ─────────────────────────────────────────────────────────────────────────────
# ① 审计写失败不再静默
# ─────────────────────────────────────────────────────────────────────────────


class TestAuditWriteFailureIsVisible:
    @pytest.mark.asyncio
    async def test_failure_logs_at_error_and_counts(self, caplog):
        with caplog.at_level(logging.ERROR, logger="core.pipeline.state"):
            await pipeline_state._audit_log(
                _BoomDB(), "r125-audit", "pipeline_start", "pdf=x.pdf"
            )

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "审计写失败只打了 warning —— 会被日常噪声淹没"

        info = pipeline_state.audit_write_failures()
        assert info["count"] == 1
        assert info["last_action"] == "pipeline_start"
        assert "disk I/O error" in info["last_error"]
        assert info["last_at"]

    @pytest.mark.asyncio
    async def test_blocked_transition_audit_shares_the_same_counter(
        self, test_db, caplog
    ):
        """两个审计写入点必须共用一个计数 —— 否则自述只覆盖一半路径。"""
        await _seed_job(test_db, "r125-blocked", status="archived", minutes_ago=0)

        with caplog.at_level(logging.ERROR, logger="core.pipeline.state"):
            with pytest.raises(pipeline_state.InvalidTransitionError):
                await pipeline_state._transition_status_unlocked(
                    _FailAuditInsert(test_db), "r125-blocked", "ocr_running", "测试"
                )

        info = pipeline_state.audit_write_failures()
        assert info["count"] == 1
        assert info["last_action"] == "status_transition_blocked"

    @pytest.mark.asyncio
    async def test_success_path_does_not_count(self, test_db):
        """反向对照：正常写入不得让计数上涨（否则指标变成噪声）。"""
        await _seed_job(test_db, "r125-ok", minutes_ago=0)
        await pipeline_state._audit_log(test_db, "r125-ok", "pipeline_start", "ok")

        assert pipeline_state.audit_write_failures()["count"] == 0
        cur = await test_db.execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE job_id = ?", ("r125-ok",)
        )
        assert (await cur.fetchone())["n"] == 1

    def test_snapshot_is_a_copy(self):
        """自述快照必须是副本 —— 否则调用方改一个字段就篡改了全局状态。"""
        info = pipeline_state.audit_write_failures()
        info["count"] = 999
        assert pipeline_state.audit_write_failures()["count"] == 0

    @pytest.mark.asyncio
    async def test_endpoint_exposes_the_counter(self, client, caplog):
        with caplog.at_level(logging.ERROR, logger="core.pipeline.state"):
            await pipeline_state._audit_log(_BoomDB(), "r125-ep", "stuck_recovery", "")

        body = (await client.get("/api/health/watchdog")).json()
        assert body["audit_write_failures"]["count"] == 1
        assert body["audit_write_failures"]["last_action"] == "stuck_recovery"


# ─────────────────────────────────────────────────────────────────────────────
# ② 自述给出"卡的是哪个 job"
# ─────────────────────────────────────────────────────────────────────────────


class TestStalledJobsAreListed:
    @pytest.mark.asyncio
    async def test_endpoint_lists_the_actual_job(self, client, test_db):
        await _seed_job(test_db, "r125-stalled", minutes_ago=180)

        body = (await client.get("/api/health/watchdog")).json()
        stalled = body["currently_stalled"]
        assert [j["id"] for j in stalled] == ["r125-stalled"]
        item = stalled[0]
        assert item["status"] == "analyzing"
        assert item["elapsed_s"] > item["limit_s"] > 0
        assert body["currently_stalled_total"] == 1
        assert body["currently_stalled_truncated"] is False

    @pytest.mark.asyncio
    async def test_healthy_job_is_not_listed(self, client, test_db):
        await _seed_job(test_db, "r125-healthy", minutes_ago=0)

        body = (await client.get("/api/health/watchdog")).json()
        assert body["currently_stalled"] == []
        assert body["currently_stalled_total"] == 0

    @pytest.mark.asyncio
    async def test_limit_truncates_but_reports_total(self, test_db):
        """上限必须**显式**（静默截断会让"只有 20 个"和"有 200 个"看起来一样）。"""
        from core.watchdog import runtime_report

        for i in range(3):
            await _seed_job(test_db, f"r125-many-{i}", minutes_ago=180)

        report = await runtime_report(stalled_limit=2)
        assert len(report["currently_stalled"]) == 2
        assert report["currently_stalled_total"] == 3
        assert report["currently_stalled_truncated"] is True

    @pytest.mark.asyncio
    async def test_report_is_read_only(self, client, test_db):
        """自述只读 —— 探针被频繁轮询，绝不能顺手改状态。"""
        await _seed_job(test_db, "r125-readonly", minutes_ago=180)

        await client.get("/api/health/watchdog")

        cur = await test_db.execute(
            "SELECT status FROM jobs WHERE id = ?", ("r125-readonly",)
        )
        assert (await cur.fetchone())["status"] == "analyzing"
        cur = await test_db.execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE job_id = ?", ("r125-readonly",)
        )
        assert (await cur.fetchone())["n"] == 0, "自述不该写审计"

    @pytest.mark.asyncio
    async def test_same_predicate_as_find_stalled_jobs(self, test_db):
        """清单必须来自 `find_stalled_jobs()`（同一判据）。

        另写一份查询的后果是"看门狗认为卡了、自述认为没卡"—— 两个数字都
        不可信，而用户只看得见自述那个。
        """
        from core.watchdog import find_stalled_jobs, runtime_report

        await _seed_job(test_db, "r125-p1", minutes_ago=180)
        await _seed_job(test_db, "r125-p2", minutes_ago=0)

        expected = {j["id"] for j in await find_stalled_jobs()}
        report = await runtime_report()
        assert {j["id"] for j in report["currently_stalled"]} == expected


class TestStalledScanDegradesInsteadOfFailing:
    """库读不出来时**降级**，不是整体 500。

    加清单之前这个端点是纯内存的。加进来之后它依赖一次 DB 读，于是"库忙 /
    库坏"会让探针 500 —— 而那正是最需要它的时候。探针的存活字段必须照常返回。
    """

    @pytest.mark.asyncio
    async def test_db_failure_keeps_200_and_says_not_scanned(
        self, client, monkeypatch
    ):
        from core import watchdog

        async def _boom(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(watchdog, "find_stalled_jobs", _boom)

        r = await client.get("/api/health/watchdog")
        assert r.status_code == 200, "库读不出来不该让「看门狗还活着吗」这个探针 500"
        body = r.json()
        # None = 没扫成；[] = 扫了、没卡。**绝不合并**。
        assert body["currently_stalled"] is None
        assert body["currently_stalled_total"] is None
        assert body["currently_stalled_truncated"] is None
        assert "database is locked" in body["currently_stalled_error"]
        # 存活字段照常 —— 否则降级等于把探针本身废掉
        assert "running" in body and "last_scan_at" in body
        assert "stall_limits_s" in body

    @pytest.mark.asyncio
    async def test_empty_list_is_a_conclusion_not_a_failure(self, client):
        """`[]` 必须与 `None` 可区分：前者是结论，后者是没有结论。"""
        body = (await client.get("/api/health/watchdog")).json()
        assert body["currently_stalled"] == []
        assert body["currently_stalled_error"] is None
