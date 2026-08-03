"""Async SQLite client using aiosqlite with schema migration."""
import aiosqlite
import logging
from pathlib import Path
from config import config

logger = logging.getLogger(__name__)

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
        await migrate(_db)
    return _db


async def init_schema(db: aiosqlite.Connection):
    schema_path = Path(__file__).parent / "schema.sql"
    schema = schema_path.read_text(encoding="utf-8")
    await db.executescript(schema)
    await db.commit()


async def migrate(db: aiosqlite.Connection):
    """Add missing columns/tables for existing databases."""
    # Check which columns exist in jobs table
    cursor = await db.execute("PRAGMA table_info(jobs)")
    existing_cols = {row["name"] for row in await cursor.fetchall()}

    migrations = [
        ("failed_pages TEXT", "failed_pages"),
        ("stage1_ms INTEGER", "stage1_ms"),
        ("stage2_ms INTEGER", "stage2_ms"),
        ("stage3_ms INTEGER", "stage3_ms"),
    ]

    for col_def, col_name in migrations:
        if col_name not in existing_cols:
            try:
                await db.execute(f"ALTER TABLE jobs ADD COLUMN {col_def}")
                logger.info(f"Migration: added jobs.{col_name}")
            except Exception as e:
                logger.warning(f"Migration skip jobs.{col_name}: {e}")

    # Create audit_log table if not exists
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                finding_id INTEGER,
                action TEXT NOT NULL,
                detail TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_job ON audit_log(job_id)")
        logger.info("Migration: audit_log table ready")
    except Exception as e:
        logger.warning(f"Migration skip audit_log: {e}")

    # Phase 3: add source column to findings (rule | llm_page | llm_fallback | llm_cross)
    try:
        cursor = await db.execute("PRAGMA table_info(findings)")
        finding_cols = {row["name"] for row in await cursor.fetchall()}
        if "source" not in finding_cols:
            await db.execute("ALTER TABLE findings ADD COLUMN source TEXT DEFAULT 'rule'")
            logger.info("Migration: added findings.source")
    except Exception as e:
        logger.warning(f"Migration skip findings.source: {e}")

    await db.commit()


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None
