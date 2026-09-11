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
SCHEMA_VERSION = 10


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
                await conn.execute("PRAGMA busy_timeout=5000")
                await init_schema(conn)
                await migrate(conn)
                _db = conn
    return _db


async def init_schema(db: aiosqlite.Connection):
    # Pre-clean: legacy DBs (user_version < 5) may contain duplicate findings
    # rows written by pre-v5 code (no dedup index then). schema.sql creates the
    # v5 UNIQUE index unconditionally — on a dirty legacy DB that CREATE would
    # fail hard and abort startup (IntegrityError), never reaching the graceful
    # dedupe inside _migrate_v5. Dedupe here first (keep MIN(id) per group) so
    # schema.sql can always complete; idempotent + tolerant for fresh DBs.
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    if (int(row[0]) if row else 0) < 5:
        try:
            probe = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='findings'"
            )
            if await probe.fetchone():
                await db.execute("""
                    DELETE FROM findings
                    WHERE id NOT IN (
                        SELECT MIN(id) FROM findings
                        GROUP BY job_id, source, page, type, description
                    )
                """)
                logger.info("Migration: pre-deduped legacy findings before v5 unique index")
        except Exception as e:
            logger.warning(f"Migration: pre-dedup skipped: {e}")
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

    if current_version < 3:
        await _migrate_v3(db)

    if current_version < 4:
        await _migrate_v4(db)

    if current_version < 5:
        await _migrate_v5(db)

    if current_version < 6:
        await _migrate_v6(db)

    if current_version < 7:
        await _migrate_v7(db)

    if current_version < 8:
        await _migrate_v8(db)

    if current_version < 9:
        await _migrate_v9(db)

    if current_version < 10:
        await _migrate_v10(db)

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
                created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
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


async def _migrate_v3(db: aiosqlite.Connection):
    """v3: jobs.md5 — 上传去重的内容摘要列。"""
    try:
        cursor = await db.execute("PRAGMA table_info(jobs)")
        existing_cols = {row["name"] for row in await cursor.fetchall()}
        if "md5" not in existing_cols:
            await db.execute("ALTER TABLE jobs ADD COLUMN md5 TEXT")
            logger.info("Migration: added jobs.md5")
    except Exception as e:
        logger.warning(f"Migration skip jobs.md5: {e}")
    await db.commit()


async def _migrate_v4(db: aiosqlite.Connection):
    """v4: findings.user_rule_id — 用户规则命中溯源列（GMP 可追溯）。"""
    try:
        cursor = await db.execute("PRAGMA table_info(findings)")
        finding_cols = {row["name"] for row in await cursor.fetchall()}
        if "user_rule_id" not in finding_cols:
            await db.execute(
                "ALTER TABLE findings ADD COLUMN user_rule_id TEXT"
            )
            logger.info("Migration: added findings.user_rule_id")
    except Exception as e:
        logger.warning(f"Migration skip findings.user_rule_id: {e}")
    await db.commit()


async def _migrate_v5(db: aiosqlite.Connection):
    """v5: findings 去重 UNIQUE 索引 — retry 防重复插入 + 批量写入性能。

    旧代码先查后插无索引保护，重复运行 Stage 3 可能产生完全相同的行
    （rule 确定性生成）。UNIQUE 索引让 INSERT OR IGNORE 原子去重，
    同时避免重复行残留在历史库中（先删后建）。
    """
    try:
        # 1) 清掉历史重复行（保留每指纹的最小 id）
        await db.execute("""
            DELETE FROM findings
            WHERE id NOT IN (
                SELECT MIN(id) FROM findings
                GROUP BY job_id, source, page, type, description
            )
        """)
        # 2) 建 UNIQUE 索引（重复行已清，可安全创建）
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_findings_dedup
            ON findings(job_id, source, page, type, description)
        """)
        logger.info("Migration: findings dedup UNIQUE index ready")
    except Exception as e:
        logger.warning(f"Migration skip findings dedup index: {e}")
    await db.commit()


async def _migrate_v6(db: aiosqlite.Connection):
    """v6: persist page-level OCR integrity evidence for GMP review."""
    try:
        cursor = await db.execute("PRAGMA table_info(page_cache)")
        existing_cols = {row["name"] for row in await cursor.fetchall()}
        if "ocr_diagnostics" not in existing_cols:
            await db.execute(
                "ALTER TABLE page_cache ADD COLUMN ocr_diagnostics TEXT"
            )
            logger.info("Migration: added page_cache.ocr_diagnostics")
        # Backfill the known MinerU table-degradation signature so historical
        # review pages do not continue presenting a silent false success.
        await db.execute(
            "UPDATE page_cache SET ocr_diagnostics = ? "
            "WHERE ocr_diagnostics IS NULL AND raw_html LIKE '%simple_table%'",
            ('{"source":"historical","integrity":"incomplete",'
             '"reasons":["检测到 OCR 缺失内容占位"]}',),
        )
    except Exception as e:
        logger.warning(f"Migration skip page_cache.ocr_diagnostics: {e}")
    await db.commit()


async def _migrate_v7(db: aiosqlite.Connection):
    """v7: GMP 法规依据引用 — findings.gmp_basis（gmp_basis.py 映射）。"""
    try:
        cursor = await db.execute("PRAGMA table_info(findings)")
        existing_cols = {row["name"] for row in await cursor.fetchall()}
        if "gmp_basis" not in existing_cols:
            await db.execute(
                "ALTER TABLE findings ADD COLUMN gmp_basis TEXT"
            )
            logger.info("Migration: added findings.gmp_basis")
    except Exception as e:
        logger.warning(f"Migration skip findings.gmp_basis: {e}")
    await db.commit()


async def _migrate_v8(db: aiosqlite.Connection):
    """v8: knowledge base - kb_entries mirror + findings.kb_refs citations."""
    await db.execute(
        "CREATE TABLE IF NOT EXISTS kb_entries ("
        "entry_id TEXT PRIMARY KEY,"
        "source_id TEXT NOT NULL,"
        "chapter TEXT,"
        "article_label TEXT NOT NULL,"
        "no INTEGER,"
        "text TEXT NOT NULL)"
    )
    try:
        cur = await db.execute("PRAGMA table_info(findings)")
        cols = {row["name"] for row in await cur.fetchall()}
        if "kb_refs" not in cols:
            await db.execute("ALTER TABLE findings ADD COLUMN kb_refs TEXT")
            logger.info("Migration: added findings.kb_refs")
    except Exception as e:
        logger.warning(f"Migration skip findings.kb_refs: {e}")
    await db.commit()


async def _migrate_v9(db: aiosqlite.Connection):
    """v9: KB-2 RAG provenance - llm_call_audit.kb_used (injected entries)."""
    try:
        cur = await db.execute("PRAGMA table_info(llm_call_audit)")
        cols = {row["name"] for row in await cur.fetchall()}
        if "kb_used" not in cols:
            await db.execute(
                "ALTER TABLE llm_call_audit ADD COLUMN kb_used TEXT")
            logger.info("Migration: added llm_call_audit.kb_used")
    except Exception as e:
        logger.warning(f"Migration skip llm_call_audit.kb_used: {e}")
    await db.commit()


async def _migrate_v10(db: aiosqlite.Connection):
    """v10: findings.confidence + findings.raw_type（M2 质量可度量/类型白名单）。

    - confidence：写入期置信度，支持 SQL 排序/阈值筛选。旧行保持 NULL →
      读取期（api/review.py）现算兜底，历史 job 无需回填、功能不降级。
    - raw_type：类型归一前的原始 type，仅在发生归一时写入（GMP 可追溯）。
    """
    wanted = (
        ("confidence", "confidence REAL"),
        ("raw_type", "raw_type TEXT"),
    )
    try:
        cur = await db.execute("PRAGMA table_info(findings)")
        cols = {row["name"] for row in await cur.fetchall()}
        for name, col_def in wanted:
            if name not in cols:
                await db.execute(f"ALTER TABLE findings ADD COLUMN {col_def}")
                logger.info(f"Migration: added findings.{name}")
    except Exception as e:
        logger.warning(f"Migration skip findings v10 columns: {e}")
    await db.commit()


async def close_db():
    global _db, _db_init_lock
    if _db:
        await _db.close()
        _db = None
    # 重置初始化锁：asyncio.Lock 绑定事件循环，跨 loop 复用会在 acquire 时报错。
    # 单例连接关闭后必须一并重置，否则测试/重启场景出现潜伏 flake。
    _db_init_lock = None
