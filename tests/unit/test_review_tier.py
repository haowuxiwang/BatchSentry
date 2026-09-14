"""T6.4：三色复核分级的单一来源与跨文件不变式。

背景（为什么要机检）
--------------------
"规则=红 / LLM 辅助=蓝 / 系统校验通过=绿"的映射若在 Python、Jinja 模板、
review.js、review.css 各存一份，任何一处新增来源都会静默漂移 —— 这与
`type_zh` 历史上踩的坑同源（见 tests/unit/test_type_sync.py）。故：
  1. 分级映射**只在** `core/finding_quality.REVIEW_TIER_BY_SOURCE` 定义；
  2. 服务端把 `tier` 挂到每条 finding 上（SSR 与 AJAX 共用同一函数）；
  3. 前端只读 `f.tier`，不得自带映射表；
  4. CSS 类名 `tier-<tier>` 与 Python 的分级取值必须逐键一致。
"""
from __future__ import annotations

import re
from pathlib import Path

from core.finding_quality import (
    FINDING_TIERS,
    REVIEW_TIER_BY_SOURCE,
    REVIEW_TIERS,
    REVIEW_TIER_ZH,
    attach_review_tier,
    review_tier,
    tier_counts,
)

REPO = Path(__file__).resolve().parents[2]
REVIEW_HTML = REPO / "templates" / "review.html"
REVIEW_JS = REPO / "static" / "review.js"
REVIEW_CSS = REPO / "static" / "review.css"


class TestReviewTierMapping:
    def test_known_sources_map_to_expected_tier(self):
        assert review_tier("rule") == "rule"
        assert review_tier("user_rule") == "rule"
        assert review_tier("llm_page") == "llm"
        assert review_tier("llm_fallback") == "llm"
        assert review_tier("llm_cross") == "llm"

    def test_case_and_whitespace_insensitive(self):
        assert review_tier(" RULE ") == "rule"
        assert review_tier("LLM_Cross") == "llm"

    def test_missing_source_defaults_to_rule(self):
        """缺省来源 = 规则：与写入路径一致（stage3 `f.get("source", "rule")`
        与置信度评分 `or "rule"`）。"""
        assert review_tier(None) == "rule"
        assert review_tier("") == "rule"

    def test_unknown_source_falls_back_to_llm(self):
        """未知来源一律降级为 LLM 辅助 —— 绝不冒充确定性判据（会让复核者放松警惕）。"""
        assert review_tier("llm_new_thing") == "llm"
        assert review_tier("mystery") == "llm"

    def test_all_tiers_have_chinese_label(self):
        for t in REVIEW_TIERS:
            assert REVIEW_TIER_ZH.get(t), f"分级 {t} 缺中文名"

    def test_finding_tiers_derive_from_source_map(self):
        assert FINDING_TIERS == ("rule", "llm")
        assert set(FINDING_TIERS) <= set(REVIEW_TIERS)
        assert "pass" not in FINDING_TIERS, "pass 是覆盖面概念，不来自 finding"


class TestAttachAndCount:
    def test_attach_then_count(self):
        findings = [
            {"source": "rule"},
            {"source": "llm_cross"},
            {"source": None},
            {"source": "user_rule"},
        ]
        assert attach_review_tier(findings) is findings  # 就地返回同一列表
        assert [f["tier"] for f in findings] == ["rule", "llm", "rule", "rule"]
        assert tier_counts(findings) == {"rule": 3, "llm": 1}

    def test_count_tolerates_missing_tier(self):
        assert tier_counts([{"source": "llm_page"}]) == {"rule": 0, "llm": 1}

    def test_attach_is_non_dict_safe(self):
        findings = ["not-a-dict", {"source": "rule"}]
        attach_review_tier(findings)
        assert findings[1]["tier"] == "rule"
        assert tier_counts(findings) == {"rule": 1, "llm": 0}

    def test_count_returns_both_keys_even_when_empty(self):
        assert tier_counts([]) == {"rule": 0, "llm": 0}


class TestRuleCoverage:
    """系统校验通过（绿）—— 与命中在同一可陈述单元（type）上对齐。"""

    def test_passed_and_fired_partition_all_types(self):
        """命中 ∪ 通过 = 全部类型，且两者不相交（计数条数字必须自洽）。"""
        from core.rules.registry import rule_coverage

        cov = rule_coverage({"completeness": 3, "alteration": 1})
        fired_types = {e["type"] for e in cov["fired"]}
        passed_types = {e["type"] for e in cov["passed"]}
        assert fired_types and passed_types
        assert not (fired_types & passed_types), "同一类型同时出现在命中与通过"
        assert cov["fired_count"] == len(fired_types)
        assert cov["passed_count"] == len(passed_types)
        assert cov["type_total"] == len(fired_types | passed_types)

    def test_fired_types_appear_with_counts(self):
        from core.rules.registry import rule_coverage

        cov = rule_coverage({"completeness": 3})
        fired = {e["type"]: e["count"] for e in cov["fired"]}
        assert fired.get("completeness") == 3
        passed_types = {e["type"] for e in cov["passed"]}
        assert "completeness" not in passed_types
        # 同一 type 的多条规则一并列出（便于追溯）
        comp = next(e for e in cov["fired"] if e["type"] == "completeness")
        assert set(comp["rule_ids"]) >= {"R6", "R8b"}

    def test_clean_job_all_types_pass(self):
        """零命中（洁净批次）→ 全部类型通过，这正是"绿"要表达的价值。"""
        from core.rules.registry import rule_coverage

        cov = rule_coverage({})
        assert cov["fired_count"] == 0
        assert cov["passed_count"] == cov["type_total"]
        assert cov["passed_count"] > 0

    def test_rule_total_matches_enabled_specs(self):
        from core.rules.registry import enabled_rule_specs, rule_coverage

        cov = rule_coverage({})
        assert cov["rule_total"] == len(enabled_rule_specs())

    def test_entries_carry_description_and_basis(self):
        from core.rules.registry import rule_coverage

        cov = rule_coverage({})
        for e in cov["passed"]:
            assert e["description"], f"{e['type']} 缺描述"
            assert e["basis"], f"{e['type']} 缺 GMP 依据"
            assert e["rule_ids"], f"{e['type']} 无对应规则 id"

    def test_deterministic_ordering(self):
        """输出按 type 排序 —— 前端展示与断言都依赖稳定顺序。"""
        from core.rules.registry import rule_coverage

        cov = rule_coverage({"alteration": 1, "completeness": 2})
        assert [e["type"] for e in cov["fired"]] == sorted(e["type"] for e in cov["fired"])
        assert [e["type"] for e in cov["passed"]] == sorted(e["type"] for e in cov["passed"])


class TestNoDriftAcrossArtifacts:
    """跨文件不变式：映射只在 Python 一处，前端只读 tier。"""

    def _all_source_literals(self) -> dict[str, list[str]]:
        """扫描 finding 产出点写下的 source 字面量。

        排除 core/mineru_client.py —— 那里的 `"source": "mineru"` 是 OCR
        块元数据统计，不是 finding 来源。
        """
        targets = [REPO / "core" / "rules",
                   REPO / "core" / "pipeline",
                   REPO / "core" / "finding_noise.py"]
        found: dict[str, list[str]] = {}
        for t in targets:
            files = [t] if t.is_file() else sorted(t.rglob("*.py"))
            for f in files:
                if f.name == "mineru_client.py":
                    continue
                for m in re.finditer(r'"source":\s*"([a-zA-Z_]+)"',
                                     f.read_text(encoding="utf-8")):
                    found.setdefault(m.group(1), []).append(f.name)
        return found

    def test_every_produced_source_has_a_tier(self):
        literals = self._all_source_literals()
        assert literals, "未扫描到任何 source 字面量 —— 扫描路径可能已失效"
        unknown = sorted(set(literals) - set(REVIEW_TIER_BY_SOURCE))
        assert not unknown, (
            f"产出端写下了未登记分级的 source：{unknown}（各自出现于 "
            f"{ {k: literals[k] for k in unknown} }）→ 请补 REVIEW_TIER_BY_SOURCE")

    def test_template_reads_server_tier(self):
        html = REVIEW_HTML.read_text(encoding="utf-8")
        assert "f.tier" in html, "模板未使用服务端 tier"
        assert "tier-bar" in html and "系统校验通过" in html, "模板缺三色计数条"
        assert "rule_coverage" in html, "模板缺规则覆盖面（绿）面板"
        # 模板不得自带 source→tier 映射表
        assert "tier_by_source" not in html and "TIER_BY_SOURCE" not in html

    def test_js_reads_server_tier_without_own_map(self):
        js = REVIEW_JS.read_text(encoding="utf-8")
        assert "f.tier" in js, "review.js 未使用服务端 tier"
        assert "tier-" in js, "review.js 未输出 tier 类名"
        # 不得自己再写一份 source→tier 映射（漂移源）
        assert not re.search(r"tierBySource|TIER_BY_SOURCE|tierOf\s*\(", js), \
            "review.js 自持分级映射 → 与后端必然漂移"

    def test_css_defines_every_tier_class(self):
        css = REVIEW_CSS.read_text(encoding="utf-8")
        for t in REVIEW_TIERS:
            assert f".tier-dot-{t}" in css, f"review.css 缺 .tier-dot-{t}"
        for t in FINDING_TIERS:
            assert f".finding-card.tier-{t}" in css, f"review.css 缺卡片色条 .tier-{t}"

    def test_tier_zh_keys_match_tiers(self):
        assert set(REVIEW_TIER_ZH) == set(REVIEW_TIERS)
