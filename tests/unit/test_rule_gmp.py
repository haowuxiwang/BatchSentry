"""R11–R17 单测（M4/T4.8）—— 每条规则正例/反例/边界/畸形 ≥4 例。

规则函数是纯函数（吃 `_normalize_pages` 输出的 pages），故离线、零 LLM、
逐位可复现。断言聚焦"是否触发 + type + severity"，描述文本只做关键子串校验。
"""
from __future__ import annotations

import pytest

from core.rules.base import _normalize_pages
from core.rules.rule_gmp import (
    _check_alteration,
    _check_deviation_link,
    _check_doc_version,
    _check_env_monitor,
    _check_equipment_state,
    _check_mass_balance,
    _check_self_review,
)


def _norm(pages: list[dict]) -> list[dict]:
    return _normalize_pages(pages)


def _page(n: int, steps: list[dict], page_info: dict | None = None) -> dict:
    return {"page": n, "data": {
        "page_info": {"page_number": n, **(page_info or {})},
        "steps": steps,
        "findings": [],
    }}


def _step(no=1, op="工序", **extra) -> dict:
    s = {"no": no, "step_no": no, "operation": op}
    s.update(extra)
    return s


def _types(findings: list[dict]) -> list[str]:
    return [f["type"] for f in findings]


# ===========================================================================
# R11 mass_balance
# ===========================================================================


class TestMassBalance:
    def test_declared_spec_but_no_value_warns(self):
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "浓缩滤液收率", "spec_range": "≥50%", "value": ""},
        ])])])
        fs = _check_mass_balance(pages)
        assert _types(fs) == ["mass_balance"]
        assert fs[0]["severity"] == "warning"
        assert "未记录实测值" in fs[0]["description"]

    def test_text_declares_yield_without_param_warns(self):
        pages = _norm([_page(1, [_step(
            1, "浓缩滤液收率应≥50%，其计算公式如下：收率(%)=A÷B×100%")])])
        fs = _check_mass_balance(pages)
        assert _types(fs) == ["mass_balance"]
        assert fs[0]["severity"] == "warning"

    def test_value_and_spec_present_no_finding(self):
        """spec+value 都在 → 交给 R3 判越界，R11 不重复。"""
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "精制收率", "spec_range": "≥80%", "value": "82.7"},
        ])])])
        assert _check_mass_balance(pages) == []

    def test_percent_range_spec_with_value_no_finding(self):
        """边界：物料平衡率 '99%~101%' + 有值 → 不报（M4 实测的解析缺口）。"""
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "PE袋物料平衡率", "spec_range": "99%~101%", "value": "100%"},
        ])])])
        assert _check_mass_balance(pages) == []

    def test_value_without_spec_is_info(self):
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "制备收率", "spec_range": "", "value": "91.2"},
        ])])])
        fs = _check_mass_balance(pages)
        assert _types(fs) == ["mass_balance"]
        assert fs[0]["severity"] == "info"

    def test_unparseable_spec_does_not_claim_absent(self):
        """畸形/非数值 spec（如 '是/否'）不得谎报"未声明"。"""
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "收率", "spec_range": "是/否", "value": "82"},
        ])])])
        assert _check_mass_balance(pages) == []


# ===========================================================================
# R12 self_review
# ===========================================================================


class TestSelfReview:
    def test_operator_equals_reviewer_critical(self):
        pages = _norm([_page(1, [_step(
            1, operator="操作员甲", reviewer="操作员甲")])])
        fs = _check_self_review(pages)
        assert _types(fs) == ["self_review"]
        assert fs[0]["severity"] == "critical"
        assert "操作员甲" in fs[0]["description"]

    def test_signature_roles_same_person_critical(self):
        pages = _norm([_page(1, [_step(1, signatures=[
            {"role": "operator", "name": "李伟胜"},
            {"role": "reviewer", "name": "李伟胜"},
        ])])])
        fs = _check_self_review(pages)
        assert _types(fs) == ["self_review"]
        assert fs[0]["severity"] == "critical"

    def test_role_aliases_review_and_draft(self):
        """真实数据 role 混杂（draft/review），须归一后判定。"""
        pages = _norm([_page(1, [_step(1, signatures=[
            {"role": "draft", "name": "冯珠"},
            {"role": "review", "name": "冯珠"},
        ])])])
        fs = _check_self_review(pages)
        assert _types(fs) == ["self_review"]

    def test_different_people_no_finding(self):
        pages = _norm([_page(1, [_step(
            1, operator="操作员甲", reviewer="复核员乙")])])
        assert _check_self_review(pages) == []

    def test_empty_fields_no_finding(self):
        """边界：两侧必须都非空才判定（空字段不得误报）。"""
        pages = _norm([_page(1, [_step(1, operator="", reviewer="")])])
        assert _check_self_review(pages) == []

    def test_name_noise_normalized(self):
        """畸形：OCR 噪声 '#' 与空串归一后不计为同人。"""
        pages = _norm([_page(1, [_step(1, operator="#", reviewer="")])])
        assert _check_self_review(pages) == []


# ===========================================================================
# R13 equipment_state
# ===========================================================================


class TestEquipmentState:
    def test_check_answered_no_warns(self):
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "清洗过程是否无异常", "spec_range": "是/否", "value": "否"},
        ])])])
        fs = _check_equipment_state(pages)
        assert _types(fs) == ["equipment_state"]
        assert fs[0]["severity"] == "warning"

    def test_declared_choice_spec_unfilled_info(self):
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "状态标志使用是否正确", "spec_range": "是/否", "value": ""},
        ])])])
        fs = _check_equipment_state(pages)
        assert _types(fs) == ["equipment_state"]
        assert fs[0]["severity"] == "info"

    def test_answered_yes_no_finding(self):
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "设备及管路是否及时清洗干净", "spec_range": "是/否", "value": "是"},
        ])])])
        assert _check_equipment_state(pages) == []

    def test_unfilled_without_choice_spec_ignored(self):
        """边界：无"是/否"模板（spec 空）时不判"未填写"（避免误报印刷模板）。"""
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "状态标志使用是否正确", "spec_range": "", "value": ""},
        ])])])
        assert _check_equipment_state(pages) == []

    def test_non_equipment_param_ignored(self):
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "pH", "spec_range": "是/否", "value": ""},
        ])])])
        assert _check_equipment_state(pages) == []

    def test_non_dict_param_no_crash(self):
        """畸形：parameters 混入非 dict 元素不崩。"""
        pages = _norm([_page(1, [_step(1, parameters=["garbage", None])])])
        assert _check_equipment_state(pages) == []


# ===========================================================================
# R14 env_monitor
# ===========================================================================


class TestEnvMonitor:
    def test_differential_pressure_no_warns(self):
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "洁净区层流区压差是否符合要求", "spec_range": "√是/□否",
             "value": "否"},
        ])])])
        fs = _check_env_monitor(pages)
        assert _types(fs) == ["env_monitor"]
        assert fs[0]["severity"] == "warning"

    def test_declared_clean_env_without_data_info(self):
        pages = _norm([_page(1, [_step(
            1, "检查确认空调系统运行正常；洁净环境符合要求。过滤器S2104应完好。")])])
        fs = _check_env_monitor(pages)
        assert _types(fs) == ["env_monitor"]
        assert fs[0]["severity"] == "info"

    def test_declared_choice_spec_unfilled_info(self):
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "M2101隔离器压差是否符合要求", "spec_range": "√是/□否",
             "value": ""},
        ])])])
        fs = _check_env_monitor(pages)
        assert _types(fs) == ["env_monitor"]
        assert fs[0]["severity"] == "info"

    def test_pressure_check_ok_no_declared_noise(self):
        pages = _norm([_page(1, [_step(
            1, "洁净环境符合要求", parameters=[
                {"name": "洁净区层流区压差是否符合要求",
                 "spec_range": "√是/□否", "value": "√是"},
            ])])])
        assert _check_env_monitor(pages) == []

    def test_temperature_only_not_env(self):
        """边界：工艺温度不计入环境监测（否则规则永不触发且语义混乱）。"""
        pages = _norm([_page(1, [_step(1, measurements=[
            {"time": "08:00", "values": {"温度": {"actual": "25", "value_source": "printed"}}},
        ])])])
        assert _check_env_monitor(pages) == []

    def test_measurement_env_col_suppresses_declared_branch(self):
        pages = _norm([_page(1, [_step(
            1, "洁净环境符合要求", measurements=[
                {"time": "08:00", "values": {"压差": {"actual": "12", "value_source": "handwritten"}}},
            ])])])
        assert _check_env_monitor(pages) == []


# ===========================================================================
# R15 doc_version
# ===========================================================================


class TestDocVersion:
    def test_same_file_code_two_versions_warns(self):
        pages = _norm([
            _page(1, [_step(1, start_time="2025-01-01 08:00")],
                  page_info={"file_code": "H3-MPD-10133-R23", "version": "R23"}),
            _page(2, [_step(2, start_time="2025-01-01 09:00")],
                  page_info={"file_code": "H3-MPD-10133-R23", "version": "09"}),
        ])
        fs = _check_doc_version(pages)
        assert _types(fs) == ["doc_version"]
        assert fs[0]["severity"] == "warning"
        assert fs[0]["page"] == 1

    def test_different_file_codes_no_finding(self):
        """关键反例：不同表单编号（R20/R22/R23…）本就不同，不得误报。"""
        pages = _norm([
            _page(1, [_step(1)], page_info={"file_code": "H3-MPD-10133-R20", "version": "09"}),
            _page(2, [_step(2)], page_info={"file_code": "H3-MPD-10133-R22", "version": "09"}),
        ])
        assert _check_doc_version(pages) == []

    def test_same_code_same_version_no_finding(self):
        pages = _norm([
            _page(1, [_step(1)], page_info={"file_code": "X-R27", "version": "11"}),
            _page(2, [_step(2)], page_info={"file_code": "X-R27", "version": "11"}),
        ])
        assert _check_doc_version(pages) == []

    def test_version_digit_normalization_avoids_false_conflict(self):
        """边界：'R23' 与 '23' 视为同一版本（去前导零/字母）。"""
        pages = _norm([
            _page(1, [_step(1)], page_info={"file_code": "C", "version": "R23"}),
            _page(2, [_step(2)], page_info={"file_code": "C", "version": "23"}),
        ])
        assert _check_doc_version(pages) == []

    def test_missing_version_ignored(self):
        pages = _norm([
            _page(1, [_step(1)], page_info={"file_code": "C", "version": ""}),
            _page(2, [_step(2)], page_info={"file_code": "C", "version": "09"}),
        ])
        assert _check_doc_version(pages) == []

    def test_malformed_nonstring_version_no_crash(self):
        pages = _norm([
            _page(1, [_step(1)], page_info={"file_code": "C", "version": 9}),
            _page(2, [_step(2)], page_info={"file_code": "C", "version": "09"}),
        ])
        # 9 -> "9"，"09" -> "9" → 同版本，不报
        assert _check_doc_version(pages) == []


# ===========================================================================
# R16 deviation_link
# ===========================================================================


class TestDeviationLink:
    def test_explicit_anomaly_without_deviation_no_warns(self):
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "生产有无异常", "value": "有"},
        ])])])
        fs = _check_deviation_link(pages, set())
        assert _types(fs) == ["deviation_link"]
        assert fs[0]["severity"] == "warning"

    def test_oos_with_empty_deviation_field_warns(self):
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "偏差号", "value": ""},
        ])])])
        fs = _check_deviation_link(pages, {1})
        assert _types(fs) == ["deviation_link"]

    def test_no_anomaly_no_finding(self):
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "生产有无异常", "value": "无"},
            {"name": "偏差号", "value": ""},
        ])])])
        assert _check_deviation_link(pages, set()) == []

    def test_oos_without_deviation_field_no_finding(self):
        """边界：表单无"偏差号"字段时超限不报（避免误报/R3 重复放大）。"""
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "pH", "value": "3.0", "spec_range": "6.0-8.0"},
        ])])])
        assert _check_deviation_link(pages, {1}) == []

    def test_oos_with_filled_deviation_field_no_finding(self):
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "偏差号", "value": "DEV-2025-018"},
        ])])])
        assert _check_deviation_link(pages, {1}) == []

    def test_explicit_anomaly_with_deviation_filled_no_finding(self):
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "生产有无异常", "value": "有"},
            {"name": "偏差编号", "value": "DEV-01"},
        ])])])
        assert _check_deviation_link(pages, set()) == []


# ===========================================================================
# R17 alteration
# ===========================================================================


class TestAlteration:
    def test_alteration_in_notes_without_signature_warns(self):
        pages = _norm([_page(1, [_step(1, handwritten=["原值5.2划改为5.6"])])])
        fs = _check_alteration(pages)
        assert _types(fs) == ["alteration"]
        assert fs[0]["severity"] == "warning"

    def test_alteration_mark_in_operation_warns(self):
        pages = _norm([_page(1, [_step(1, "称量 5.2(误) ")])])
        fs = _check_alteration(pages)
        assert _types(fs) == ["alteration"]

    def test_no_trace_no_finding(self):
        pages = _norm([_page(1, [_step(1, "物料称量", start_time="2025-01-01 08:00")])])
        assert _check_alteration(pages) == []

    def test_trace_with_signature_no_finding(self):
        """边界：有操作人签名/时间 → 视为已按规定划改留痕，不报。"""
        pages = _norm([_page(1, [_step(
            1, "称量 5.2(误)", operator="操作员甲",
            start_time="2025-01-01 08:00")])])
        assert _check_alteration(pages) == []

    def test_non_string_notes_no_crash(self):
        """畸形：handwritten 混入非字符串不崩。"""
        pages = _norm([_page(1, [_step(1, handwritten=[None, 123, {}])])])
        assert _check_alteration(pages) == []

    def test_long_value_not_treated_as_note(self):
        """边界：参数值里的"修改"字样若很长（正文），不当作涂改标记。"""
        pages = _norm([_page(1, [_step(1, parameters=[
            {"name": "说明", "value": "本批生产过程中未发生任何修改或变更事项" * 2},
        ])])])
        assert _check_alteration(pages) == []
