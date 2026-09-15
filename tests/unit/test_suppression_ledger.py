"""P0-2 抑制留痕 —— 台账落库 / 迁移 / 呈现面 的机检不变式。

法规依据：EU GMP Annex 11 §16、中国附录《计算机化系统》第 15/16 条要求关键
数据的修改经批准并记录理由；PIC/S PI 041-1 认可"经验证的异常报告"替代逐页复核，
前提是留痕可追溯。因此：

* 抑制**必须返回明细**（禁止只返回计数）；
* 每条抑制必须带**非空 reason**（SQL CHECK + Python 双重把关）；
* 台账必须**有呈现面**（复核页可查、可回退），否则等于没留痕。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import aiosqlite
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from core.rules.spec_guard import (  # noqa: E402
    SUPPRESSION_INSERT_SQL,
    suppression_rows,
)

STAGE2 = REPO / "core" / "pipeline" / "stage2.py"
REVIEW_PY = REPO / "api" / "review.py"
REVIEW_HTML = REPO / "templates" / "review.html"
REVIEW_JS = REPO / "static" / "review.js"
SCHEMA_SQL = REPO / "db" / "schema.sql"


# ── 迁移 ──────────────────────────────────────────────────────────────────


async def _make_v10_db(db: aiosqlite.Connection):
    """构造 v10 库：跑**真实** ``schema.sql``，再回退 v11 的两个增项。

    手写建表会随 schema.sql 演进不断缺列（``jobs.status``、``jobs.created_at``…），
    那样测出来的是"fixture 与 schema 是否同步"而非迁移逻辑本身。回退法让
    fixture 永远与真值同源。
    """
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    await db.execute("DROP TABLE IF EXISTS finding_suppressions")
    await db.execute("ALTER TABLE page_cache DROP COLUMN regions_json")
    await db.execute("INSERT INTO jobs (id, filename) VALUES ('j', 'f.pdf')")
    await db.execute("PRAGMA user_version = 10")
    await db.commit()


async def _upgrade(db: aiosqlite.Connection):
    """复刻 ``db.client.init_db()`` 的真实升级顺序（schema.sql → migrate）。

    不得只用 ``migrate()`` 代替 —— 那会漏掉"旧库由 schema.sql 补建台账表"这
    一关键路径，测试就退化成了对实现细节的自证。
    """
    from db.client import init_schema, migrate

    await init_schema(db)
    await migrate(db)


@pytest.mark.asyncio
async def test_v10_to_v11_adds_ledger_and_regions_column(tmp_path):
    from db.client import SCHEMA_VERSION

    async with aiosqlite.connect(str(tmp_path / "v11.db")) as db:
        await _make_v10_db(db)
        await _upgrade(db)

        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='finding_suppressions'"
        )
        assert await cur.fetchone(), "v11 必须建 finding_suppressions 台账表"

        cur = await db.execute("PRAGMA table_info(page_cache)")
        cols = {r["name"] for r in await cur.fetchall()}
        assert "regions_json" in cols, "v11 必须加 page_cache.regions_json"

        cur = await db.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_v11_migration_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "v11b.db")) as db:
        await _make_v10_db(db)
        await _upgrade(db)
        await _upgrade(db)  # 第二次：migrate 直接跳过，schema.sql 幂等
        cur = await db.execute("PRAGMA table_info(page_cache)")
        names = [r["name"] for r in await cur.fetchall()]
        assert names.count("regions_json") == 1
        cur = await db.execute("SELECT COUNT(*) AS n FROM finding_suppressions")
        assert (await cur.fetchone())["n"] == 0


@pytest.mark.asyncio
async def test_db_check_constraint_rejects_blank_reason(tmp_path):
    """DB 层必须堵死"无理由的抑制"—— 这是不可抽检的记录，法规不允许。

    注意：SQLite 单参 ``trim()`` 默认只去 ASCII 空格，``\\t``/``\\n``/``\\r``
    会漏过 → 必须显式给出空白字符集（本用例即锁定该写法）。
    """
    async with aiosqlite.connect(str(tmp_path / "v11c.db")) as db:
        await _make_v10_db(db)
        await _upgrade(db)
        for blank in ("", "   ", "\t", "\n", "\r", " \t\n "):
            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute(
                    "INSERT INTO finding_suppressions "
                    "(job_id, page, type, severity, description, reason) "
                    "VALUES ('j', 1, 'param_out_of_spec', 'info', 'd', ?)",
                    (blank,),
                )
        # 非空理由必须能正常写入（避免把 CHECK 收紧成误伤）
        await db.execute(
            "INSERT INTO finding_suppressions "
            "(job_id, page, type, severity, description, reason) "
            "VALUES ('j', 1, 'param_out_of_spec', 'info', 'd', ' 规则层复核为合规 ')",
        )
        cur = await db.execute("SELECT COUNT(*) AS n FROM finding_suppressions")
        assert (await cur.fetchone())["n"] == 1


def test_ledger_ddl_has_single_source():
    """台账建表语句只允许声明一次（schema.sql）—— 禁止在迁移体里复制。

    两处 DDL 各自演化 = 静默漂移，且迁移体那份可能悄悄放宽 CHECK；本项目
    对"同一事实两处定义"有明令禁止，此处机检固化。
    """
    schema = SCHEMA_SQL.read_text(encoding="utf-8")
    assert re.search(
        r"CREATE TABLE IF NOT EXISTS finding_suppressions", schema
    ), "schema.sql 必须声明 finding_suppressions"
    client_src = (REPO / "db" / "client.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS finding_suppressions" not in client_src, (
        "迁移体不得复制台账建表语句（schema.sql 是唯一声明处）"
    )
    assert "idx_suppress_dedup" not in client_src, "索引同样归 schema.sql 声明"


@pytest.mark.asyncio
async def test_insert_sql_matches_schema_and_roundtrips(tmp_path):
    """落库语句与 schema 必须一致（防列名/顺序漂移），且能存能读回。"""
    schema = SCHEMA_SQL.read_text(encoding="utf-8")
    async with aiosqlite.connect(str(tmp_path / "v11d.db")) as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(schema)
        await db.execute("INSERT INTO jobs (id, filename) VALUES ('j', 'f.pdf')")
        rows = suppression_rows(
            "j", 9,
            [{
                "finding": {
                    "type": "param_out_of_spec", "severity": "critical",
                    "description": "T2101a 压力 0.16 MPa 超出规格范围 <0.3 MPa",
                    "ocr_text": "T2101a 压力 0.16 MPa",
                },
                "reason": "规则层复核为合规",
                "evidence": {"states": ["in"], "matched": []},
            }],
        )
        await db.executemany(SUPPRESSION_INSERT_SQL, rows)
        await db.commit()
        cur = await db.execute("SELECT * FROM finding_suppressions")
        got = await cur.fetchone()
        assert got["job_id"] == "j" and got["page"] == 9
        assert got["reason"] == "规则层复核为合规"
        assert got["reverted_at"] is None and got["reverted_finding_id"] is None
        assert '"states"' in got["evidence"]


@pytest.mark.asyncio
async def test_suppression_rows_rejects_blank_reason_python_side():
    """Python 侧同样把关（SQL CHECK 之外的第二道）：空理由 → 显式失败。

    SQL 的空白字符集覆盖不到全角空格（U+3000）等 Unicode 空白，而 Python 的
    ``str.strip()`` 可以 —— 两道互补，缺一不可。
    """
    for blank in ("", "   ", "\t", "\u3000", "\u3000 \t"):
        with pytest.raises(ValueError):
            suppression_rows("j", 1, [{
                "finding": {"type": "param_out_of_spec", "description": "d"},
                "reason": blank, "evidence": {},
            }])


@pytest.mark.asyncio
async def test_suppression_rows_skips_malformed_items_without_blank_reason_trap():
    """结构不完整的明细（非 dict / 缺 finding）→ **跳过**而非抛错。

    为什么必须区分：``suppression_rows`` 对"有 finding 但无 reason"抛 ValueError
    是**法规硬要求**（禁止无理由抑制）；而"根本没有 finding"是上游传参脏数据，
    两者混淆会让一次脏传参把整批留痕写入全部打断 —— 留痕宁可少一条也不能整批丢失。
    """
    rows = suppression_rows("j", 1, [
        "not a dict",                       # 非 dict → 跳过
        {"reason": "理由在但无 finding"},     # 缺 finding → 跳过
        {"finding": "not a dict", "reason": "同上"},
        {                                    # 唯一合法项，必须保留
            "finding": {"type": "param_out_of_spec", "severity": "info",
                        "description": "d", "ocr_text": "o"},
            "reason": "规则层复核为合规", "evidence": {},
        },
    ])
    assert len(rows) == 1, f"只应保留合法项，实际 {rows}"
    assert rows[0][0] == "j" and rows[0][2] == "param_out_of_spec"


@pytest.mark.asyncio
async def test_ledger_dedup_index_is_idempotent(tmp_path):
    """重分析/重试重放同一指纹 → 只留一条台账（INSERT OR IGNORE）。"""
    schema = SCHEMA_SQL.read_text(encoding="utf-8")
    async with aiosqlite.connect(str(tmp_path / "v11e.db")) as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(schema)
        await db.execute("INSERT INTO jobs (id, filename) VALUES ('j', 'f.pdf')")
        rows = suppression_rows("j", 3, [{
            "finding": {"type": "param_out_of_spec", "severity": "info",
                        "description": "d", "ocr_text": "o"},
            "reason": "r", "evidence": {},
        }])
        await db.executemany(SUPPRESSION_INSERT_SQL, rows)
        await db.executemany(SUPPRESSION_INSERT_SQL, rows)
        await db.commit()
        cur = await db.execute("SELECT COUNT(*) AS n FROM finding_suppressions")
        assert (await cur.fetchone())["n"] == 1


# ── 源码扫描不变式（防回归到"只记计数"）────────────────────────────────────


def test_stage2_writes_ledger_not_just_count():
    src = STAGE2.read_text(encoding="utf-8")
    assert "finding_suppressions" in src or "SUPPRESSION_INSERT_SQL" in src, (
        "stage2 必须把抑制明细写入 finding_suppressions —— 只记计数等于"
        "不可查、不可回退、不可抽检"
    )
    assert "suppression_rows(" in src, "stage2 必须经由唯一构造点 suppression_rows"
    # 旧形态（仅计数变量 + 仅日志）不得回归
    assert "_dropped_spec" not in src, "禁止回归为只保留计数"


def test_spec_guard_returns_details_signature():
    src = (REPO / "core" / "rules" / "spec_guard.py").read_text(encoding="utf-8")
    m = re.search(
        r"def drop_unfounded_spec_findings\([^)]*\)\s*->\s*([^:]+):", src
    )
    assert m, "drop_unfounded_spec_findings 必须有返回注解"
    ann = m.group(1)
    assert "int" not in ann, "禁止 drop_* 只返回计数（法规留痕硬要求）"
    assert ann.count("list") >= 2, f"必须返回 (保留, 明细列表)，实际 {ann!r}"


def test_api_exposes_query_and_revert_routes():
    src = REVIEW_PY.read_text(encoding="utf-8")
    assert '"/jobs/{job_id}/suppressions"' in src, "缺少抑制台账查询端点"
    assert "suppressions/{suppression_id}/revert" in src, "缺少一键回退端点"
    assert "suppression_reverted" in src, "回退必须写 audit_log"
    assert "is_local_request" in src


def test_review_surface_renders_and_reverts():
    html = REVIEW_HTML.read_text(encoding="utf-8")
    assert 'id="suppression-panel"' in html, "复核页必须给抑制台账留呈现位"
    assert 'id="suppression-list"' in html
    js = REVIEW_JS.read_text(encoding="utf-8")
    assert "renderSuppressions" in js and "loadSuppressions" in js
    assert "window.revertSuppression = revertSuppression" in js, (
        "回退按钮是内联 onclick，必须挂到 window"
    )
