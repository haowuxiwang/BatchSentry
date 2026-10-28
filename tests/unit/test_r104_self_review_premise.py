"""R104 / P1-4：LLM 的"同一人复核"断言不得建立在规则层已判为不可靠的读数上。

事故（实测 `8df2f568` p4）：

| # | source | type | 文案 |
|---|---|---|---|
| 1654 | rule | `signature_unreadable` | 工序8、9、10 操作人与复核人**签名辨认不可靠**…**无法排除**同一人复核，请核对原页笔迹 |
| 1655 | llm_cross | `self_review` | 操作人'王小雨'与复核人'王小雨'**为同一人**，违反职责分离 |

**同一条不可靠的 OCR 读数，一边说不可判、一边当铁证**。CR-4（R98）已经把
规则层收敛成"只发 info + 请复核员看原页"，但 LLM 没有跟着收敛 —— 于是复核员
看到系统自相矛盾，且被 warning 级的硬断言误导。

判据是**页级**的：规则层判"整页没有一对可靠签名"时，该页上任何"同一人"断言
都建立在同一份不可靠读数上 ⇒ 抑制 LLM 那条，保留规则层的 info（它才是诚实的）。
"""
from __future__ import annotations

import pytest

from core.pipeline.stage3 import (
    _is_unfounded_self_review,
    _unreadable_signature_pages,
)


def _rule(page: int, ftype: str = "signature_unreadable", **kw) -> dict:
    return {"page": page, "type": ftype, "source": "rule", **kw}


def _llm(page: int, ftype: str = "self_review", src: str = "llm_cross") -> dict:
    return {"page": page, "type": ftype, "source": src}


class TestUnreadablePages:
    """哪些页算"规则层已判签名不可辨" —— 口径只能来自规则层。"""

    def test_rule_unreadable_marks_the_page(self):
        assert _unreadable_signature_pages([_rule(4)]) == {4}

    def test_llm_unreadable_does_not_mark_the_page(self):
        """LLM 自己说"读不清"不算判据 —— 它没有置信度模型，随口一说就把
        同页的真缺陷放行了。"""
        assert _unreadable_signature_pages(
            [{"page": 4, "type": "signature_unreadable", "source": "llm_page"}]
        ) == set()

    def test_other_rule_types_do_not_mark_the_page(self):
        """`signature_mismatch` 是"读清了但两个人不同" —— 前提成立，不得拦。"""
        assert _unreadable_signature_pages(
            [_rule(4, "signature_mismatch"), _rule(4, "completeness")]
        ) == set()

    def test_type_match_is_case_insensitive(self):
        """规则层写的是字面量规范值；这里只做大小写无关比较。

        ⚠️ 刻意**不调** `normalize_finding_type` —— `test_type_sync.py` 的
        「归一某集合 ⇒ 必须先富集」护栏会把任何对 `findings` 的归一当成改写了
        集合（本函数只读不写）。见 `_unreadable_signature_pages` 的 docstring。
        """
        assert _unreadable_signature_pages([_rule(7, "SIGNATURE_UNREADABLE")]) == {7}

    def test_non_dict_entries_are_ignored(self):
        assert _unreadable_signature_pages([None, "x", _rule(5)]) == {5}

    def test_pages_are_collected_across_entries(self):
        assert _unreadable_signature_pages(
            [_rule(4), _rule(4), _rule(9), _llm(12)]
        ) == {4, 9}


class TestPredicate:
    """抑制判据本身 —— 每条都配反向控制。"""

    def test_llm_cross_self_review_on_unreadable_page_is_suppressed(self):
        assert _is_unfounded_self_review(4, "self_review", "llm_cross", {4}) is True

    def test_llm_fallback_covered_too(self):
        assert _is_unfounded_self_review(4, "self_review", "llm_fallback", {4}) is True

    def test_other_types_on_the_same_page_are_kept(self):
        """同页的"姓名不符""签名缺失"不依赖"读得准"这个前提 ⇒ 不得连带抑制。"""
        for ftype in ("signature_mismatch", "completeness", "param_out_of_spec"):
            assert _is_unfounded_self_review(4, ftype, "llm_cross", {4}) is False

    def test_self_review_on_a_different_page_is_kept(self):
        assert _is_unfounded_self_review(9, "self_review", "llm_cross", {4}) is False

    def test_rule_and_user_rule_are_exempt(self):
        """规则层与用户规则是判据来源，不复核（与 `_DEDUP_SOURCES` 同一份口径）。"""
        assert _is_unfounded_self_review(4, "self_review", "rule", {4}) is False
        assert _is_unfounded_self_review(4, "self_review", "user_rule", {4}) is False

    def test_llm_page_is_out_of_scope_by_design(self):
        """⚠️ **已知边界**：`llm_page` 不在覆盖范围。

        页级 findings 在 Stage 2 就落库了，那时规则层还没跑（规则在 Stage 3 的
        `analyze_cross_page` 里才产出）⇒ 这里根本看不到那些行。要覆盖它得在
        Stage 3 回头改已落库的行，而 `findings` 表没有"已抑制"状态、只有
        `finding_suppressions` 台账 —— 那是一次新的写路径，不能顺手加。

        781 条真实 finding 里**没有**一例 llm_page 版（实测：带
        `signature_unreadable` 的页只有 p4，其 self_review 是 llm_cross）。
        真出现了再单独立项，别把这条边界当"已覆盖"。
        """
        assert _is_unfounded_self_review(4, "self_review", "llm_page", {4}) is False


class TestRealCorpusPair:
    """把 p4 的真实一对还原成断言 —— 这是本批要修的那一条。"""

    RULE = {
        "page": 4, "type": "signature_unreadable", "source": "rule",
        "severity": "info",
        "description": (
            "第4页 工序8、9、10 操作人与复核人签名辨认不可靠"
            "（原始识别：操作人「王小雨」/ 复核人「王小雨」），"
            "无法排除同一人复核，请核对原页笔迹"
        ),
    }
    LLM = {
        "page": 4, "type": "self_review", "source": "llm_cross",
        "severity": "warning",
        "description": (
            "步骤8和步骤9中，操作人'王小雨'与复核人'王小雨'为同一人，"
            "违反了操作与复核职责分离的原则。"
        ),
    }

    def test_llm_assertion_is_suppressed_rule_info_kept(self):
        findings = [self.RULE, self.LLM]
        pages = _unreadable_signature_pages(findings)
        assert pages == {4}
        assert _is_unfounded_self_review(
            self.LLM["page"], self.LLM["type"], self.LLM["source"], pages,
        ) is True
        # 规则层那条**必须留下** —— 它说的是"请核对原页笔迹"，正是复核员要做的
        assert _is_unfounded_self_review(
            self.RULE["page"], self.RULE["type"], self.RULE["source"], pages,
        ) is False

    def test_a_solid_rule_self_review_is_not_swept_away(self):
        """规则层有可靠签名时发的是 `self_review`（critical），此时它自己
        就进了 `rule_covered` 精确去重 —— 本判据不该再插手。"""
        solid = _rule(4, "self_review")
        pages = _unreadable_signature_pages([solid])
        assert pages == set()
        assert _is_unfounded_self_review(
            4, "self_review", "llm_cross", pages,
        ) is False


@pytest.mark.parametrize(
    "ftype,src,page,expect",
    [
        ("self_review", "llm_cross", 4, True),
        ("self_review", "llm_page", 4, False),  # 边界，见 TestPredicate 的说明
        ("self_review", "user_rule", 4, False),
        ("self_review", "llm_cross", 5, False),
        ("signature_mismatch", "llm_cross", 4, False),
    ],
)
def test_predicate_matrix(ftype, src, page, expect):
    assert _is_unfounded_self_review(page, ftype, src, {4}) is expect
