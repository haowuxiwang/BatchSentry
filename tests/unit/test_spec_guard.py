"""LLM 自报 param_out_of_spec 的规则层复核（M8/P0）单测。

数据取自 M8 真实 51 页 e2e（job a5284ca3-47b）p14 的原始结构化载荷，用于锁定
"OCR 丢 ± → LLM 成片误报"这一缺陷不再复发。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.rules.spec_guard import (  # noqa: E402
    _triple_state,
    drop_unfounded_spec_findings,
    index_specs,
)


def _p14_structured() -> dict:
    return {
        "steps": [
            {
                "parameters": [
                    {"name": "缓冲液 pH", "spec_range": "7.5 0.2", "value": "7.49/A"},
                    {"name": "浓缩结束温度", "spec_range": "40 3°C", "value": "42.1 °C"},
                    {"name": "浓缩前热水温度", "spec_range": "70 5°C", "value": "72.5 °C"},
                    {"name": "浓缩结束真空度", "spec_range": "≤0.08MPa", "value": "-0.090 MPa"},
                ],
                "measurements": [
                    {
                        "time": "2025年01月21日 06:01",
                        "values": {
                            "温度 ( 40 3°C )": {"spec": "40 3°C", "actual": "42.1"},
                        },
                    },
                    {
                        "time": "2025年01月21日 07:01",
                        "values": {
                            "温度 ( 40 3°C )": {"spec": "40 3°C", "actual": "38.4"},
                        },
                    },
                ],
            }
        ]
    }


class TestTripleState:
    def test_in_spec(self):
        assert _triple_state("40 3°C", "42.1 °C") == "in"

    def test_out_of_spec(self):
        assert _triple_state("0~5°C", "45.6") == "out"

    def test_unknown_spec(self):
        assert _triple_state("规格见附件", "42.1") == "unknown"

    def test_actual_with_note_suffix(self):
        """实测值夹带备注（"7.49/A"）仍可取首个数字判定。"""
        assert _triple_state("7.5 0.2", "7.49/A") == "in"

    def test_vacuum_sign_convention_is_unknown(self):
        """负表压 vs 印版正限值 → 符号约定存疑，不得据此剔除 LLM 结论。"""
        assert _triple_state("≤0.08MPa", "-0.090 MPa") == "unknown"


class TestIndexSpecs:
    def test_both_raw_and_base_column_names(self):
        idx = index_specs(_p14_structured())
        names = {n for n, _, _ in idx}
        assert "温度 ( 40 3°C )" in names
        assert "温度" in names  # 去括号基名


class TestDropUnfoundedSpecFindings:
    def test_drops_llm_false_positives_from_p14(self):
        """真实 p14：5 条由"±丢成空格"导致的 LLM 误报须被剔除。"""
        findings = [
            {"type": "param_out_of_spec", "description": "缓冲液 pH 7.49/A 超出规格范围 7.5 0.2",
             "ocr_text": "缓冲液 pH 7.49/A"},
            {"type": "param_out_of_spec", "description": "浓缩结束温度 42.1 °C 超出规格范围 40 3°C",
             "ocr_text": "浓缩结束温度 42.1 °C"},
            {"type": "param_out_of_spec", "description": "保温记录 07:01 温度 38.4 °C 超出规格范围 40 3°C",
             "ocr_text": "07:01 38.4"},
            {"type": "param_out_of_spec", "description": "浓缩前热水温度 72.5 °C 超出规格范围 70 5°C",
             "ocr_text": "浓缩前热水温度 72.5 °C"},
        ]
        kept, dropped = drop_unfounded_spec_findings(findings, _p14_structured())
        assert dropped == 4
        assert kept == []

    def test_keeps_genuine_out_of_spec(self):
        """真超差（45.6 vs 0~5°C）必须保留——规则层独立复核不得吞掉真实偏差。"""
        structured = {
            "steps": [{"measurements": [
                {"time": "12:10", "values": {"温度（0~5°C）": {"spec": "0~5°C", "actual": "45.6"}}},
            ]}]
        }
        findings = [{"type": "param_out_of_spec", "description": "温度（0~5°C）在 12:10 时实测 45.6°C 超出规格",
                     "ocr_text": "12:10 45.6"}]
        kept, dropped = drop_unfounded_spec_findings(findings, structured)
        assert dropped == 0 and len(kept) == 1

    def test_keeps_when_no_structured_match(self):
        """定位不到结构化字段 → fail-closed，保留。"""
        findings = [{"type": "param_out_of_spec", "description": "某某参数超差", "ocr_text": "xx"}]
        kept, dropped = drop_unfounded_spec_findings(findings, _p14_structured())
        assert dropped == 0 and kept == findings

    def test_keeps_when_spec_unparseable(self):
        structured = {"steps": [{"parameters": [
            {"name": "效价", "spec_range": "见附件", "value": "偏低"},
        ]}]}
        findings = [{"type": "param_out_of_spec", "description": "效价 偏低 超出规格 见附件",
                     "ocr_text": "效价"}]
        kept, dropped = drop_unfounded_spec_findings(findings, structured)
        assert dropped == 0 and len(kept) == 1

    def test_non_spec_types_untouched(self):
        findings = [
            {"type": "completeness", "description": "缺项 温度 40 3°C", "ocr_text": ""},
            {"type": "handwritten", "description": "手写 温度", "ocr_text": ""},
        ]
        kept, dropped = drop_unfounded_spec_findings(findings, _p14_structured())
        assert dropped == 0 and kept == findings

    def test_raw_type_alias_also_guarded(self):
        findings = [{"type": "param_out_of_range", "description": "浓缩结束温度 42.1 °C 超出规格 40 3°C",
                     "ocr_text": "浓缩结束温度 42.1 °C"}]
        kept, dropped = drop_unfounded_spec_findings(findings, _p14_structured())
        assert dropped == 1 and kept == []

    def test_non_dict_entries_passed_through(self):
        findings = ["junk", {"type": "param_out_of_spec", "description": "浓缩结束温度 42.1 °C 超出规格 40 3°C",
                             "ocr_text": "浓缩结束温度 42.1 °C"}]
        kept, dropped = drop_unfounded_spec_findings(findings, _p14_structured())
        assert dropped == 1 and kept == ["junk"]
