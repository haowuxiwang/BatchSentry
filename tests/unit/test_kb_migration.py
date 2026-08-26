"""KB v8 migration tests — kb_entries table + findings.kb_refs column.

Mirrors the v7 migration test pattern in test_gmp_basis.py: build a v7-shaped
DB by hand, run migrate(), assert v8 artifacts exist and are writable, then
re-run for idempotency.
"""
import aiosqlite
import pytest


async def _make_v7_db(db):
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
            gmp_basis TEXT
        )
    """)
    await db.execute(
        "INSERT INTO findings (job_id, page, type, severity, description) "
        "VALUES ('j', 1, 'completeness', 'warning', 'x')"
    )
    await db.execute("PRAGMA user_version = 7")
    await db.commit()


class TestMigrationV8:
    @pytest.mark.asyncio
    async def test_v7_to_v8_creates_table_and_column(self, tmp_path):
        from db.client import migrate

        async with aiosqlite.connect(str(tmp_path / "v8.db")) as db:
            await _make_v7_db(db)
            await migrate(db)

            cur = await db.execute("PRAGMA table_info(findings)")
            cols = {r["name"] for r in await cur.fetchall()}
            assert "kb_refs" in cols

            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='kb_entries'")
            assert (await cur.fetchone()) is not None

            cur = await db.execute("PRAGMA user_version")
            assert (await cursor_version(cur)) == 9

            # 旧数据无损 + 新列可写
            cur = await db.execute("SELECT description FROM findings")
            assert (await cur.fetchone())[0] == "x"
            await db.execute(
                "INSERT INTO findings (job_id, page, type, severity, "
                "description, kb_refs) VALUES ('j', 1, 'completeness', "
                "'info', 'y', ?)",
                ('[{"entry_id":"gmp2010-151","label":"第一百五十一条",'
                 '"excerpt":"...","score":9.9}]',),
            )
            await db.commit()

    @pytest.mark.asyncio
    async def test_migration_idempotent(self, tmp_path):
        from db.client import init_schema, migrate

        async with aiosqlite.connect(str(tmp_path / "again.db")) as db:
            await _make_v7_db(db)
            await migrate(db)
            await migrate(db)  # 第二次不得报错/重复加列
            cur = await db.execute("PRAGMA table_info(findings)")
            cols = [r["name"] for r in await cur.fetchall()]
            assert cols.count("kb_refs") == 1


async def cursor_version(cur):
    row = await cur.fetchone()
    return row[0]
