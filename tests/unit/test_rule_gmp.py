"""R11–R17 单测（M4/T4.8）—— 每条规则正例/反例/边界/畸形 ≥4 例。

规则函数是纯函数（吃 `_normalize_pages` 输出的 pages），故离线、零 LLM、
逐位可复现。断言聚焦"是否触发 + type + severity"，描述文本只做关键子串校验。
"""
from __future__ import annotations


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

    def test_balance_row_used_exceeds_issued_is_info_with_caveat(self):
        """实测 sy p9：OCR 读成 领用20/使用30 ⇒ 表内算术不成立。

        手写读数未经独立复核 ⇒ 只能 info +「不作为偏差结论」附注，
        与 R130 的「断言由读数质量决定」同一口径。
        """
        pages = _norm([_page(9, [_step(8, "PE袋使用的物料平衡计算。", parameters=[
            {"name": "透明PE袋 (1100×700mm ×0.06mm) 领用数", "value": "0309 20",
             "value_source": "handwritten"},
            {"name": "透明PE袋 (1100×700mm ×0.06mm) 使用数", "value": "30",
             "value_source": "handwritten"},
            {"name": "透明PE袋 (1100×700mm ×0.06mm) 损耗数", "value": "0",
             "value_source": "handwritten"},
        ])])])
        fs = _check_mass_balance(pages)
        assert _types(fs) == ["mass_balance"]
        assert fs[0]["severity"] == "info"
        assert "不作为偏差结论" in fs[0]["description"]
        assert "使用数30" in fs[0]["description"]
        assert "领用数20" in fs[0]["description"]

    def test_balance_row_used_plus_loss_exceeds_issued(self):
        pages = _norm([_page(1, [_step(8, parameters=[
            {"name": "PE袋 领用数", "value": "10", "value_source": "handwritten"},
            {"name": "PE袋 使用数", "value": "9", "value_source": "handwritten"},
            {"name": "PE袋 损耗数", "value": "2", "value_source": "handwritten"},
        ])])])
        fs = _check_mass_balance(pages)
        assert len(fs) == 1 and fs[0]["type"] == "mass_balance"

    def test_balance_row_arithmetic_ok_is_silent(self):
        """p4 实测 3=2+1：领用 = 使用 + 损耗，不得报警。"""
        pages = _norm([_page(4, [_step(8, parameters=[
            {"name": "PE袋 领用数", "value": "3", "value_source": "handwritten"},
            {"name": "PE袋 使用数", "value": "2", "value_source": "handwritten"},
            {"name": "PE袋 损耗数", "value": "1", "value_source": "handwritten"},
        ])])])
        assert _check_mass_balance(pages) == []

    def test_balance_row_printed_values_warn_without_handwritten_caveat(self):
        pages = _norm([_page(1, [_step(8, parameters=[
            {"name": "PE袋 领用数", "value": "10"},
            {"name": "PE袋 使用数", "value": "12"},
        ])])])
        fs = _check_mass_balance(pages)
        assert fs and fs[0]["severity"] == "warning"
        assert "不作为偏差结论" not in fs[0]["description"]

    def test_raw_text_fallback_when_structured_missing(self):
        """结构化漏抽整张表（实测 sy_r7 p9）⇒ 原文三元组兜底仍判得出。"""
        html = (
            "<td>领用数 (只)</td><td>使用数 (只)</td><td>损耗数 (只)</td>"
            "<td>1</td><td>透明PE袋</td><td>3000196</td><td>72502</td><td>0309</td>"
            "<td>20</td><td>30</td><td>0</td>"
            "<td>2</td><td>透明PE袋</td><td>3000203</td><td>72504</td><td>0207</td>"
            "<td>30</td><td>30</td><td>0</td>"
        )
        page = _page(9, [_step(8, "PE袋使用的物料平衡计算。")])
        page["raw_html"] = html
        fs = _check_mass_balance(_norm([page]))
        assert len(fs) == 1, "只有 20/30/0 一行算术不成立；30/30/0 正常"
        assert fs[0]["severity"] == "info", "原文读数无法区分印刷/手写 ⇒ 只许 info"
        assert "不作为偏差结论" in fs[0]["description"]
        assert "第1行" in fs[0]["description"]
        assert fs[0]["ocr_text"].startswith("balance_row=text_row1")

    def test_raw_text_fallback_skipped_when_structured_rows_exist(self):
        """结构化已有读数（30/30/0 正常）⇒ 不再用原文兜底，避免同表两张卡。"""
        html = "<td>领用数</td><td>使用数</td><td>损耗数</td><td>20</td><td>30</td><td>0</td>"
        page = _page(9, [_step(8, parameters=[
            {"name": "PE袋 领用数", "value": "30"},
            {"name": "PE袋 使用数", "value": "30"},
            {"name": "PE袋 损耗数", "value": "0"},
        ])])
        page["raw_html"] = html
        assert _check_mass_balance(_norm([page])) == []

    def test_raw_text_without_header_is_silent(self):
        """无「领用数」表头的连续数字串（批号/代码）不得误判成表格行。"""
        html = "<td>72502</td><td>0309</td><td>20</td><td>30</td><td>0</td>"
        page = _page(9, [_step(8)])
        page["raw_html"] = html
        assert _check_mass_balance(_norm([page])) == []

    def test_raw_text_stops_at_next_step_sentence(self):
        """表格之后的工序句子即表格结束：步骤 10 的压差/温湿度三元组不得混入。

        实测 sy_r7 p9：平衡表后紧跟「9 将过筛后物料转移至…」与
        「房间号 时间 相对压差 温度 湿度 … 20.6 51.6 11.2」—— 不截断时会
        多报一条「第3行 领用20.6/使用51.6/损耗11.2」假阳性。
        """
        html = (
            "<td>领用数</td><td>使用数</td><td>损耗数</td>"
            "<td>1</td><td>透明PE袋</td><td>3000196</td><td>72502</td><td>0309</td>"
            "<td>20</td><td>30</td><td>0</td>"
            "<td>2</td><td>透明PE袋</td><td>3000203</td><td>72504</td><td>0207</td>"
            "<td>30</td><td>30</td><td>0</td>"
            "<td>9</td><td>将过筛后物料转移至物料复核室等待下一步生产。</td>"
            "<td>房间号</td><td>时间</td><td>相对压差</td><td>温度</td><td>湿度</td>"
            "<td>20.6</td><td>51.6</td><td>11.2</td>"
        )
        page = _page(9, [_step(8, "PE袋使用的物料平衡计算。")])
        page["raw_html"] = html
        fs = _check_mass_balance(_norm([page]))
        assert len(fs) == 1, "只保留平衡表第1行；环境表的三元组属于另一张表"
        assert "使用数30" in fs[0]["description"]


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
        """两侧签名都**显式 high** ⇒ critical（手写体也有可靠读数时允许定性）。"""
        pages = _norm([_page(1, [_step(1, signatures=[
            {"role": "operator", "name": "吴伟胜", "confidence": "high"},
            {"role": "reviewer", "name": "吴伟胜", "confidence": "high"},
        ])])])
        fs = _check_self_review(pages)
        assert _types(fs) == ["self_review"]
        assert fs[0]["severity"] == "critical"

    def test_signature_without_confidence_is_unreadable(self):
        """R133：签名槽缺 confidence ≠ 可靠。实证 10 页 p4 原页为两人，旧逻辑误判 critical。"""
        pages = _norm([_page(1, [_step(1, signatures=[
            {"role": "operator", "name": "吴伟胜"},
            {"role": "reviewer", "name": "吴伟胜"},
        ])])])
        fs = _check_self_review(pages)
        assert _types(fs) == ["signature_unreadable"]
        assert fs[0]["severity"] == "info"
        assert "核对原页笔迹" in fs[0]["description"]

    def test_signature_medium_confidence_is_unreadable(self):
        """medium 也不够：只有两侧都显式 high 才允许 critical（单一真值：high）。"""
        pages = _norm([_page(1, [_step(1, signatures=[
            {"role": "operator", "name": "吴伟胜", "confidence": "medium"},
            {"role": "reviewer", "name": "吴伟胜", "confidence": "medium"},
        ])])])
        assert _types(_check_self_review(pages)) == ["signature_unreadable"]

    def test_printed_fields_still_critical(self):
        """反向控制：印刷字段相等不受签名置信度新规影响（不得误降）。"""
        pages = _norm([_page(1, [_step(1, operator="操作员甲", reviewer="操作员甲")])])
        fs = _check_self_review(pages)
        assert _types(fs) == ["self_review"]
        assert fs[0]["severity"] == "critical"

    def test_role_aliases_review_and_draft(self):
        """真实数据 role 混杂（draft/review），须归一后判定。"""
        pages = _norm([_page(1, [_step(1, signatures=[
            {"role": "draft", "name": "冯珠", "confidence": "high"},
            {"role": "review", "name": "冯珠", "confidence": "high"},
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
