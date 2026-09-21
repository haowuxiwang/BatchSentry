"""B1-12 布尔/勾选类处置**统一类型闸门**。

定位（Round 53，原页核验）
--------------------------
三条同源假阳性的真实文案：

| 页 | 文案 | 旧处置 |
|---|---|---|
| p39 | `…参数值为否，不符合规格范围` | L1 **抑制** |
| p46 | `清洗过程是否无异常参数值为否，超出规格范围` | L1 **抑制** |
| p45 | `步骤 2 中是否将柱内甲醇压干 勾选了否，不符合操作指导` | 仅**降级** |

**旧记载的根因不准**（本轮实测更正）：p45 的 fail-open **不是** `_NOT_A_VALUE` 里
"不符合操作指导"造成的 —— `_NOT_A_VALUE` 是拿**取到的值**去比，而 p45 的文案里
**根本没有 `值` 字**，`_BARE_VALUE_RE`（认「…值为 X」）压根匹配不上 ⇒ 取不到值 ⇒
fail-open。真正的原因是**取值器只认一种措辞**，于是处置档位由动词决定。

**原页核验结论**（渲染 p39/p45/p46 原页）：
* p39 `物料堆放/器具排放/地面是否干净` 三行均为 **`√ 是 / □ 否`**；
* p45 `是否将柱内甲醇压干` 为 **`√ 是 / □ 否`**；
* p46 工序 5/6 全部 **`√ 是 / □ 否`**。
⇒ 三页真值都是**「是」**，三处"否"主张**全是 LLM 读反**（假阳性）——
本就该得到**同一种**处置。

本文件钉住的不变式
------------------
1. 三种措辞 ⇒ **同一层、同一文案**（档位与措辞解耦）；
2. 闸门是**类型**闸门，不是"找否"闸门 —— 断言 `是` 也一样处理；
3. **不误伤**：含疑问词「是否」的**数值**文案不得被判成布尔；
4. `guard_layer` 契约：理由文本必须让 `_layer_of` 归到 `L1-value-shape`。
"""

from __future__ import annotations

import pytest

from core.rules.llm_finding_guard import (
    _check_spec_value_shape,
    _declared_boolean_literal,
    review_llm_findings,
)

# 三条**真实**文案（逐字取自 job 6f80145a-47f 的 findings 表）
REAL_CASES = [
    (39, "步骤 6 中的器具排放是否整齐、地面是否干净、场地是否清理、"
         "设备及管路是否及时清洗干净参数值为否，不符合规格范围", "否"),
    (45, "步骤 2 中是否将柱内甲醇压干 勾选了否，不符合操作指导", "1☐ 否"),
    (46, "清洗过程是否无异常参数值为否，超出规格范围", "否"),
]


def _f(page, desc, ocr, ftype="param_out_of_spec", sev="critical"):
    return {"page": page, "type": ftype, "severity": sev,
            "description": desc, "ocr_text": ocr, "source": "llm_page"}


class TestUniformDisposition:
    """核心验收：同一件事必须得到同一种处置。"""

    def test_all_three_phrasings_are_suppressed_identically(self):
        findings = [_f(p, d, o) for p, d, o in REAL_CASES]
        kept, down, sup = review_llm_findings(
            findings, structured_by_page={}, raw_by_page={},
        )
        assert kept == []
        assert down == [], f"仍有条目被降级（档位由措辞决定）：{down}"
        assert len(sup) == 3
        assert {s["evidence"]["guard_layer"] for s in sup} == {"L1-value-shape"}
        # 文案必须**逐字相同**（只差被点名的字面量）
        reasons = {s["reason"] for s in sup}
        assert len(reasons) == 1, f"同一件事给出了不同理由：{reasons}"

    @pytest.mark.parametrize("page,desc,ocr", REAL_CASES)
    def test_each_phrasing_is_recognised(self, page, desc, ocr):
        assert _declared_boolean_literal(_f(page, desc, ocr)) == "否"

    def test_yes_claim_is_treated_the_same_way(self):
        """闸门是**类型**闸门：断言「是」同样不构成数值超差。"""
        assert _declared_boolean_literal(
            _f(1, "步骤 2 中是否将柱内甲醇压干 勾选了是，超出规格范围", "")
        ) == "是"


class TestBooleanLiteralRecogniser:
    @pytest.mark.parametrize("text,expect", [
        ("实际值为否，不符合规格范围", "否"),
        ("参数值为是", "是"),
        ("勾选了否，不符合操作指导", "否"),
        ("勾选为“否”", "否"),
        ("标记为是", "是"),
        ("选中了否", "否"),
        ("填写为是", "是"),
    ], ids=["val-no", "val-yes", "tick-no", "tick-quoted-no",
            "mark-yes", "pick-no", "fill-yes"])
    def test_recognised_phrasings(self, text, expect):
        assert _declared_boolean_literal(_f(1, text, "")) == expect

    @pytest.mark.parametrize("text", [
        # 疑问词「是否」本身不是主张（否则任何"是否…"都会被误判）
        "步骤 2 中是否将柱内甲醇压干，氮气压力 0.16 MPa 超出规格范围",
        "清洗过程是否无异常，实测值 7.49 未在规格范围内",
        # 「勾选是否正确」是评价"勾选动作"，不是断言勾选**结果** —— 动词后紧跟的
        # 「是」属于疑问词，必须靠负向预查排除
        "记录中勾选是否正确无法确认",
        # 字母数值 / 评价词不是布尔
        "13:18 时 P3 (MPa) 的实际值为 'A'，不符合规格范围",
        "T2101a~d 压力值超出规格范围",
    ], ids=["question-time", "question-result", "question-tick",
            "letter-value", "evaluative-phrase"])
    def test_not_recognised(self, text):
        assert _declared_boolean_literal(_f(1, text, "")) is None


class TestNoCollateralDamage:
    """不误伤：布尔闸门不得吃掉真数值形态的条目（fail-open 对照）。"""

    @pytest.mark.parametrize("text", [
        "步骤 2 中是否将柱内甲醇压干，氮气压力 0.16 MPa 超出规格范围",
        "清洗液 pH7~8，实际值为 9.2，不符合规格范围",
        "T2101a~d 压力值超出规格范围",
    ])
    def test_numeric_claims_fall_through(self, text):
        """数值形态的条目不得被判成布尔 —— 闸门返回 None，交由后续判据。"""
        assert _check_spec_value_shape(_f(1, text, ""), None) is None

    def test_non_spec_type_is_untouched(self):
        """布尔闸门只作用于 spec 类型；completeness 等不看值形态。"""
        assert _check_spec_value_shape(
            _f(1, "勾选了否", "", ftype="completeness", sev="warning"), None
        ) is None
