"""GMP 法规依据引用（v7）— gmp_basis.py 映射 + findings.gmp_basis 迁移。

借鉴参考产品"7 类知识库检索"的最小可行版：finding type → 法规原则名
映射（不用硬编码条款号 — 错误条款号在 GMP 审计比无依据更糟）。
"""
import pytest


class TestGmpBasisMap:
    """GMP_BASIS_MAP 覆盖面与内容 sanity。"""

    def test_all_builtin_rule_types_covered(self):
        """全部内置规则产出 type（15 条）与 LLM 常见 type 都有映射。"""
        from core.rules.gmp_basis import GMP_BASIS_MAP
        builtin = {
            "time_reversal", "year_contradiction", "suspicious_date",
            "signature_time_anomaly", "completeness", "batch_inconsistency",
            "param_out_of_spec", "low_confidence", "handwritten",
            "signature_mismatch", "time_anomaly", "step_gap",
        }
        assert builtin <= set(GMP_BASIS_MAP.keys())

    def test_basis_text_cites_norm_names(self):
        """依据文本须引用规范名（GMP 2010 / ALCOA+ / EU GMP 之一）。"""
        from core.rules.gmp_basis import GMP_BASIS_MAP
        for ftype, basis in GMP_BASIS_MAP.items():
            assert any(
                kw in basis for kw in ("GMP", "ALCOA", "偏差管理", "记录填写规范")
            ), f"{ftype} 依据未引用规范名: {basis}"

    def test_unmapped_types_are_intentional(self):
        """不映射集合明确（技术噪音/用户规则）且不在映射表里。"""
        from core.rules.gmp_basis import GMP_BASIS_MAP, _UNMAPPED
        assert _UNMAPPED.isdisjoint(GMP_BASIS_MAP.keys())


class TestAttachGmpBasis:
    """attach_gmp_basis 行为：映射、幂等、防御、不映射。"""

    def test_attaches_for_mapped_type(self):
        from core.rules.gmp_basis import attach_gmp_basis
        findings = [{"type": "batch_inconsistency", "severity": "critical"}]
        attach_gmp_basis(findings)
        assert findings[0]["gmp_basis"]
        assert "ALCOA+" in findings[0]["gmp_basis"]

    def test_no_key_for_unmapped(self):
        from core.rules.gmp_basis import attach_gmp_basis
        findings = [{"type": "ocr_noise"}, {"type": "user_rule"},
                    {"type": "unknown_future_type"}]
        attach_gmp_basis(findings)
        assert "gmp_basis" not in findings[0]
        assert "gmp_basis" not in findings[1]
        assert "gmp_basis" not in findings[2]

    def test_idempotent_keeps_existing(self):
        """已有非空 gmp_basis 不覆盖（保留 LLM 更精确引用）。"""
        from core.rules.gmp_basis import attach_gmp_basis
        findings = [{"type": "completeness", "gmp_basis": "《XX规范》第1条"}]
        attach_gmp_basis(findings)
        assert findings[0]["gmp_basis"] == "《XX规范》第1条"

    def test_non_dict_entries_survive(self):
        """非 dict 元素（脏数据）不炸。"""
        from core.rules.gmp_basis import attach_gmp_basis
        findings = [{"type": "step_gap"}, None, "garbage"]
        attach_gmp_basis(findings)
        assert findings[0]["gmp_basis"]
        assert findings[1] is None


class TestGmpBasisMigration:
    """schema v7：findings.gmp_basis 列迁移（v6 库 → v7）。"""

    @pytest.mark.asyncio
    async def test_v6_db_gets_gmp_basis_column(self, tmp_path):
        """v6 老库（无 gmp_basis 列）→ migrate 后有列、版本号 = 7。"""
        import aiosqlite
        from db.client import migrate

        db_path = str(tmp_path / "v6.db")
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            # 构造 v6 形态 findings 表（无 gmp_basis）
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
                    user_rule_id TEXT
                )
            """)
            await db.execute("PRAGMA user_version = 6")
            await db.commit()

            await migrate(db)

            cursor = await db.execute("PRAGMA table_info(findings)")
            cols = {row["name"] for row in await cursor.fetchall()}
            assert "gmp_basis" in cols
            cursor = await db.execute("PRAGMA user_version")
            assert (await cursor.fetchone())[0] >= 7

            # 迁移后 INSERT 带 gmp_basis 可用（列真实可写）
            await db.execute(
                "INSERT INTO findings (job_id, page, type, severity, "
                "description, gmp_basis) VALUES ('j', 1, 'completeness', "
                "'warning', 'x', 'GMP 2010')"
            )
            await db.commit()
            cursor = await db.execute("SELECT gmp_basis FROM findings")
            assert (await cursor.fetchone())[0] == "GMP 2010"

    @pytest.mark.asyncio
    async def test_migration_idempotent(self, tmp_path):
        """v7 已迁移库再跑 migrate 不报错、不重复加列。"""
        import aiosqlite
        from db.client import init_schema, migrate

        db_path = str(tmp_path / "v7.db")
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            await init_schema(db)
            await migrate(db)
            await migrate(db)  # 幂等重跑
            cursor = await db.execute(
                "SELECT COUNT(*) FROM pragma_table_info('findings') "
                "WHERE name='gmp_basis'"
            )
            assert (await cursor.fetchone())[0] == 1
