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
SCHEMA_VERSION = 15


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

    if current_version < 11:
        await _migrate_v11(db)

    if current_version < 12:
        await _migrate_v12(db)

    if current_version < 13:
        await _migrate_v13(db)

    if current_version < 14:
        await _migrate_v14(db)

    if current_version < 15:
        await _migrate_v15(db)

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


async def _migrate_v11(db: aiosqlite.Connection):
    """v11: 抑制留痕 + 区域级证据锚（P0-2 / P0-3）。

    - ``page_cache.regions_json``：OCR 原始区域 bbox（表/块级）。两个后端都
      **不提供单元格级 bbox**（见 docs/NOISE_REDUCTION_SPIKE.md），故只存区域
      级；坐标必须连同其所在坐标系 {"space":[w,h]} 一并保存 —— 裸坐标无法
      归一化，也就无法与渲染页对齐，属禁止写入的形态。
    - ``finding_suppressions`` 台账的建表语句**不在此处**：`init_db()` 每次启动
      都先跑 ``schema.sql``（`CREATE TABLE IF NOT EXISTS`），旧库同样会被补建，
      故 schema.sql 是它的**唯一**声明处。迁移体里复制一份 = 两处 DDL 各自演化
      → 静默漂移（本项目明令禁止）。此处仅保留 schema.sql 无法表达的"给已存在
      表加列"。
    """
    try:
        cur = await db.execute("PRAGMA table_info(page_cache)")
        cols = {row["name"] for row in await cur.fetchall()}
        if "regions_json" not in cols:
            await db.execute("ALTER TABLE page_cache ADD COLUMN regions_json TEXT")
            logger.info("Migration: added page_cache.regions_json")
    except Exception as e:
        logger.warning(f"Migration skip page_cache.regions_json: {e}")
    await db.commit()


async def _migrate_v12(db: aiosqlite.Connection):
    """v12: ``jobs.last_activity_at`` —— 运行时看门狗所需的"最后推进时刻"。

    为什么需要它：看门狗要回答"这个 job 还在动吗"，而此前**没有任何一列**
    能表达这件事 —— ``created_at`` 只有起点、``finished_at`` 非终态为空、
    ``ocr_progress`` 只有值没有时间戳、``audit_log`` 在 51 页分析期间可能
    数十分钟无写入（粒度不足，任何基于它的阈值都会误杀长任务）。

    为什么**不给默认值**：``ALTER TABLE ... ADD COLUMN`` 的默认值必须是常量，
    唯二允许的表达式默认 ``CURRENT_TIMESTAMP`` 是 **UTC** —— 与本项目全库
    ``datetime('now','localtime')`` 的口径冲突，混用会让"停滞多久"算错时区
    偏移。故列本身无默认，由代码在写入点显式赋值；``NULL`` 一律视为
    **不可判定 → 看门狗跳过**（宁缺勿错）。

    为什么能确定代码会写：写入点集中在 ``core/pipeline/state.py`` 的
    ``touch_activity``（状态迁移 / OCR 进度 / 自愈进度 / 跨页进度）+ Stage 2
    逐页分析完成 + ``api/jobs/upload.py`` 创建 job，均有回归用例覆盖。
    """
    try:
        cur = await db.execute("PRAGMA table_info(jobs)")
        cols = {row["name"] for row in await cur.fetchall()}
        if "last_activity_at" not in cols:
            await db.execute("ALTER TABLE jobs ADD COLUMN last_activity_at TIMESTAMP")
            logger.info("Migration: added jobs.last_activity_at")
    except Exception as e:
        logger.warning(f"Migration skip jobs.last_activity_at: {e}")
    await db.commit()


async def _migrate_v13(db: aiosqlite.Connection):
    """v13: ``llm_call_audit.finish_reason`` —— LLM 结束原因（TODO 0-38）。

    为什么需要：这是**唯一**能区分「模型自然收尾」与「被 max_tokens 硬截断」
    的信号。此前全库未采集它 ⇒ 事后无法回答"这一页为什么失败"，而 fix-hint
    重试对截断型失败是**纯浪费**（实测单次白烧 903.2s + 8000 token）。

    归一化值（stop|length|other），原始枚举另见调用日志；旧行保持 NULL ⇒
    读取期视为「未采集」，不参与任何判定（历史 job 无需回填、功能不降级）。
    """
    try:
        cur = await db.execute("PRAGMA table_info(llm_call_audit)")
        cols = {row["name"] for row in await cur.fetchall()}
        if "finish_reason" not in cols:
            await db.execute(
                "ALTER TABLE llm_call_audit ADD COLUMN finish_reason TEXT")
            logger.info("Migration: added llm_call_audit.finish_reason")
    except Exception as e:
        logger.warning(f"Migration skip llm_call_audit.finish_reason: {e}")
    await db.commit()



async def _migrate_v14(db: aiosqlite.Connection):
    """v14: ``findings_archive`` —— 重分析前的 findings 归档台账（TODO 0-44）。

    为什么需要：``review → retry`` 是**全量重新分析**，会 ``DELETE FROM findings``。
    此前没有归档 ⇒ 已裁决行的 ``reviewer_note`` / ``reviewed_at`` / ``corrected_text``
    连同 ``analysis_reset`` 的固定文案一起消失，事后无法回答"清空前有几条被确认过、
    驳回理由是什么"（GMP 数据完整性要求记录可追溯）。

    ⚠️ 刻意**不设外键**：原行已被删除，``finding_id`` 只是历史 id 快照 ——
    与 ``audit_log.finding_id`` 同一取舍。本表只增不改，是台账不是工作集。

    ⚠️ 本函数**刻意不写 DDL** —— 表结构只声明在 ``db/schema.sql``（与 ``_migrate_v11``
    对 ``finding_suppressions`` 的同一取舍）。这不是风格偏好，是可核的事实：
    ``get_db()`` 是 ``migrate()`` 的**唯一**调用方，且它**先**跑 ``init_schema()``
    （``schema.sql`` 全量执行，全是 ``IF NOT EXISTS``）⇒ 走到这里时表**一定已存在**，
    迁移体里的 ``CREATE TABLE`` 只会被静默跳过 —— 那是**死代码**，却让读代码的人
    以为"表是迁移建的"，还会与 schema.sql 各自演化成静默漂移（本项目明令禁止）。
    这里只留一条日志：排查"老库为什么没有这张表"时，答案永远是「``schema.sql`` 没跑」。

    （``_migrate_v1`` 的 ``audit_log``、``_migrate_v7`` 的 ``kb_entries`` 里各有一份
    历史遗留的 ``CREATE TABLE`` —— 那是 v11 把"唯一声明处"定为规矩**之前**的产物，
    本函数不复制那个做法，也不在本次改动里回头动它们。）
    """
    logger.info("Migration: v14 findings_archive declared in schema.sql (no DDL here)")


async def _migrate_v15(db: aiosqlite.Connection):
    """v15: ``findings.guard_reason`` —— 收权/降级理由与用户文案**分离**（CR-1）。

    为什么需要：``apply_review`` 此前把护栏理由以 ``｜理由`` **追加到 description**，
    于是复核页正文里出现「v4 prompt 已禁止此类表述」「L2 溯源失败」「LLM 独断 critical」
    这类**内部语**。用户明确：提示里返回后端代码/系统提示词是不可接受的。
    更坏的一种形态（实测 p22 的 ``1563``）：业务正文本身为空，追加后**整条只剩理由**
    —— 用户看到的就是一句后端日志。

    所以理由必须另存一列：结论变更仍可解释（GMP），但不污染业务文案。
    旧行保持 NULL ⇒ 读取期视为"未采集"，不参与判定；历史 job 无需回填。
    """
    for table in ("findings", "findings_archive"):
        try:
            # 先确认表存在：`findings_archive` 是 v14 才声明的，构造性/极旧库
            # 可能没有它。靠 `PRAGMA table_info` 抛异常来兜底会让"表不存在"
            # 和"迁移失败"混成同一条 warning（排查时看不出区别）。
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            if await cur.fetchone() is None:
                continue
            cur = await db.execute(f"PRAGMA table_info({table})")
            cols = {row["name"] for row in await cur.fetchall()}
            if "guard_reason" not in cols:
                await db.execute(
                    f"ALTER TABLE {table} ADD COLUMN guard_reason TEXT")
                logger.info(f"Migration: added {table}.guard_reason")
        except Exception as e:
            logger.warning(f"Migration skip {table}.guard_reason: {e}")
    await db.commit()

async def close_db():
    global _db, _db_init_lock
    if _db:
        await _db.close()
        _db = None
    # 重置初始化锁：asyncio.Lock 绑定事件循环，跨 loop 复用会在 acquire 时报错。
    # 单例连接关闭后必须一并重置，否则测试/重启场景出现潜伏 flake。
    _db_init_lock = None
