"""`review → retry`（全量重新分析）必须**先归档再删除**（TODO 0-44 / 对抗审查 F1）。

## 这条护栏在防什么

`review → retry` 是"全量重新分析"：删 findings、清 `structured_json`、保留 OCR 缓存。
删之前不归档 ⇒ 已裁决行（`confirmed` / `rejected` / `corrected`）连同
`reviewer_note` / `reviewed_at` / `corrected_text` 一起消失；而 `analysis_reset`
的审计 detail 此前是**固定文案**（不含条数、不含状态分布）⇒ 事后无法回答
"清空前有几条被确认过、驳回理由是什么"。这与 GMP「记录可追溯」直接冲突。

## 判据不是"归档表有行"，而是"能重建"

所以断言分三层：

1. **live 侧真的删了** —— 否则"归档"可能只是复制一份，用户以为重跑过、其实没跑；
2. **归档侧逐字段一致** —— `status` / `reviewer_note` / `reviewed_at` / `corrected_text`；
3. **审计 detail 含条数与分布** —— 这才是"可重建"；只有一句固定文案等于没记。

## 不得读成已通过的部分

- 本轮**没有**改重跑语义：仍然是"全量重新分析"，已裁决行**不会**留在复核页。
  归档是**留存**，不是"保留在界面上"（保留在界面上是 A 方案，见 TODO 0-44）。
- 归档与删除的原子性只靠"同一隐式事务"（都在那一次 `commit()` 之前），
  没有显式 `BEGIN`；`retry_job` 整体仍**没有**包进 `db_lock`
  （属 0-45 / F2-3.2，**未修**）。
"""
from __future__ import annotations

from unittest.mock import patch

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


async def _seed_review_job(test_db, tmp_path, job_id: str = "retry-arch") -> None:
    """造一个 review 终态 job：1 页 page_cache + 3 条已裁决/待审 findings。"""
    import fitz
    doc = fitz.open()
    doc.new_page()
    pdf_path = tmp_path / f"{job_id}.pdf"
    doc.save(str(pdf_path))
    doc.close()

    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status) VALUES (?, ?, ?, 'review')",
        (job_id, f"{job_id}.pdf", str(pdf_path)),
    )
    await test_db.execute(
        "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
        "VALUES (?, 1, '<table><tr><td>批号</td></tr></table>', '{\"steps\":[]}')",
        (job_id,),
    )
    rows = [
        # (page, type, status, note, corrected, reviewed_at)
        (1, "完整性", "pending", None, None, None),
        (2, "参数", "confirmed", "已核对原始记录，确认超限", None, "2026-10-01 09:00:00"),
        (3, "计算", "rejected", "复核后判定为误报", "应为 12.5 h", "2026-10-01 09:05:00"),
    ]
    for page, typ, status, note, corrected, reviewed_at in rows:
        await test_db.execute(
            "INSERT INTO findings (job_id, page, type, severity, source, description,"
            " status, reviewer_note, corrected_text, reviewed_at) "
            "VALUES (?, ?, ?, 'warning', 'rule', ?, ?, ?, ?, ?)",
            (job_id, page, typ, f"第{page}页旧结果", status, note, corrected, reviewed_at),
        )
    await test_db.commit()


class TestRetryFromReviewArchivesFindings:
    @pytest.mark.asyncio
    async def test_live_findings_are_cleared(self, client, test_db, tmp_path):
        """第 1 层：重跑语义没变 —— live 侧仍然清空（否则就是偷偷改了行为）。"""
        await _seed_review_job(test_db, tmp_path)
        with patch("api.jobs.launch_pipeline"):
            r = await client.post("/api/jobs/retry-arch/retry")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending"

        cur = await test_db.execute(
            "SELECT COUNT(*) AS n FROM findings WHERE job_id = ?", ("retry-arch",))
        assert (await cur.fetchone())["n"] == 0

    @pytest.mark.asyncio
    async def test_archive_keeps_every_field_verbatim(self, client, test_db, tmp_path):
        """第 2 层：归档必须**逐字段**一致 —— 只存个 id/描述等于没留证据。"""
        await _seed_review_job(test_db, tmp_path)
        with patch("api.jobs.launch_pipeline"):
            await client.post("/api/jobs/retry-arch/retry")

        cur = await test_db.execute(
            "SELECT finding_id, page, status, reviewer_note, corrected_text, reviewed_at,"
            " archive_reason FROM findings_archive WHERE job_id = ? ORDER BY page",
            ("retry-arch",),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 3, rows

        assert [r["status"] for r in rows] == ["pending", "confirmed", "rejected"]
        # 已裁决两行的复核痕迹必须原样保留
        assert rows[1]["reviewer_note"] == "已核对原始记录，确认超限"
        assert rows[1]["reviewed_at"] == "2026-10-01 09:00:00"
        assert rows[2]["reviewer_note"] == "复核后判定为误报"
        assert rows[2]["corrected_text"] == "应为 12.5 h"
        # 待审行本来就没有复核痕迹 ⇒ 归档里也必须是 NULL，不能凭空造
        assert rows[0]["reviewer_note"] is None
        assert rows[0]["reviewed_at"] is None
        # 归档原因可检索（同一张表以后可能被别的动作复用）
        assert {r["archive_reason"] for r in rows} == {"review_retry_full_reanalysis"}
        # finding_id 是原行 id 的快照（不设外键，原行已删）
        assert all(isinstance(r["finding_id"], int) for r in rows)

    @pytest.mark.asyncio
    async def test_audit_detail_is_reconstructable(self, client, test_db, tmp_path):
        """第 3 层：审计 detail 必须含**条数与状态分布**，不是固定文案。"""
        await _seed_review_job(test_db, tmp_path)
        with patch("api.jobs.launch_pipeline"):
            await client.post("/api/jobs/retry-arch/retry")

        cur = await test_db.execute(
            "SELECT detail FROM audit_log WHERE job_id = ? AND action = 'analysis_reset'",
            ("retry-arch",),
        )
        rows = await cur.fetchall()
        assert len(rows) == 1, "analysis_reset 应当且只当写一条"
        detail = rows[0]["detail"]
        assert "3 条" in detail, detail
        for token in ("pending=1", "confirmed=1", "rejected=1"):
            assert token in detail, detail
        # 归档这件事本身也要在文案里（否则读者不知道去 findings_archive 找）
        assert "归档" in detail, detail

    @pytest.mark.asyncio
    async def test_no_findings_writes_zero_and_says_so(self, client, test_db, tmp_path):
        """反向对照：0 条时不得插空行、也不得静默 —— 审计要写「0 条（无）」。"""
        import fitz
        doc = fitz.open()
        doc.new_page()
        pdf_path = tmp_path / "empty.pdf"
        doc.save(str(pdf_path))
        doc.close()
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status) VALUES (?, ?, ?, 'review')",
            ("retry-empty", "empty.pdf", str(pdf_path)),
        )
        await test_db.commit()

        with patch("api.jobs.launch_pipeline"):
            r = await client.post("/api/jobs/retry-empty/retry")
        assert r.status_code == 200, r.text

        cur = await test_db.execute(
            "SELECT COUNT(*) AS n FROM findings_archive WHERE job_id = ?", ("retry-empty",))
        assert (await cur.fetchone())["n"] == 0

        cur = await test_db.execute(
            "SELECT detail FROM audit_log WHERE job_id = ? AND action = 'analysis_reset'",
            ("retry-empty",),
        )
        detail = (await cur.fetchone())["detail"]
        assert "0 条" in detail, detail
        assert "无" in detail, detail


class TestArchiveTableIsMigrated:
    """`findings_archive` 在新库/老库上都必须存在。

    两条路径都靠 `schema.sql` 建表（它是**唯一**声明处）；本类钉的是
    「两条路径都真的执行到了 schema.sql」，而不是「迁移体里有 DDL」。
    """

    @pytest.mark.asyncio
    async def test_fresh_db_has_the_table(self, test_db):
        cur = await test_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='findings_archive'")
        assert await cur.fetchone() is not None

    def test_ddl_is_declared_only_in_schema_sql(self):
        """表结构只许声明在 `db/schema.sql` 一处（`_migrate_v11` 定下的规矩）。

        为什么钉这个：`_migrate_v14` 的初稿把 `CREATE TABLE` 复制进了迁移体，
        而 `get_db()` 总是先跑 `init_schema()` ⇒ 那份 DDL 是**死代码**，
        却让读代码的人以为"表是迁移建的"，还给两份 DDL 留了静默漂移的空间。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        schema = (root / "db" / "schema.sql").read_text(encoding="utf-8")
        client = (root / "db" / "client.py").read_text(encoding="utf-8")
        # 带上结尾的 " (" —— 否则 `findings_archive_x` 这类改名会被前缀匹配放过
        stmt = "CREATE TABLE IF NOT EXISTS findings_archive ("
        assert schema.count(stmt) == 1
        assert stmt not in client

    @pytest.mark.asyncio
    async def test_v13_db_gets_the_table_on_the_real_startup_path(self, tmp_path):
        """老库（user_version=13、没有该表）走**真实启动序列**必须补上。

        真实序列是 ``get_db()`` 里的 ``init_schema()`` → ``migrate()``，**不是**单跑
        ``migrate()``。本用例钉的是「这条真实序列确实能把表补上」，**不是**
        「迁移体里有 DDL」—— 后者曾被当成事实写进初稿，实测是死代码：
        ``init_schema()`` 先跑 ``schema.sql``，走到 v14 时表一定已存在。
        见 ``db/client.py::_migrate_v14`` 的 docstring。
        """
        import aiosqlite
        from db.client import SCHEMA_VERSION, init_schema, migrate

        path = tmp_path / "old.db"
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        try:
            # 只建一张"够 schema.sql 的索引活下来"的 jobs：
            # `idx_jobs_status` / `idx_jobs_created_at` 会引用这两列，
            # 缺了它们 schema.sql 会直接报 no such column。
            await conn.executescript(
                "CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, created_at TIMESTAMP);"
                "PRAGMA user_version = 13;")
            await conn.commit()

            assert SCHEMA_VERSION >= 14, SCHEMA_VERSION
            await init_schema(conn)
            await migrate(conn)

            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='findings_archive'")
            assert await cur.fetchone() is not None
            cur = await conn.execute("PRAGMA user_version")
            assert (await cur.fetchone())[0] == SCHEMA_VERSION
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_real_startup_sequence_is_idempotent(self, tmp_path):
        """连跑两次 ``init_schema()`` + ``migrate()`` 不报错、版本不变（F1-3.3）。

        这是**每次启动都会发生**的事（`get_db()` 无版本判断地先跑 schema.sql），
        所以幂等是硬要求而不是锦上添花：第二次若报 "table already exists"，
        服务就再也起不来了。
        """
        import aiosqlite
        from db.client import SCHEMA_VERSION, init_schema, migrate

        path = tmp_path / "twice.db"
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        try:
            await conn.executescript(
                "CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, created_at TIMESTAMP);")
            await conn.commit()

            for _ in range(2):
                await init_schema(conn)
                await migrate(conn)

            cur = await conn.execute("PRAGMA user_version")
            assert (await cur.fetchone())[0] == SCHEMA_VERSION
            cur = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='findings_archive'")
            assert (await cur.fetchone())[0] == 1
        finally:
            await conn.close()
