"""规则注册表不变式（M4/T4.0）—— 让"规则集合"本身可机检。

注册表是规则元数据的单一来源；本组测试锁定其结构约束与配置开关行为，
避免日后新增/调整规则时出现"id 重复 / type 不规范 / 缺依据 / 依赖错序"。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.finding_quality import CANONICAL_TYPES
from core.rules import registry
from core.rules.registry import (
    RULE_REGISTRY,
    enabled_rule_specs,
    rule_ids,
)


class TestRegistryStructure:
    def test_ids_unique(self):
        ids = rule_ids()
        assert len(ids) == len(set(ids)), f"规则 id 重复：{ids}"

    def test_all_types_canonical(self):
        """每个规则的 type 必须在规范白名单内（否则前端/统计会漂移）。"""
        bad = [s.id for s in RULE_REGISTRY if s.type not in CANONICAL_TYPES]
        assert bad == [], f"非规范 type 的规则：{bad}"

    def test_every_rule_has_basis(self):
        """每条规则必须有 GMP 依据（basis 取自 GMP_BASIS_MAP）。"""
        missing = [s.id for s in RULE_REGISTRY if not s.basis.strip()]
        assert missing == [], f"缺 GMP 依据的规则：{missing}"

    def test_every_rule_has_description_and_severity(self):
        for s in RULE_REGISTRY:
            assert s.description.strip(), f"{s.id} 缺 description"
            assert s.severity in ("critical", "warning", "info"), s.id

    def test_check_callable(self):
        for s in RULE_REGISTRY:
            assert callable(s.check), f"{s.id} check 不可调用"

    def test_r3_precedes_r16_dependency(self):
        """R16（偏差关联）依赖 R3 先算出越界页 —— 顺序不得颠倒。"""
        ids = rule_ids()
        assert ids.index("R3") < ids.index("R16")

    def test_m4_rules_registered(self):
        ids = set(rule_ids())
        assert {"R11", "R12", "R13", "R14", "R15", "R16", "R17"} <= ids


class TestEnabledRuleSpecs:
    def test_all_enabled_by_default(self):
        assert len(enabled_rule_specs()) == len(RULE_REGISTRY)

    def test_disabled_ids_excluded(self):
        with patch.object(registry, "disabled_rule_ids", return_value={"R12", "R15"}):
            specs = enabled_rule_specs()
        ids = [s.id for s in specs]
        assert "R12" not in ids and "R15" not in ids
        assert len(specs) == len(RULE_REGISTRY) - 2

    def test_config_read_failure_keeps_all_enabled(self):
        """config 读取失败 → 全部启用（合规检查宁可多报不可漏检）。"""
        with patch("config.load_disabled_rule_ids", side_effect=OSError("boom")):
            assert len(registry.enabled_rule_specs()) == len(RULE_REGISTRY)


class TestRegistryDrivesAnalysis:
    @pytest.mark.asyncio
    async def test_only_enabled_specs_run(self):
        """注册表驱动：把规则集收窄为单条 → 只产出该规则的 type。"""
        from core.rules import analyze_cross_page
        from unittest.mock import AsyncMock

        only_r12 = [s for s in RULE_REGISTRY if s.id == "R12"]
        pages = [{"page": 1, "data": {
            "page_info": {"page_number": 1},
            "steps": [{"step_no": 1, "operation": "工序",
                       "operator": "甲", "reviewer": "甲"}],
            "findings": [],
        }}]
        import core.rules as cr
        with patch.object(cr, "enabled_rule_specs", return_value=only_r12), \
                patch("core.rules._llm_fallback_check", new=AsyncMock(return_value=[])), \
                patch("core.rules._llm_based_check", new=AsyncMock(return_value=[])):
            findings = await analyze_cross_page(pages, job_id="t")
        types = {f["type"] for f in findings}
        assert types == {"self_review"}
