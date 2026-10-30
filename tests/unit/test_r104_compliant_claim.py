"""R104/P1-6：自证"符合要求"的条目不得入库。

实测反例（`8df2f568` p9，真实语料）
----------------------------------
LLM 产出一条 `self_review`：

    步骤8和步骤9中，操作人'赵阿珍'与复核人'王小雨'不同，符合要求。
    但需注意，第4页中'王小雨'曾作为操作人自我复核，此处角色转换为复核人……

**前半句自己在说"没问题"**，却作为缺陷入库。危害不是"多一条"，
而是复核员会认为**系统把合规项也当缺陷** ⇒ 整条流水线可信度下降。

反向控制（本文件最重要的一半）
----------------------------
1. `"不符合要求"` —— 含"符合要求"，但方向恰好反了 ⇒ 必须保留；
2. `"步骤8符合要求，但步骤9未签名"` —— 后半句才是结论 ⇒ 必须保留；
3. `0c5cfb00` p15 真实反例：**检查项名字里**带"符合要求"
   （「S2103是否符合要求」勾选为"否"）⇒ 必须保留，否则会吞掉一条真缺陷。
   这一条是"剥引号"判据的来源（见 `_QUOTED_RE`）。
"""
from __future__ import annotations

import pytest

from core.finding_quality import is_compliant_claim
from core.rules.llm_finding_guard import review_llm_findings

# 真实语料原文（逐字抄自 `8df2f568` p9）
P9_TEXT = (
    "步骤8和步骤9中，操作人'赵阿珍'与复核人'王小雨'不同，符合要求。"
    "但需注意，第4页中'王小雨'曾作为操作人自我复核，此处角色转换为复核人，"
    "需确保其复核独立性。"
)
# 真实语料原文（`0c5cfb00` p15）—— 检查项名里带"符合要求"
P15_TEXT = (
    "第15页 检查项「S2103是否符合要求」勾选为“否”（◇），"
    "需确认是否已启动偏差处理并在记录中留痕"
)
# 用户表扬过的那条（`8df2f568` p2）—— 含"正常"，但不是在说本条合规。
# R133/TODO 0-76：文案已删掉「否定式提问的"否"通常即正常答案」这句
# 极性推断（题面极性无可靠判据，会把复核人往反方向引），只陈述事实 + 请人工核对。
P2_PRAISED = (
    '第2页 检查项「电子台秤(A-FW01-307-4005)上一次清场是否在有效期内」勾选为“否”（☑），'
    '请对照 PDF 原页确认该答案是否为预期'
)


class TestPredicate:
    def test_real_self_negating_finding_is_detected(self):
        assert is_compliant_claim(P9_TEXT) is True

    @pytest.mark.parametrize("text", [
        "该栏填写不符合要求。",          # 含「符合要求」，方向恰好反了
        "参数值为否，不符合规格范围。",  # 连「符合要求」都不含
    ])
    def test_negated_form_is_not_compliant(self, text):
        """`不符合要求` 含 `符合要求` —— 少列 `不符合` 就会把"不合规"读成"合规"。"""
        assert is_compliant_claim(text) is False

    def test_defect_after_contrast_is_kept(self):
        assert is_compliant_claim("步骤8符合要求，但步骤9未签名。") is False

    def test_compliance_word_inside_quoted_check_item_is_not_conclusion(self):
        assert is_compliant_claim(P15_TEXT) is False, (
            "检查项名里的「符合要求」是被引用的名称，不是本条结论"
        )

    def test_praised_checkbox_finding_is_kept(self):
        assert is_compliant_claim(P2_PRAISED) is False

    @pytest.mark.parametrize("text", ["", "   ", None])
    def test_empty_is_not_compliant(self, text):
        assert is_compliant_claim(text) is False

    def test_plain_compliance_statement(self):
        assert is_compliant_claim("所有检查项均正常，符合要求。") is True


class TestGuardWiring:
    """守卫链必须真的抑制它，并留痕（GMP 可追溯"为什么少了一条"）。"""

    def test_suppressed_with_l0_evidence(self):
        f = [{
            "page": 9, "type": "self_review", "severity": "warning",
            "source": "llm_cross", "description": P9_TEXT,
        }]
        kept, _down, suppressed = review_llm_findings(f)
        assert kept == []
        assert len(suppressed) == 1
        assert suppressed[0]["evidence"]["guard_layer"] == "L0"
        assert "合规" in suppressed[0]["reason"]

    def test_real_defect_survives_the_guard(self):
        f = [{
            "page": 9, "type": "completeness", "severity": "warning",
            "source": "llm_page", "description": "步骤8符合要求，但步骤9未签名。",
        }]
        kept, _down, suppressed = review_llm_findings(f)
        assert len(kept) == 1 and suppressed == []

    def test_applies_to_user_rule_too(self):
        """`user_rule` 同样是 LLM 产出（`llm_checks` 沿用模型给的 type）。"""
        f = [{
            "page": 3, "type": "user_rule", "severity": "critical",
            "source": "user_rule", "description": "该栏内容均正常，符合要求。",
        }]
        kept, _down, suppressed = review_llm_findings(f)
        assert kept == []
        assert len(suppressed) == 1
