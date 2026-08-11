"""Async SQLite client using aiosqlite with schema migration."""
import aiosqlite
import asyncio
import logging
from pathlib import Path
from config import config

logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None
# 对抗审查(cr-5): 启动期并发 get_db()（lifespan 初始化 + recover_stuck_jobs
# 后台任务 + 首个 API 请求）可能重复建连接：_db is None 检查与 connect 之间
# 有 await 间隙，先建者被覆盖（文件句柄泄漏 + row_factory 初始化错位）。
_db_init_lock: asyncio.Lock | None = None


def _get_init_lock() -> asyncio.Lock:
    global _db_init_lock
    if _db_init_lock is None:
        _db_init_lock = asyncio.Lock()
    return _db_init_lock

# Current schema migration level, persisted via PRAGMA user_version.
SCHEMA_VERSION = 2


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        async with _get_init_lock():
            if _db is None:
                db_path = config["app"].database_path
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(db_path)
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA foreign_keys=ON")
                await init_schema(conn)
                await migrate(conn)
                _db = conn
    return _db


async def init_schema(db: aiosqlite.Connection):
    schema_path = Path(__file__).parent / "schema.sql"
    schema = schema_path.read_text(encoding="utf-8")
    await db.executescript(schema)
    await db.commit()


async def migrate(db: aiosqlite.Connection):
    """Versioned schema migration via PRAGMA user_version.

    Old databases (created before versioning) have user_version=0; the v1
    migration below is probe-based and idempotent, so it safely upgrades them.
    Future changes bump SCHEMA_VERSION and add a guarded migration block.
    """
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    current_version = int(row[0]) if row else 0
    if current_version >= SCHEMA_VERSION:
        return

    if current_version < 1:
        await _migrate_v1(db)

    if current_version < 2:
        await _migrate_v2(db)

    # PRAGMA user_version cannot be parameterized; SCHEMA_VERSION is an int
    # constant defined in this module, so f-string is safe.
    await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    await db.commit()
    logger.info(f"Migration: database schema upgraded to v{SCHEMA_VERSION}")


async def _migrate_v1(db: aiosqlite.Connection):
    """v1: baseline probe-based migration (idempotent)."""
    # Check which columns exist in jobs table
    cursor = await db.execute("PRAGMA table_info(jobs)")
    existing_cols = {row["name"] for row in await cursor.fetchall()}

    migrations = [
        ("failed_pages TEXT", "failed_pages"),
        ("stage1_ms INTEGER", "stage1_ms"),
        ("stage2_ms INTEGER", "stage2_ms"),
        ("stage3_ms INTEGER", "stage3_ms"),
        ("ocr_progress TEXT", "ocr_progress"),
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


async def _migrate_v2(db: aiosqlite.Connection):
    """v2: jobs.ocr_backend_used — 双 OCR 主备切换的审计记录列。"""
    try:
        cursor = await db.execute("PRAGMA table_info(jobs)")
        existing_cols = {row["name"] for row in await cursor.fetchall()}
        if "ocr_backend_used" not in existing_cols:
            await db.execute(
                "ALTER TABLE jobs ADD COLUMN ocr_backend_used TEXT"
            )
            logger.info("Migration: added jobs.ocr_backend_used")
    except Exception as e:
        logger.warning(f"Migration skip jobs.ocr_backend_used: {e}")
    await db.commit()


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None
