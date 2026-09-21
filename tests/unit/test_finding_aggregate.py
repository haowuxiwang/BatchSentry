"""B1-1 同根因 finding 聚合的护栏测试。

覆盖四件事（每件都可变异验证）：
  1. **不回归**：单个 finding 的 description / ocr_text / severity 与重构前
     **逐字节相同**（用真实语料 p8 的 7 条冻结串钉住；重构前串取自生产库）。
  2. **真的合并**：p8 同型输入 7 条 ⇒ 1 条，且时间点清单完整。
  3. **不误并**（反向对照）：取值不同 / 根因不同 / 页不同 / 主语不同 ⇒ 不合并。
  4. **留痕**：合并明细（成员数、成员原文、severity 分布）必须返回。
  5. **派生键覆盖**：`_check_param_out_of_spec` 的**每一条**产出都必须带聚合键
     —— 防止未来新增发射点漏挂而静默绕过聚合。
"""
from __future__ import annotations

import pytest

from core.rules.finding_aggregate import (
    AGG_KEY,
    AGG_PARTS,
    SEVERITY_RANK,
    aggregate_by_root_cause,
    make_oos_finding,
)
from core.rules import llm_finding_guard
from core.rules.rule_spec import _check_param_out_of_spec


def _p8_pages(times=("10:38", "11:34", "12:37", "13:36", "14:39", "15:34", "16:22")):
    """真实 p8 同型输入：一列「进料压力」在 N 个时间点被读成 46（丢小数点）。"""
    return [{
        "page": 8,
        "steps": [{
            "step_no": "3",
            "measurements": [
                {"time": t, "values": {
                    "进料压力": {"spec": "3.0-5.0bar", "actual": "46",
                                 "unit": "bar", "value_source": "handwritten"},
                }}
                for t in times
            ],
        }],
    }]


# ---------------------------------------------------------------------------
# 1. 契约：severity 排序与 llm_finding_guard 同源
# ---------------------------------------------------------------------------


class TestSeverityRankContract:
    def test_rank_matches_guard_constant(self):
        """两处各持常量 ⇒ 钉住相等，任一侧漂移即红（避免两套排序悄悄分叉）。"""
        assert SEVERITY_RANK == llm_finding_guard._SEV_RANK


# ---------------------------------------------------------------------------
# 2. 不回归：单条产出逐字节不变
# ---------------------------------------------------------------------------


class TestSingleFindingsUnchanged:
    def test_p8_single_descriptions_are_byte_identical(self):
        """7 条单条描述与**重构前生产库**的原文逐字节相同。

        冻结串取自 job `6f80145a-47f` 的 findings（source=rule,
        type=param_out_of_spec, page=8），即重构前的真实产出。
        """
        findings = _check_param_out_of_spec(_p8_pages(), [])
        got = [f["description"] for f in findings]
        for t in ("10:38", "11:34", "16:22"):
            expected = (
                f"第8页 进料压力 在 {t} 时实测 46.0bar 不在规格 3.0-5.0bar 内，"
                f"实测值与规格相差约 10 倍（疑似 OCR 丢失小数点，如 4.6 读成 46），"
                f"请对照 PDF 原页人工核对"
            )
            assert expected in got, f"单条文案被改写（{t}）"
        assert all(f["severity"] == "info" for f in findings)
        assert [f["ocr_text"] for f in findings] == [
            "10:38 进料压力: spec=3.0-5.0bar actual=46",
            "11:34 进料压力: spec=3.0-5.0bar actual=46",
            "12:37 进料压力: spec=3.0-5.0bar actual=46",
            "13:36 进料压力: spec=3.0-5.0bar actual=46",
            "14:39 进料压力: spec=3.0-5.0bar actual=46",
            "15:34 进料压力: spec=3.0-5.0bar actual=46",
            "16:22 进料压力: spec=3.0-5.0bar actual=46",
        ]

    def test_aggregator_leaves_single_member_untouched(self):
        """组内只有 1 条时：**原对象**通过（不是重建一份等值副本）。"""
        findings = _check_param_out_of_spec(_p8_pages(times=("10:38",)), [])
        out, rows = aggregate_by_root_cause(findings)
        assert out[0] is findings[0]
        assert rows == []

    def test_unkeyed_findings_pass_through_by_identity(self):
        """未携带派生键的条目（如 completeness）不得被触碰，且顺序保持。"""
        inert = [{"page": 1, "type": "completeness", "description": "x"}]
        keyed = make_oos_finding(
            page=1, type_="param_out_of_spec", severity="warning",
            kind="cell", subject="温度", cause="hard",
            desc_value="9", raw_value="9", unit="°C", spec="0-5", where="01:00",
        )
        out, _ = aggregate_by_root_cause(inert + [keyed])
        assert out[0] is inert[0]
        assert out[1] is keyed


# ---------------------------------------------------------------------------
# 3. 真的合并 + 留痕
# ---------------------------------------------------------------------------


class TestAggregation:
    def test_p8_seven_become_one_with_full_time_list(self):
        findings = _check_param_out_of_spec(_p8_pages(), [])
        assert len(findings) == 7
        out, rows = aggregate_by_root_cause(findings)
        assert len(out) == 1
        desc = out[0]["description"]
        assert "在 7 个时间点（10:38、11:34、12:37、13:36、14:39、15:34、16:22）" in desc
        assert "实测 46.0bar 不在规格 3.0-5.0bar 内" in desc
        # 语义未被改写：仍指向同一根因
        assert "疑似 OCR 丢失小数点" in desc
        assert out[0]["severity"] == "info"

    def test_merged_ocr_text_keeps_every_member_detail(self):
        """留痕：每条原始 spec/actual 都还在 ocr_text 里（可抽检）。"""
        out, _ = aggregate_by_root_cause(
            _check_param_out_of_spec(_p8_pages(), []))
        ocr = out[0]["ocr_text"]
        for t in ("10:38", "11:34", "12:37", "13:36", "14:39", "15:34", "16:22"):
            assert f"{t} 进料压力: spec=3.0-5.0bar actual=46" in ocr

    def test_disclosure_row_is_detail_not_count(self):
        _, rows = aggregate_by_root_cause(_check_param_out_of_spec(_p8_pages(), []))
        assert len(rows) == 1
        row = rows[0]
        assert row["count"] == 7
        assert row["page"] == 8
        assert row["cause"] == "decimal_loss"
        assert len(row["members"]) == 7
        assert row["severities"] == ["info"]

    def test_merged_severity_is_max(self):
        """同组内 severity 分化（手写 vs 印刷）⇒ 取最高，不取首条。"""
        a = make_oos_finding(page=1, type_="param_out_of_spec", severity="info",
                             kind="cell", subject="压力", cause="hard",
                             desc_value="9", raw_value="9", unit="", spec="0-5",
                             where="01:00")
        b = make_oos_finding(page=1, type_="param_out_of_spec", severity="warning",
                             kind="cell", subject="压力", cause="hard",
                             desc_value="9", raw_value="9", unit="", spec="0-5",
                             where="02:00")
        out, _ = aggregate_by_root_cause([a, b])
        assert len(out) == 1
        assert out[0]["severity"] == "warning"

    def test_param_fanout_across_steps_uses_place_phrase(self):
        """同页同名参数（跨工序）也属同根因 ⇒ 合并，措辞用"共 N 处"。

        ⚠️ 该措辞的 N 必须来自**成员数**：单值参数没有"时间点"维度，
        若从位置列表推会恒得 0（实测踩过 ⇒ 措辞永不出现）。
        """
        a = make_oos_finding(page=45, type_="param_out_of_spec", severity="warning",
                             kind="param", subject="水洗开始时间", cause="hard",
                             desc_value="9", raw_value="9", unit="", spec="0-5")
        b = make_oos_finding(page=45, type_="param_out_of_spec", severity="warning",
                             kind="param", subject="水洗开始时间", cause="hard",
                             desc_value="9", raw_value="9", unit="", spec="0-5")
        out, rows = aggregate_by_root_cause([a, b])
        assert len(out) == 1
        assert rows[0]["count"] == 2
        assert "（本页共 2 处同名参数）" in out[0]["description"]

    def test_param_where_phrase_absent_when_single(self):
        """单值参数**单条**时不得出现"共 N 处"（N=1 是废话且会误导）。"""
        a = make_oos_finding(page=45, type_="param_out_of_spec", severity="warning",
                             kind="param", subject="水洗开始时间", cause="hard",
                             desc_value="9", raw_value="9", unit="", spec="0-5")
        assert a["description"] == "第45页 参数 水洗开始时间=9 不在规格 0-5 内"


# ---------------------------------------------------------------------------
# 4. 反向对照：**不得**合并的情形（防"过度合并"）
# ---------------------------------------------------------------------------


class TestNoOverMerge:
    def test_different_values_are_not_merged(self):
        """同页同列同规格但**取值不同** ⇒ 两条不同事实，不得并。"""
        findings = _check_param_out_of_spec([{
            "page": 8,
            "steps": [{"step_no": "3", "measurements": [
                {"time": "10:38", "values": {"进料压力": {
                    "spec": "3.0-5.0bar", "actual": "46", "unit": "bar"}}},
                {"time": "11:34", "values": {"进料压力": {
                    "spec": "3.0-5.0bar", "actual": "47", "unit": "bar"}}},
            ]}],
        }], [])
        out, rows = aggregate_by_root_cause(findings)
        assert len(out) == 2
        assert rows == []

    def test_different_root_cause_is_not_merged(self):
        """同列同值同 severity，但根因不同 ⇒ 判据基础不同，不得并。

        ⚠️ 用 ``printed`` vs ``hard`` 这一对**故意构造**：两者的提示语
        （hint）**都是空串** ⇒ 只靠 suffix 无法区分，唯一能拦住合并的是
        ``cause`` 本身。若换成"边缘超差 vs 丢小数点"，是 suffix 在起作用的
        ——那样的用例**钉不住** cause 字段（实测变异 M2 因此漏网过一次）。
        """
        a = make_oos_finding(page=8, type_="param_out_of_spec", severity="warning",
                             kind="cell", subject="进料压力", cause="printed",
                             desc_value="46", raw_value="46", unit="bar",
                             spec="3.0-5.0bar", suffix="", where="10:38")
        b = make_oos_finding(page=8, type_="param_out_of_spec", severity="warning",
                             kind="cell", subject="进料压力", cause="hard",
                             desc_value="46", raw_value="46", unit="bar",
                             spec="3.0-5.0bar", suffix="", where="11:34")
        assert a["description"].replace("10:38", "") == \
            b["description"].replace("11:34", ""), "前提失效：两者文案本应同构"
        out, rows = aggregate_by_root_cause([a, b])
        assert len(out) == 2, "不同根因被并 ⇒ 判据基础被抹平"
        assert rows == []

    def test_different_hint_is_not_merged(self):
        """同根因但提示语不同（如单位换算备注不同）⇒ 不得并。"""
        a = make_oos_finding(page=8, type_="param_out_of_spec", severity="info",
                             kind="cell", subject="进料压力", cause="edge_margin",
                             desc_value="46", raw_value="46", unit="bar",
                             spec="3.0-5.0bar", suffix="，超差幅度较小", where="10:38")
        b = make_oos_finding(page=8, type_="param_out_of_spec", severity="info",
                             kind="cell", subject="进料压力", cause="decimal_loss",
                             desc_value="46", raw_value="46", unit="bar",
                             spec="3.0-5.0bar", suffix="，疑似丢失小数点", where="11:34")
        out, rows = aggregate_by_root_cause([a, b])
        assert len(out) == 2

    def test_different_page_is_not_merged(self):
        a = make_oos_finding(page=8, type_="param_out_of_spec", severity="info",
                             kind="cell", subject="进料压力", cause="hard",
                             desc_value="46", raw_value="46", unit="bar",
                             spec="3.0-5.0bar", where="10:38")
        b = make_oos_finding(page=9, type_="param_out_of_spec", severity="info",
                             kind="cell", subject="进料压力", cause="hard",
                             desc_value="46", raw_value="46", unit="bar",
                             spec="3.0-5.0bar", where="10:38")
        out, rows = aggregate_by_root_cause([a, b])
        assert len(out) == 2

    def test_different_subject_is_not_merged(self):
        a = make_oos_finding(page=8, type_="param_out_of_spec", severity="info",
                             kind="cell", subject="进料压力", cause="hard",
                             desc_value="46", raw_value="46", unit="bar",
                             spec="3.0-5.0bar", where="10:38")
        b = make_oos_finding(page=8, type_="param_out_of_spec", severity="info",
                             kind="cell", subject="进料_压力", cause="hard",
                             desc_value="46", raw_value="46", unit="bar",
                             spec="3.0-5.0bar", where="10:38")
        out, _ = aggregate_by_root_cause([a, b])
        assert len(out) == 2

    def test_cell_and_param_kinds_are_not_merged(self):
        a = make_oos_finding(page=8, type_="param_out_of_spec", severity="info",
                             kind="cell", subject="压力", cause="hard",
                             desc_value="46", raw_value="46", unit="bar",
                             spec="3.0-5.0bar", where="10:38")
        b = make_oos_finding(page=8, type_="param_out_of_spec", severity="info",
                             kind="param", subject="压力", cause="hard",
                             desc_value="46", raw_value="46", unit="bar",
                             spec="3.0-5.0bar")
        out, _ = aggregate_by_root_cause([a, b])
        assert len(out) == 2


# ---------------------------------------------------------------------------
# 5. spec_unverifiable 两种根因的聚合文案
# ---------------------------------------------------------------------------


class TestSpecUnverifiableTemplates:
    @staticmethod
    def _vacuum(times):
        """真实 p33 同型：一列真空度在 N 个时间点都是负值、规格是正限值。"""
        return [{
            "page": 33,
            "steps": [{"step_no": "1", "measurements": [
                {"time": t, "values": {"D2101_真空度": {
                    "spec": "≤0.09MPa", "actual": "-0.098MPa",
                    "unit": "MPa", "value_source": "handwritten"}}}
                for t in times
            ]}],
        }]

    def test_sign_uncertain_fanout_merged(self):
        times = tuple(f"{h:02d}:21" for h in (0, 4, 8, 12, 16, 20)) + ("00:17", "02:32")
        findings = _check_param_out_of_spec(self._vacuum(times), [])
        assert len(findings) == 8
        assert all(f["type"] == "spec_unverifiable" for f in findings)
        out, rows = aggregate_by_root_cause(findings)
        assert len(out) == 1
        assert rows[0]["cause"] == "sign_uncertain"
        assert "在 8 个时间点（" in out[0]["description"]
        assert "符号约定（真空度/负压）存疑" in out[0]["description"]

    def test_unit_mismatch_template_renders_for_cell_and_param(self):
        cell = make_oos_finding(page=5, type_="spec_unverifiable", severity="warning",
                                kind="cell", subject="残留溶剂", cause="unit_mismatch",
                                desc_value="20.0ppm", raw_value="20.0ppm", unit="ppm",
                                spec="≤900pp", where="12:10")
        assert "第5页 残留溶剂 在 12:10 时实测值单位不一致且无换算规则" in cell["description"]
        assert "需人工确认" in cell["description"]
        param = make_oos_finding(page=5, type_="spec_unverifiable", severity="warning",
                                 kind="param", subject="残留溶剂", cause="unit_mismatch",
                                 desc_value="20.0ppm", raw_value="20.0ppm", unit="ppm",
                                 spec="≤900pp")
        assert param["description"] == (
            "第5页 参数 残留溶剂 单位不一致且无换算规则"
            "（spec=≤900pp, actual=20.0ppm），需人工确认"
        )

    def test_unknown_type_raises_instead_of_silent_half_text(self):
        """未知 **type** 必须显式失败 —— 不允许静默产出半成品文案。"""
        with pytest.raises(ValueError):
            make_oos_finding(page=1, type_="completeness", severity="info",
                             kind="cell", subject="x", cause="hard",
                             desc_value="1", raw_value="1", unit="", spec="0-1",
                             where="01:00")

    def test_spec_unverifiable_with_unknown_cause_raises(self):
        """`spec_unverifiable` 的分支已穷举两种根因 ⇒ 第三种必须显式失败。"""
        with pytest.raises(ValueError):
            make_oos_finding(page=1, type_="spec_unverifiable", severity="warning",
                             kind="cell", subject="x", cause="nonexistent",
                             desc_value="1", raw_value="1", unit="", spec="0-1",
                             where="01:00")


# ---------------------------------------------------------------------------
# 6. 派生键覆盖（防"新增发射点漏挂键"）
# ---------------------------------------------------------------------------


class TestAggKeyCoverage:
    def test_every_emitted_finding_carries_agg_fields(self):
        """`_check_param_out_of_spec` 的每条产出都必须带 AGG_KEY / AGG_PARTS。

        否则该发射点会**静默绕过**聚合（降噪承诺失效而测试全绿）。
        """
        pages = _p8_pages()
        pages.append({
            "page": 9,
            "steps": [{"step_no": "1", "parameters": [
                {"name": "浓缩结束真空度", "value": "-0.090", "unit": "MPa",
                 "spec_range": "≤0.08MPa", "value_source": "handwritten"},
                {"name": "残留溶剂", "value": "20.0ppm", "unit": "ppm",
                 "spec_range": "≤900pp", "value_source": "handwritten"},
            ]}],
        })
        findings = _check_param_out_of_spec(pages, [])
        assert findings, "夹具未产出任何 finding（测试前提失效）"
        missing = [
            (f.get("page"), f.get("type"), f.get("description", "")[:40])
            for f in findings if AGG_KEY not in f or AGG_PARTS not in f
        ]
        assert missing == [], f"以下产出漏挂聚合键：{missing}"

    def test_key_excludes_where_but_parts_carry_it(self):
        """位置（时间点）是变化维度 ⇒ 不进键；但部件必须携带它（聚合靠它重建清单）。"""
        a = make_oos_finding(page=1, type_="param_out_of_spec", severity="info",
                             kind="cell", subject="压力", cause="hard",
                             desc_value="9", raw_value="9", unit="", spec="0-5",
                             where="01:00")
        b = make_oos_finding(page=1, type_="param_out_of_spec", severity="info",
                             kind="cell", subject="压力", cause="hard",
                             desc_value="9", raw_value="9", unit="", spec="0-5",
                             where="02:00")
        assert a[AGG_KEY] == b[AGG_KEY], "位置不应进键，否则永不合并"
        assert a[AGG_PARTS]["where"] == "01:00"
        assert b[AGG_PARTS]["where"] == "02:00"
