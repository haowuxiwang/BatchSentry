"""R100 护栏：参数类「护栏知道错还发」（CR-12）/ 规则层拒判抑制 LLM 断言（CR-13）
/ finding 必须带可核对原文（CR-23），以及落地过程中暴露的规格解析缺口（CR-35）。

为什么这四条要放在一个文件里：它们共享**同一条**判据链
（`parsing.value_shape_reason / spec_shape_reason / bound_plausibility_reason`
→ `rule_spec._judge_value` → `spec_guard._triple_state`）。任何一环被改回
"判得出来就发超差"，这里必须红。

真实数据来源（全部可复现，见 `devlogs/_verify/r100_probe.py`）：
  51 页 job `83a0219d-e0b`、10 页 job `4cffb358-3b3`。
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from core.rules.finding_aggregate import (
    CAUSE_DECIMAL_LOSS,
    CAUSE_HARD,
    CAUSE_LIMIT_LITERAL,
    CAUSE_RATIO_IMPLAUSIBLE,
    CAUSE_SPEC_NOT_A_LIMIT,
    CAUSE_SPEC_ZERO_BOUND,
    CAUSE_TIME_LITERAL,
    expected_type,
    make_oos_finding,
)
from core.rules.parsing import (
    _judge,
    _parse_spec,
    bound_plausibility_reason,
    spec_shape_reason,
    value_shape_reason,
)
from core.rules.rule_spec import _judge_cell, _judge_param
from core.rules.spec_guard import _triple_state, drop_unfounded_spec_findings

REPO = Path(__file__).resolve().parents[2]


def _param(name, spec, value, unit="", **kw):
    d = {"name": name, "spec_range": spec, "value": value, "unit": unit}
    d.update(kw)
    return d


def _judge_one(name, spec, value, unit=""):
    findings: list[dict] = []
    queue: list[dict] = []
    _judge_param(_param(name, spec, value, unit), 1, "1", name, findings, queue)
    return findings, queue


# ---------------------------------------------------------------------------
# CR-35：括号写法（真实 10 页语料 8 处温压监控此前从未被判）
# ---------------------------------------------------------------------------


class TestBracketedSpec:
    """`(18-26)°C` / `(方位) ≥5Pa` 此前整条解析失败 ⇒ 静默不判。"""

    def test_range_in_brackets_with_trailing_unit(self):
        b = _parse_spec("(18-26)°C")
        assert b is not None and (b.low, b.high) == (18.0, 26.0)

    def test_fullwidth_brackets(self):
        b = _parse_spec("（18-26）℃")
        assert b is not None and (b.low, b.high) == (18.0, 26.0)

    def test_location_prefix_then_limit(self):
        b = _parse_spec("(洁净走廊→过筛室) ≥5Pa")
        assert b is not None and b.op == "ge" and b.low == 5.0

    def test_location_prefix_with_space_inside(self):
        """真实 p9 原文括号内夹空格：「(洁净走廊→过筛 室) ≥5Pa」。"""
        b = _parse_spec("(洁净走廊→过筛 室) ≥5Pa")
        assert b is not None and b.op == "ge" and b.low == 5.0

    def test_multi_number_spec_is_still_unparsed(self):
        """反向对照：括号里是分段规格时**不得**硬解释成单一区间。"""
        assert _parse_spec("(收集前 0.1~0.2，收集后 1.0~1.5) m^3/h") is None

    def test_temperature_cell_now_judged_in_spec(self):
        """修好之后：19°C 落在 [18,26] ⇒ 合规，且**不产生任何 finding**。"""
        findings, queue = _judge_one("2124_温度(°C)", "(18-26)°C", "19°C", "°C")
        assert findings == [] and queue == []

    def test_pressure_cell_now_judged_in_spec(self):
        findings, queue = _judge_one("相对压差(Pa)", "(洁净走廊→过筛室) ≥5Pa", "10Pa", "Pa")
        assert findings == [] and queue == []


# ---------------------------------------------------------------------------
# CR-12 三段判据：形状级 / 规格级 / 越界可信度
# ---------------------------------------------------------------------------


class TestValueShapeReason:
    def test_clock_literal_is_time_literal(self):
        assert value_shape_reason("18 时 30 分") == "time_literal"

    def test_colon_clock_is_time_literal(self):
        assert value_shape_reason("09:00") == "time_literal"

    def test_plain_number_is_not(self):
        assert value_shape_reason("46") is None
        assert value_shape_reason("3.711") is None
        assert value_shape_reason("<0.02%") is None


class TestSpecShapeReason:
    def test_bare_target_with_different_actual(self):
        """真实 p24：规格栏写「50L」（目标值），记录 48 L。"""
        assert spec_shape_reason("50L", "48 L") == "spec_not_a_limit"

    def test_bare_target_equal_to_actual_is_silent(self):
        """实测等于目标 ⇒ 无可报之事（真实 p17/p18 总流速 14 对 14）。"""
        assert spec_shape_reason("14 L/min", "14") is None

    def test_segmented_spec_is_not_called_a_target(self):
        """分段规格有 `~` ⇒ 不是"目标值、无允许范围"，交 LLM 兜底。"""
        spec = "收集前 0.1~0.2 m^3/h ，收集后 1.0~1.5 m^3/h"
        assert spec_shape_reason(spec, "0.181") is None

    def test_text_spec_without_number_is_not_a_target(self):
        assert spec_shape_reason("规格见附件", "42.1") is None

    def test_empty_spec_is_not_a_target(self):
        assert spec_shape_reason("", "42.1") is None


class TestBoundPlausibility:
    """六条真实验收点：每一条都必须落到**唯一**的原因码上。"""

    @pytest.mark.parametrize("spec,actual,cause", [
        ("3.0-5.0", "46", CAUSE_DECIMAL_LOSS),          # p8 46bar
        ("≤0", "0.02%", CAUSE_SPEC_ZERO_BOUND),          # p28 其他单个杂质≤0
        ("≤50", "<99ppm", CAUSE_LIMIT_LITERAL),          # p31 乙酸乙酯
        ("800~1000L", "7800L", CAUSE_RATIO_IMPLAUSIBLE),  # p39 液碱用量
        ("400~500L", "7400L", CAUSE_RATIO_IMPLAUSIBLE),   # p39 用量
    ])
    def test_real_cases(self, spec, actual, cause):
        from core.rules.parsing import _parse_number
        bounds = _parse_spec(spec)
        num = _parse_number(actual)
        assert not _judge(bounds, num)
        assert bound_plausibility_reason(bounds, num, actual) == cause

    @pytest.mark.parametrize("spec,actual", [
        ("0~5°C", "45.6"),      # p26 冷库失控：带小数点，是真偏差
        ("不超过100次", "417"),   # p9 树脂超用：倍数 4.17 < 5
        ("≥98%", "49.0"),       # p19 纯度不合格
        ("0~5", "-1"),          # between 的下界 0 是正常区间端
    ])
    def test_real_deviations_are_plausible(self, spec, actual):
        """反向对照：真实超差**不得**被判成"不可信"。"""
        from core.rules.parsing import _parse_number
        bounds = _parse_spec(spec)
        num = _parse_number(actual)
        assert not _judge(bounds, num)
        assert bound_plausibility_reason(bounds, num, actual) is None


# ---------------------------------------------------------------------------
# CR-12 ④：类型由根因派生（自证矛盾在构造点就失败）
# ---------------------------------------------------------------------------


class TestTypeDerivedFromCause:
    def test_unverifiable_causes_map_to_spec_unverifiable(self):
        for cause in (CAUSE_TIME_LITERAL, CAUSE_SPEC_ZERO_BOUND,
                      CAUSE_LIMIT_LITERAL, CAUSE_RATIO_IMPLAUSIBLE,
                      CAUSE_SPEC_NOT_A_LIMIT, CAUSE_DECIMAL_LOSS):
            assert expected_type(cause) == "spec_unverifiable"

    def test_hard_causes_map_to_param_out_of_spec(self):
        assert expected_type(CAUSE_HARD) == "param_out_of_spec"

    def test_contradictory_pair_raises(self):
        """把 decimal_loss 标成 param_out_of_spec ⇒ 构造点直接抛错。"""
        with pytest.raises(ValueError, match="自相矛盾"):
            make_oos_finding(page=8, type_="param_out_of_spec", severity="info",
                             kind="cell", subject="进料压力", cause=CAUSE_DECIMAL_LOSS,
                             desc_value="46", raw_value="46", unit="bar",
                             spec="3.0-5.0bar", where="10:38")

    def test_no_param_out_of_spec_text_says_ocr(self):
        """CR-12④ 的文案面：param_out_of_spec 里不得出现"疑似 OCR"。"""
        f = make_oos_finding(page=9, type_="param_out_of_spec", severity="warning",
                             kind="param", subject="本批树脂使用次数", cause=CAUSE_HARD,
                             desc_value=417.0, raw_value="417", unit="次",
                             spec="不超过100次")
        assert "疑似 OCR" not in f["description"]


# ---------------------------------------------------------------------------
# CR-12 端到端：六条验收点全部改发 spec_unverifiable
# ---------------------------------------------------------------------------


class TestJudgeEmitsUnverifiable:
    @pytest.mark.parametrize("name,spec,value,unit", [
        ("进料压力", "3.0-5.0", "46", "bar"),
        ("有关物质-其他单个杂质", "≤0", "<0.02%", "%"),
        ("残留溶剂-乙酸乙酯", "≤50", "<99ppm", "ppm"),
        ("液碱用量", "800~1000L", "7800L", "L"),
        ("用量", "400~500L", "7400L", "L"),
        ("浓缩液体积", "50L", "48 L", "L"),
    ])
    def test_becomes_spec_unverifiable(self, name, spec, value, unit):
        findings, _ = _judge_one(name, spec, value, unit)
        assert len(findings) == 1
        assert findings[0]["type"] == "spec_unverifiable"
        assert findings[0]["severity"] == "warning"
        # CR-23：ocr_text 必须带规格与单位，供复核员对照原页
        assert "spec=" in findings[0]["ocr_text"]

    def test_time_literal_cell(self):
        """真实 p38：pH 列取到「18 时 30 分」。"""
        findings: list[dict] = []
        queue: list[dict] = []
        _judge_cell({"spec": "7~8", "actual": "18 时 30 分", "unit": ""},
                    38, "3", "回流液 pH", "18:30", findings, queue)
        assert len(findings) == 1
        assert findings[0]["type"] == "spec_unverifiable"
        assert "列串位" in findings[0]["description"]

    @pytest.mark.parametrize("name,spec,value", [
        ("本批树脂使用次数", "不超过100次", "417"),
        ("洗脱液纯度", "≥98%", "49.0"),
    ])
    def test_real_deviations_keep_param_out_of_spec(self, name, spec, value):
        findings, _ = _judge_one(name, spec, value)
        assert len(findings) == 1
        assert findings[0]["type"] == "param_out_of_spec"
        assert findings[0]["severity"] == "warning"


class TestTimeParameterRegression:
    """R100 落地时踩过的坑：值槽是时刻 ⇒ 40 余条**正常的时间参数**被误标。

    时间参数（接收开始时间/搅拌结束时间…）本来就该是时刻值，且规格栏为空。
    判据必须先确认"存在可比较的数值规格"，再看值槽形状；顺序反了就会把
    整份记录的时间列全部标成"列串位"（实测 spec_unverifiable 16 → 54）。
    """

    @pytest.mark.parametrize("name,value", [
        ("接收开始时间", "09:00"),
        ("搅拌结束时间", "20时33分"),
        ("V2102b 回流开始时间", "21时11分"),
        ("静置起止时间", "01:01 ~ 01:27"),
    ])
    def test_time_parameter_without_spec_yields_nothing(self, name, value):
        findings, queue = _judge_one(name, "", value)
        assert findings == [], "时间参数被误标成列串位"
        assert len(queue) == 1, "规格为空应进 LLM 兜底队列"

    def test_time_value_against_numeric_spec_is_still_column_shift(self):
        """但规格**是**数值范围时，时刻值仍是列串位 —— 两个方向都要钉住。"""
        findings, _ = _judge_one("回流液 pH", "7~8", "07 时 30 分")
        assert len(findings) == 1 and findings[0]["type"] == "spec_unverifiable"


# ---------------------------------------------------------------------------
# CR-13：规则层拒判 ⇒ 抑制 LLM 同参数断言
# ---------------------------------------------------------------------------


class TestGuardSuppression:
    @staticmethod
    def _llm(desc, ocr="", type_="param_out_of_spec", page=24):
        return {"page": page, "type": type_, "severity": "warning",
                "description": desc, "ocr_text": ocr, "source": "llm_page"}

    def _structured(self):
        # 注意：`drop_unfounded_spec_findings` 收的是**单页**的 structured，
        # 不是 {page: structured}（后者是 stage2/stage3 的分组形式）。
        return {"steps": [{"step_no": 2, "parameters": [
            {"name": "浓缩开始真空度", "spec_range": "≤0.08MPa", "value": "-0.094 MPa"},
            {"name": "浓缩结束真空度", "spec_range": "≤0.08MPa", "value": "-0.096 MPa"},
        ]}]}

    def test_rule_unverifiable_suppresses_llm_assertion(self):
        """真实 p24 的 1424：负表压被 LLM 断言"超差"，规则层判"无法核定"。"""
        f = self._llm("参数超出规格范围：浓缩开始真空度-0.094 MPa > 0.08MPa"
                      "（规格≤0.08MPa）；浓缩结束真空度-0.096 MPa > 0.08MPa"
                      "（规格≤0.08MPa）")
        kept, sup = drop_unfounded_spec_findings([f], self._structured())
        assert kept == []
        assert len(sup) == 1
        assert "无法核定" in sup[0]["reason"]

    def test_rule_compliant_still_suppresses(self):
        """真实 p14 的 1560：37.6°C 就在 40±3°C 内 ⇒ 按"合规"抑制。"""
        structured = {"steps": [{"step_no": 3, "measurements": [
            {"time": "07:31", "values": {"温度": {"spec": "40±3°C", "actual": "37.6"}}},
        ]}]}
        f = self._llm("保温记录中，07:31 温度 37.6°C 超出规格范围 40±3°C (37-43°C)。",
                      page=14)
        kept, sup = drop_unfounded_spec_findings([f], structured)
        assert kept == [] and len(sup) == 1
        assert "合规" in sup[0]["reason"]

    def test_rule_findings_are_never_reviewed(self):
        """规则层是判据来源，不被本模块复核（stage3 整表送进来时尤其重要）。"""
        f = self._llm("第24页 参数 浓缩开始真空度 实测 -0.094MPa 为负值…", page=24)
        f["source"] = "rule"
        kept, sup = drop_unfounded_spec_findings([f], self._structured())
        assert kept == [f] and sup == []

    def test_unknown_still_kept_fail_closed(self):
        """定位不到可比判据 ⇒ 保留（漏检代价高于误报）。"""
        structured = {"steps": [{"step_no": 2, "parameters": [
            {"name": "浓缩液体积", "spec_range": "", "value": "48 L"}]}]}
        f = self._llm("浓缩液体积 48 L 不符合要求")
        kept, sup = drop_unfounded_spec_findings([f], structured)
        assert len(kept) == 1 and sup == []


class TestTripleStateContract:
    """`_triple_state` 必须与 `rule_spec` 的判据同源（两处各写一套就漂移）。"""

    @pytest.mark.parametrize("spec,actual,state", [
        ("≤0", "<0.02%", "unverifiable"),
        ("≤50", "<99ppm", "unverifiable"),
        ("3.0-5.0bar", "46", "unverifiable"),
        ("≤0.08MPa", "-0.090 MPa", "unverifiable"),
        ("7~8", "18 时 30 分", "unverifiable"),
        ("50L", "48 L", "unverifiable"),
        ("0~5°C", "45.6", "out"),
        ("不超过100次", "417", "out"),
        ("40 3°C", "42.1 °C", "in"),
        ("规格见附件", "42.1", "unknown"),
    ])
    def test_states(self, spec, actual, state):
        assert _triple_state(spec, actual) == state

    def test_uses_same_predicates_as_rule_spec(self):
        src = inspect.getsource(_triple_state)
        for fn in ("value_shape_reason", "spec_shape_reason",
                   "bound_plausibility_reason"):
            assert fn in src, f"_triple_state 未复用 {fn} ⇒ 判据已分叉"


# ---------------------------------------------------------------------------
# CR-13 接线：跨页层必须真的接上（否则机制存在但从不生效）
# ---------------------------------------------------------------------------


class TestStage3Wiring:
    def test_stage3_calls_spec_guard(self):
        src = (REPO / "core" / "pipeline" / "stage3.py").read_text(encoding="utf-8")
        assert "drop_unfounded_spec_findings(" in src, (
            "stage3（llm_cross 路径）必须调用规格可判性复核 —— R100 前只有 "
            "stage2 调用，p14 37.6°C 与 p24 1424 正是因此逃逸"
        )
        assert "_spec_sup" in src, "抑制明细必须并入同一张台账"

    def test_stage3_suppression_reaches_ledger(self):
        """抑制明细必须**真的并入**同一张台账（抑制 ≠ 删除）。

        ⚠️ 这里只钉那一行合并语句本身。早先写成
        ``"_g_sup = list(_spec_sup)" in src or "_spec_sup" in src``，
        第二个分支恒真 ⇒ 变异（把合并改回去）根本抓不住。
        """
        src = (REPO / "core" / "pipeline" / "stage3.py").read_text(encoding="utf-8")
        assert "_g_sup = list(_spec_sup) + list(_g_sup or [])" in src, (
            "跨页层抑制明细未并入 _g_sup ⇒ 抑制不写 finding_suppressions，"
            "变成无痕删除"
        )


# ---------------------------------------------------------------------------
# CR-23：finding 必须带可核对原文（值 + 规格 + 单位）
# ---------------------------------------------------------------------------


class TestOcrTextCarriesUnit:
    def test_param_ocr_text_has_unit(self):
        """原文**不自带**单位时，单位必须由构造点补上。

        ⚠️ 不能拿 ``7800L`` 这类原文自带单位的值来测 —— 去掉补单位逻辑它
        照样命中（首版就是这么写的，变异 M15 抓不住）。
        """
        findings, _ = _judge_one("浓缩开始真空度", "≤0.08MPa", "-0.068", "MPa")
        ocr = findings[0]["ocr_text"]
        assert "spec=≤0.08MPa" in ocr and "value=-0.068MPa" in ocr

    def test_cell_ocr_text_has_unit(self):
        findings: list[dict] = []
        _judge_cell({"spec": "3.0-5.0", "actual": "46", "unit": "bar"},
                    8, "3", "进料_压力", "10:38", findings, [])
        assert findings[0]["ocr_text"] == "10:38 进料_压力: spec=3.0-5.0 actual=46bar"

    def test_unit_not_duplicated(self):
        """原文已自带单位（`-0.094 MPa` + unit `MPa`）⇒ 不得变成 `MPaMPa`。"""
        findings, _ = _judge_one("浓缩开始真空度", "≤0.08MPa", "-0.094 MPa", "MPa")
        assert findings[0]["ocr_text"].count("MPa") == 2  # spec 里一个、value 里一个

    def test_frontend_shows_evidence_not_ocr(self):
        js = (REPO / "static" / "review-findings.js").read_text(encoding="utf-8")
        # 只认**模板里的标签**：注释里也写着「证据：」三个字（说明改动理由），
        # 拿 `"证据：" in js` 做断言会被注释骗过去（变异 M16 抓不住）。
        assert '>证据：${esc(f.ocr_text)}</p>' in js, (
            "CR-23：证据行标签应为「证据：」而非「OCR：」"
        )
        assert '>OCR：${' not in js, "CR-23：证据行不得退回实现语「OCR：」"
        assert "ocr_text.slice(0, 100)" not in js, (
            "CR-23：不得静默截断 100 字 —— 规格与单位常落在其后"
        )
        assert 'title="${esc(f.ocr_text)}"' in js, "全量原文必须可悬停查看"
