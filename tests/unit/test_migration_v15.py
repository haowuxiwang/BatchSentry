"""v15 迁移测试 —— `findings.guard_reason`（CR-1 / R97）。

背景：护栏降级理由此前以 ``｜理由`` **追加到 description**，于是复核页正文里
出现「LLM 独断 critical，未经规则层确定性判据复核」「v4 prompt 已禁止此类表述」
这类内部语；实测 p22 的 `1563` 更极端 —— 业务正文为空，追加后**整条只剩理由**。

修法：理由另存一列，结论变更仍可解释（GMP），但不污染业务文案。

按 v10 测试的同一模式：手工构造 v14 形态的库 → migrate() → 断言新列存在、
旧数据无损、新列可写、重复 migrate 幂等。**外加一条更关键的**：写路径
（stage2 / stage3）必须真的带上这一列，否则"加了列但没人写"是静默失效。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import aiosqlite
import pytest

ROOT = Path(__file__).resolve().parents[2]


async def _make_v14_db(db):
    """v14 形态：findings + findings_archive 都在，但没有 guard_reason。"""
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
            confidence REAL,
            raw_type TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE findings_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            finding_id INTEGER,
            job_id TEXT NOT NULL,
            page INTEGER NOT NULL,
            type TEXT NOT NULL,
            severity TEXT NOT NULL,
            description TEXT NOT NULL,
            confidence REAL,
            raw_type TEXT,
            archived_at TIMESTAMP
        )
    """)
    await db.execute(
        "INSERT INTO findings (job_id, page, type, severity, description) "
        "VALUES ('j', 1, 'completeness', 'warning', '缺少签名')"
    )
    await db.execute(
        "INSERT INTO findings_archive (job_id, page, type, severity, description) "
        "VALUES ('j', 1, 'completeness', 'warning', '旧归档行')"
    )
    await db.execute("PRAGMA user_version = 14")
    await db.commit()


async def _cols(db, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {r["name"] for r in await cur.fetchall()}


@pytest.mark.asyncio
async def test_v14_to_v15_adds_guard_reason_to_both_tables(tmp_path):
    from db.client import SCHEMA_VERSION, migrate

    async with aiosqlite.connect(str(tmp_path / "v15.db")) as db:
        await _make_v14_db(db)
        await migrate(db)

        assert "guard_reason" in await _cols(db, "findings")
        assert "guard_reason" in await _cols(db, "findings_archive")

        cur = await db.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION

        # 旧行无损；新列为 NULL（读取期视为"未采集"，不参与判定）
        cur = await db.execute("SELECT description, guard_reason FROM findings")
        row = await cur.fetchone()
        assert row["description"] == "缺少签名"
        assert row["guard_reason"] is None

        # 新列可写
        await db.execute(
            "INSERT INTO findings (job_id, page, type, severity, description, "
            "guard_reason) VALUES ('j', 2, 'completeness', 'warning', '缺少签名', "
            "'LLM 独断 critical，未经规则层确定性判据复核')"
        )
        await db.commit()
        cur = await db.execute("SELECT guard_reason FROM findings WHERE page = 2")
        assert "LLM 独断" in (await cur.fetchone())["guard_reason"]


@pytest.mark.asyncio
async def test_v15_migration_idempotent(tmp_path):
    from db.client import migrate

    async with aiosqlite.connect(str(tmp_path / "v15b.db")) as db:
        await _make_v14_db(db)
        await migrate(db)
        await migrate(db)  # 第二次应直接跳过（current >= SCHEMA_VERSION）

        for table in ("findings", "findings_archive"):
            names = [r["name"] for r in await (await db.execute(
                f"PRAGMA table_info({table})")).fetchall()]
            assert names.count("guard_reason") == 1, table


@pytest.mark.asyncio
async def test_v15_tolerates_missing_archive_table(tmp_path):
    """极旧/构造性库可能没有 `findings_archive`（v14 才声明）。

    迁移必须**跳过**而不是让整个 migrate 抛错 —— 否则老库升级直接卡死。
    """
    from db.client import migrate

    async with aiosqlite.connect(str(tmp_path / "v15c.db")) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("""
            CREATE TABLE findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL, page INTEGER NOT NULL,
                type TEXT NOT NULL, severity TEXT NOT NULL,
                description TEXT NOT NULL
            )
        """)
        await db.execute("PRAGMA user_version = 14")
        await db.commit()

        await migrate(db)  # 不得抛错
        assert "guard_reason" in await _cols(db, "findings")


# ── 写路径契约：加了列但没人写 = 静默失效 ────────────────────────────────

class TestWritePathCarriesGuardReason:
    """`db/schema.sql` 声明了列，但真正的失效点在**写入**。

    本仓踩过同类坑（`raw_type` 加列后 stage2 漏写）。这里用 AST 取 INSERT
    语句里的列名，正向断言 `guard_reason` 在列清单里 —— 反向证据是
    `test_v14_to_v15...` 里手工插值能写进去，两者合起来才闭合。
    """

    _INSERT_RE = re.compile(
        r"INSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+(findings|findings_archive)\s*\(([^)]*)\)",
        re.IGNORECASE | re.DOTALL,
    )

    def _insert_columns(self, path: Path, table: str = "findings") -> list[set[str]]:
        """收集该文件里 ``INSERT ... INTO <table> (...)`` 的列名集合。

        ⚠️ 用**正则**而不是 `sql.index("(")`：同文件里还有日志文案常量
        （``"... findings inserted (page=..."``）也含 "inserted"/"findings"，
        宽松过滤会把它当 SQL 解析并抛 ValueError —— 假红掩盖真红。

        ⚠️ 必须用 ``utf-8-sig`` 读：本仓 ``api/jobs/actions.py`` 等文件带 BOM，
        ``ast.parse`` 遇到 U+FEFF 会直接 SyntaxError（不是断言失败，是解析崩）。
        """
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        found: list[set[str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            for m in self._INSERT_RE.finditer(node.value):
                if m.group(1).lower() != table:
                    continue
                found.append(
                    {c.strip() for c in m.group(2).split(",") if c.strip()})
        return found

    def test_stage2_llm_page_insert_has_guard_reason(self):
        cols = self._insert_columns(ROOT / "core" / "pipeline" / "stage2.py")
        assert cols, "没找到 stage2 的 findings INSERT（断言会变成空转）"
        assert any("guard_reason" in c for c in cols), cols

    def test_stage3_insert_has_guard_reason(self):
        cols = self._insert_columns(ROOT / "core" / "pipeline" / "stage3.py")
        assert cols, "没找到 stage3 的 findings INSERT（断言会变成空转）"
        assert any("guard_reason" in c for c in cols), cols

    def test_archive_insert_carries_guard_reason(self):
        """归档要能回答"当时为什么改了结论"（0-44 同源要求）。"""
        cols = self._insert_columns(
            ROOT / "api" / "jobs" / "actions.py", "findings_archive")
        assert cols, "没找到归档 INSERT（断言会变成空转）"
        assert any("guard_reason" in c for c in cols), cols

    def test_schema_declares_column_in_both_tables(self):
        sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        # 两张表各一处（findings / findings_archive）
        assert sql.count("guard_reason TEXT") >= 2, sql.count("guard_reason TEXT")
