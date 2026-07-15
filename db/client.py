"""Async SQLite client using aiosqlite."""
import aiosqlite
from pathlib import Path
from config import config


_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        db_path = config["app"].database_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(db_path)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
        await init_schema(_db)
    return _db


async def init_schema(db: aiosqlite.Connection):
    schema_path = Path(__file__).parent / "schema.sql"
    schema = schema_path.read_text(encoding="utf-8")
    await db.executescript(schema)
    await db.commit()


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None
