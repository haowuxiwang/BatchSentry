"""v10 迁移测试 —— findings.confidence + findings.raw_type（M2/T2.4 + T2.6）。

按 v8/v7 迁移测试的同一模式：手工构造 v9 形态的库 → migrate() → 断言新列
存在、旧数据无损、新列可写、重复 migrate 幂等。
"""
import aiosqlite
import pytest


async def _make_v9_db(db):
    db.row_factory = aiosqlite.Row
    await db.execute("""
        CREATE TABLE findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            page INTEGER NOT NULL,
            type TEXT NOT NULL,
            severity TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            source TEXT DEFAULT 'rule',
            kb_refs TEXT
        )
    """)
    await db.execute(
        "INSERT INTO findings (job_id, page, type, severity, description) "
        "VALUES ('j', 1, 'completeness', 'warning', 'x')"
    )
    await db.execute("PRAGMA user_version = 9")
    await db.commit()


@pytest.mark.asyncio
async def test_v9_to_v10_adds_confidence_and_raw_type(tmp_path):
    from db.client import SCHEMA_VERSION, migrate

    async with aiosqlite.connect(str(tmp_path / "v10.db")) as db:
        await _make_v9_db(db)
        await migrate(db)

        cur = await db.execute("PRAGMA table_info(findings)")
        cols = {r["name"] for r in await cur.fetchall()}
        assert "confidence" in cols and "raw_type" in cols

        cur = await db.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION

        # 旧行无损，新列为 NULL（读取期兜底现算）
        cur = await db.execute("SELECT description, confidence, raw_type FROM findings")
        row = await cur.fetchone()
        assert row["description"] == "x"
        assert row["confidence"] is None and row["raw_type"] is None

        # 新列可写
        await db.execute(
            "INSERT INTO findings (job_id, page, type, severity, description, "
            "confidence, raw_type) VALUES ('j', 2, 'uncategorized', 'info', 'y', "
            "0.65, 'weird_llm_type')"
        )
        await db.commit()
        cur = await db.execute(
            "SELECT confidence, raw_type FROM findings WHERE page = 2")
        r = await cur.fetchone()
        assert r["confidence"] == 0.65 and r["raw_type"] == "weird_llm_type"


@pytest.mark.asyncio
async def test_v10_migration_idempotent(tmp_path):
    from db.client import migrate

    async with aiosqlite.connect(str(tmp_path / "v10b.db")) as db:
        await _make_v9_db(db)
        await migrate(db)
        await migrate(db)  # 第二次应直接跳过（current >= SCHEMA_VERSION）

        cur = await db.execute("PRAGMA table_info(findings)")
        names = [r["name"] for r in await cur.fetchall()]
        assert names.count("confidence") == 1
        assert names.count("raw_type") == 1
