"""LLM 自报 param_out_of_spec 的规则层复核（M8/P0）+ 抑制留痕契约（P0-2）单测。

数据取自 M8 真实 51 页 e2e（job a5284ca3-47b）p14 的原始结构化载荷，用于锁定
"OCR 丢 ± → LLM 成片误报"这一缺陷不再复发。

P0-2：抑制 **≠** 删除 —— 被抑制的每一条都必须带非空理由与可抽检证据
（EU GMP Annex 11 §16 / 中国附录《计算机化系统》第 15/16 条）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.rules.spec_guard import (  # noqa: E402
    SUPPRESSION_INSERT_SQL,
    _triple_state,
    drop_unfounded_spec_findings,
    index_specs,
    suppression_rows,
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

    def test_decimal_loss_is_soft(self):
        """真实 p08：4.6 被读成 46 → 规则层已以 info 呈现，标 soft 供去重。"""
        assert _triple_state("3.0-5.0bar", "46") == "soft"

    def test_unparseable_actual_is_unknown(self):
        assert _triple_state("1-2", "") == "unknown"


class TestIndexSpecs:
    def test_both_raw_and_base_column_names(self):
        idx = index_specs(_p14_structured())
        names = {n for n, _, _ in idx}
        assert "温度 ( 40 3°C )" in names
        assert "温度" in names  # 去括号基名

    def test_skips_empty_pairs(self):
        """spec 与 actual 皆空 → 不产出三元组。"""
        structured = {"steps": [{"parameters": [
            {"name": "盐酸批号", "spec_range": "", "value": ""},
            {"name": "有效项", "spec_range": "1-2", "value": "1.5"},
        ]}]}
        idx = index_specs(structured)
        assert all(n != "盐酸批号" for n, _, _ in idx)
        assert any(n == "有效项" for n, _, _ in idx)

    def test_tolerates_malformed_containers(self):
        """非 dict 的 step / measurement / 单元格须跳过而非抛错。"""
        structured = {"steps": [
            "junk",
            {"parameters": ["nope", {"name": "P", "spec_range": "1-2", "value": "1.5"}],
             "measurements": ["nope", {"time": "t", "values": {"列": "不是dict"}}]},
        ]}
        idx = index_specs(structured)
        assert ("P", "1-2", "1.5") in idx
        assert all(n != "列" for n, _, _ in idx)


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
        kept, suppressed = drop_unfounded_spec_findings(findings, _p14_structured())
        assert len(suppressed) == 4
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
        kept, suppressed = drop_unfounded_spec_findings(findings, structured)
        assert len(suppressed) == 0 and len(kept) == 1

    def test_keeps_when_no_structured_match(self):
        """定位不到结构化字段 → fail-closed，保留。"""
        findings = [{"type": "param_out_of_spec", "description": "某某参数超差", "ocr_text": "xx"}]
        kept, suppressed = drop_unfounded_spec_findings(findings, _p14_structured())
        assert len(suppressed) == 0 and kept == findings

    def test_keeps_when_spec_unparseable(self):
        structured = {"steps": [{"parameters": [
            {"name": "效价", "spec_range": "见附件", "value": "偏低"},
        ]}]}
        findings = [{"type": "param_out_of_spec", "description": "效价 偏低 超出规格 见附件",
                     "ocr_text": "效价"}]
        kept, suppressed = drop_unfounded_spec_findings(findings, structured)
        assert len(suppressed) == 0 and len(kept) == 1

    def test_non_spec_types_untouched(self):
        findings = [
            {"type": "completeness", "description": "缺项 温度 40 3°C", "ocr_text": ""},
            {"type": "handwritten", "description": "手写 温度", "ocr_text": ""},
        ]
        kept, suppressed = drop_unfounded_spec_findings(findings, _p14_structured())
        assert len(suppressed) == 0 and kept == findings

    def test_raw_type_alias_also_guarded(self):
        findings = [{"type": "param_out_of_range", "description": "浓缩结束温度 42.1 °C 超出规格 40 3°C",
                     "ocr_text": "浓缩结束温度 42.1 °C"}]
        kept, suppressed = drop_unfounded_spec_findings(findings, _p14_structured())
        assert len(suppressed) == 1 and kept == []

    def test_non_dict_entries_passed_through(self):
        findings = ["junk", {"type": "param_out_of_spec", "description": "浓缩结束温度 42.1 °C 超出规格 40 3°C",
                             "ocr_text": "浓缩结束温度 42.1 °C"}]
        kept, suppressed = drop_unfounded_spec_findings(findings, _p14_structured())
        assert len(suppressed) == 1 and kept == ["junk"]

    def test_drops_llm_duplicate_of_decimal_loss_with_inconsistent_severity(self):
        """真实 p08：LLM 自报 critical「进料压力多次超出规格范围」与规则层
        info+hint 的同一三元组重复且口径不一 → 剔除 LLM 重复条目。"""
        structured = {"steps": [{"parameters": [
            {"name": "进料压力", "spec_range": "3.0-5.0bar", "value": "46"},
        ]}]}
        findings = [{"type": "param_out_of_spec", "severity": "critical",
                     "description": "进料压力多次超出规格范围", "ocr_text": "进料压力 46"}]
        kept, suppressed = drop_unfounded_spec_findings(findings, structured)
        assert len(suppressed) == 1 and kept == []

    def test_drops_llm_false_positives_across_name_separator_styles(self):
        """真实 p9：结构化列名 "T2101a_压力" vs 文案 "T2101a 压力"，
        且 LLM 把 "<0.3MPa" 当成了下限（0.16 本应合规）。分隔符归一后须剔除。"""
        structured = {"steps": [{"measurements": [
            {"time": "10:00", "values": {
                "T2101a_压力": {"spec": "<0.3MPa", "actual": "0.16"},
                "T2101b_压力": {"spec": "<0.3MPa", "actual": "0.17"},
            }},
        ]}]}
        findings = [
            {"type": "param_out_of_spec", "severity": "critical",
             "description": "T2101a 压力 0.16 MPa 超出规格范围 <0.3 MPa",
             "ocr_text": "T2101a 压力 0.16 MPa"},
            {"type": "param_out_of_spec", "severity": "critical",
             "description": "T2101b 压力 0.17 MPa 超出规格范围 <0.3 MPa",
             "ocr_text": "T2101b 压力 0.17 MPa"},
        ]
        kept, suppressed = drop_unfounded_spec_findings(findings, structured)
        assert len(suppressed) == 2 and kept == []


class TestSuppressionLedgerContract:
    """P0-2：抑制必须留痕 —— 明细必须带非空理由与可抽检的证据。"""

    def test_suppressed_details_carry_reason_and_evidence(self):
        kept, suppressed = drop_unfounded_spec_findings(
            [
                {"type": "param_out_of_spec",
                 "description": "浓缩结束温度 42.1 °C 超出规格范围 40 3°C",
                 "ocr_text": "浓缩结束温度 42.1 °C"},
            ],
            _p14_structured(),
        )
        assert kept == [] and len(suppressed) == 1
        item = suppressed[0]
        assert item["reason"].strip(), "抑制理由不得为空（法规留痕硬要求）"
        # 理由必须自证：含命中的参数名/实测/规格，可被人工抽检复核
        assert "浓缩结束温度" in item["reason"]
        assert "42.1" in item["reason"]
        ev = item["evidence"]
        assert ev["states"] == ["in"]
        assert ev["matched"][0]["name"] == "浓缩结束温度"
        assert ev["matched"][0]["state"] == "in"
        # 原始 finding 必须随明细带走（台账要能还原"被抑制的是什么"）
        assert item["finding"]["type"] == "param_out_of_spec"

    def test_suppression_rows_shape_and_blank_reason_rejected(self):
        _, suppressed = drop_unfounded_spec_findings(
            [
                {"type": "param_out_of_spec", "severity": "critical",
                 "description": "缓冲液 pH 7.49/A 超出规格范围 7.5 0.2",
                 "ocr_text": "缓冲液 pH 7.49/A"},
            ],
            _p14_structured(),
        )
        rows = suppression_rows("job-1", 14, suppressed)
        assert len(rows) == 1
        job_id, page, ftype, sev, desc, ocr, source, reason, evidence = rows[0]
        assert (job_id, page, ftype, sev, source) == (
            "job-1", 14, "param_out_of_spec", "critical", "llm_page")
        assert desc and ocr and reason.strip()
        assert '"states"' in evidence  # evidence 必须是 JSON 文本
        # 不变式：理由为空 → 显式失败，绝不落库"无理由的抑制"
        with pytest.raises(ValueError):
            suppression_rows("job-1", 14, [{"finding": {"type": "x"}, "reason": "   "}])

    def test_insert_sql_targets_ledger_table_with_reason(self):
        """落库语句必须指向台账表且包含 reason/evidence 列（防止漂移回计数口径）。"""
        assert "finding_suppressions" in SUPPRESSION_INSERT_SQL
        for col in ("reason", "evidence", "job_id", "page", "type", "description"):
            assert col in SUPPRESSION_INSERT_SQL, col
