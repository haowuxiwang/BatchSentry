"""core/finding_quality.py 单测（M2/T2.4 + T2.6）。

覆盖：类型白名单归一（规范/别名/中文/关键词/未知）、置信度评分（各来源与
页标记、上下界钳制）、页完整性判据，以及"规范类型必有中文映射"不变式。
"""
from __future__ import annotations

import pytest

from core import finding_quality as fq
from core.zh_map import FINDING_TYPE_ZH, zh_finding_type


class TestNormalizeFindingType:
    @pytest.mark.parametrize("t", fq.CANONICAL_TYPES)
    def test_canonical_passthrough(self, t):
        assert fq.normalize_finding_type(t) == t

    def test_alias_mapping(self):
        assert fq.normalize_finding_type("time_anomaly") == "signature_time_anomaly"
        assert fq.normalize_finding_type("param_out_of_range") == "param_out_of_spec"
        assert fq.normalize_finding_type("missing_step") == "step_gap"

    def test_chinese_alias(self):
        assert fq.normalize_finding_type("时间倒序") == "time_reversal"
        assert fq.normalize_finding_type("批号不一致") == "batch_inconsistency"

    def test_keyword_fallback(self):
        assert fq.normalize_finding_type("signature_date_conflict") == \
            "signature_time_anomaly"
        assert fq.normalize_finding_type("batch_no_mismatch") == "batch_inconsistency"
        assert fq.normalize_finding_type("ocr_low_quality") == "ocr_noise"

    def test_case_and_whitespace_insensitive(self):
        assert fq.normalize_finding_type("  COMPLETENESS  ") == "completeness"

    @pytest.mark.parametrize("raw", [None, "", "   ", "totally_unknown_xyz"])
    def test_unmappable_goes_uncategorized(self, raw):
        assert fq.normalize_finding_type(raw) == fq.UNCATEGORIZED

    def test_is_canonical(self):
        assert fq.is_canonical("completeness")
        assert not fq.is_canonical("totally_unknown_xyz")
        assert not fq.is_canonical(None)


class TestConfidenceFor:
    def test_rule_base(self):
        assert fq.confidence_for({"source": "rule"}) == 0.85

    def test_missing_source_defaults_to_rule(self):
        assert fq.confidence_for({}) == 0.85

    def test_llm_page_penalty(self):
        assert fq.confidence_for({"source": "llm_page"}) == 0.75

    @pytest.mark.parametrize("src", ["llm_cross", "llm_fallback", "user_rule"])
    def test_llm_generated_penalty(self, src):
        assert fq.confidence_for({"source": src}) == 0.70

    def test_page_flag_penalty(self):
        assert fq.confidence_for({"source": "rule"}, page_flagged=True) == 0.65

    def test_clamped_at_min(self):
        # 0.85 - 0.15(gen) - 0.20(flag) = 0.50；再压也不会低于 0.30
        assert fq.confidence_for({"source": "llm_cross"}, page_flagged=True) == 0.50
        assert fq._CONF_MIN == 0.30

    def test_clamped_at_max(self):
        assert fq.confidence_for({"source": "rule"}, page_flagged=False) <= fq._CONF_MAX

    def test_rounding_two_decimals(self):
        v = fq.confidence_for({"source": "llm_page"}, page_flagged=True)
        assert v == 0.55


class TestPageIsFlagged:
    @pytest.mark.parametrize("key", fq.PAGE_FLAG_KEYS)
    def test_each_flag_key(self, key):
        assert fq.page_is_flagged({key: True})

    def test_no_flags(self):
        assert not fq.page_is_flagged({"page_number": 1, "findings": []})

    @pytest.mark.parametrize("bad", [None, "str", 123, []])
    def test_non_dict_is_false(self, bad):
        assert not fq.page_is_flagged(bad)

    def test_falsy_flag_value_not_flagged(self):
        assert not fq.page_is_flagged({"_ocr_warning": ""})


class TestCanonicalTypeZhInvariant:
    def test_every_canonical_type_has_zh(self):
        """规范类型必须都有中文映射 —— 否则前端会回落成英文原文。"""
        missing = [t for t in fq.CANONICAL_TYPES if t not in FINDING_TYPE_ZH]
        assert missing == []

    def test_zh_lookup_no_english_fallback_for_canonical(self):
        for t in fq.CANONICAL_TYPES:
            assert zh_finding_type(t) != t
