# -*- coding: utf-8 -*-
"""降噪 N2 回归：v4 completeness 提示收紧（v3 对照不变）。"""
import sys

sys.path.insert(0, ".")
from core.page_analyzer import PROMPTS  # noqa: E402


def test_v4_contains_completeness_spec():
    us = PROMPTS["v4"]["user_suffix"]
    assert "[完整性检查规范]" in us
    assert "禁止" in us
    assert "completeness_notes" in us  # schema 占位锚定输出位置


def test_v3_untouched():
    us = PROMPTS["v3"]["user_suffix"]
    assert "[完整性检查规范]" not in us
    assert "completeness_notes" not in us
