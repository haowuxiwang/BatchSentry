"""R108 / P1-5：签名是否要带日期，由**表头**决定，不由常识决定。

用户 2026-10-26 第 12 条裁定：表头写"签名"⇒ 不要求日期；表头写"签名+日期"
⇒ 两者必须都有。

实测依据（真实语料表头统计）：

    104 张表   「序号 | 岗位操作指导 | 操作参数 | 记录数据 | 签名」
    4 + 4 张表 「步骤 | 执行人签名和日期 | 检查人签名和日期」

前者签名格里就是「操作者郑旭 复核者吴伟胜」，**没有日期格可填**。系统却报
「所有签名均无日期，无法进行时间逻辑检查」—— 表单没设这一栏，记录方无从填起。
真实语料里这类无据断言 **29 条**。
"""
from __future__ import annotations


from core.rules.llm_finding_guard import _layer_of, apply_review
from core.signature_date_need import (
    claims_missing_signature_date,
    is_pure_date_claim,
    signature_date_required,
    unfounded_reason,
)

#: 真实形态：表头最后一列只写「签名」（104 张表都是这样）。
SIG_ONLY = (
    "<table>"
    "<tr><td>序号</td><td>岗位操作指导</td><td>记录数据</td><td>签名</td></tr>"
    "<tr><td>1</td><td>已确认生产场地整洁</td><td>☑是</td><td>操作者郑旭 复核者吴伟胜</td></tr>"
    "<tr><td>2</td><td>缓冲液配制</td><td>3.0 m^3</td><td>操作者郑旭 复核者吴伟胜</td></tr>"
    "</table>"
)

#: 真实形态：表头把"签名和日期"写在同一格（8 张表）。
SIG_DATE = (
    "<table>"
    "<tr><td>步骤</td><td>执行人签名和日期</td><td>检查人签名和日期</td></tr>"
    "<tr><td>1</td><td>赵阿珍 2025.06.03</td><td>王小雨 2025.06.03</td></tr>"
    "</table>"
)

#: 签名列 + 独立的日期列（语料里没有，但现实表单常见，判据必须认）。
SIG_PLUS_DATE_COL = (
    "<table>"
    "<tr><td>步骤</td><td>签名</td><td>日期</td></tr>"
    "<tr><td>1</td><td>赵阿珍</td><td>2025.06.03</td></tr>"
    "</table>"
)

CLAIM = "所有签名均无日期，无法进行签名时间逻辑检查。"


class TestHeaderVerdict:
    def test_signature_only_header_does_not_require_date(self):
        assert signature_date_required(SIG_ONLY) is False

    def test_combined_signature_and_date_header_requires_date(self):
        assert signature_date_required(SIG_DATE) is True

    def test_separate_date_column_requires_date(self):
        assert signature_date_required(SIG_PLUS_DATE_COL) is True

    def test_no_signature_header_abstains(self):
        """看不到签名列表头 ⇒ 弃权，**不是**"不要求日期"。"""
        assert signature_date_required("<table><tr><td>序号</td><td>参数</td></tr></table>") is None
        assert signature_date_required("") is None
        assert signature_date_required(None) is None

    def test_time_column_alone_is_not_a_date_column(self):
        """「水洗开始时间」是工序时刻，与签名要不要写日期无关 —— 别把它当日期列。

        实测 `e50e9a7e` p11 就有一排 `时间` 子表头；把它算成日期列会让判据
        几乎永不触发（等于没修）。
        """
        html = (
            "<table>"
            "<tr><td>序号</td><td>记录数据</td><td>签名</td></tr>"
            "<tr><td>1</td><td>水洗开始时间 18日09时07分</td><td>操作者吴伟平</td></tr>"
            "</table>"
        )
        assert signature_date_required(html) is False


class TestClaimRecognition:
    def test_plain_missing_date_claim(self):
        assert claims_missing_signature_date(CLAIM) is True

    def test_rule_layer_time_claim_is_not_a_date_claim(self):
        """真实规则层文案：「已记录操作**时间**但**缺少**操作者签名」。

        说的是签名没签（真缺陷），不是日期没写。放宽成"日期…6 字内…缺失"
        会把它一起吞掉 —— 这是第一版实现撞出来的假抑制。
        """
        assert claims_missing_signature_date(
            "第24页 工序2 浓缩：已记录操作时间但缺少操作者签名"
        ) is False

    def test_missing_name_with_date_present_is_not_a_date_claim(self):
        """真实文案：「签名字段 '负责人：2015.07.21' 中姓名缺失，仅识别出日期」。"""
        assert claims_missing_signature_date(
            "签名字段 '负责人：2015.07.21' 中姓名缺失，仅识别出日期。"
        ) is False


class TestPureDateClaim:
    def test_all_signatures_no_date_is_pure(self):
        assert is_pure_date_claim(CLAIM) is True

    def test_mixed_with_missing_signature_is_not_pure(self):
        """「步骤6缺少复核者签名」是另一回事，不能连坐抑制。"""
        assert is_pure_date_claim(
            "步骤5和步骤7的签名缺少日期；步骤6缺少复核者签名。"
        ) is False

    def test_mixed_with_name_recognition_is_not_pure(self):
        assert is_pure_date_claim(
            "签名信息不完整：多个签名仅识别出姓氏（如'张'、'管'），且所有签名均无日期。"
        ) is False

    def test_empty_is_not_pure(self):
        assert is_pure_date_claim("") is False
        assert is_pure_date_claim(None) is False


class TestUnfoundedReason:
    def test_signature_only_page_is_unfounded(self):
        assert unfounded_reason(CLAIM, SIG_ONLY) is not None

    def test_signature_date_page_is_kept(self):
        assert unfounded_reason(CLAIM, SIG_DATE) is None

    def test_unknown_header_is_kept(self):
        assert unfounded_reason(CLAIM, "") is None

    def test_mixed_claim_is_kept(self):
        assert unfounded_reason(
            "步骤5和步骤7的签名缺少日期；步骤6缺少复核者签名。", SIG_ONLY
        ) is None

    def test_rule_source_is_never_suppressed(self):
        """规则层是确定性判据，它的结论不得被系统自己吞掉（R104/R105 同一条边界）。"""
        assert unfounded_reason(CLAIM, SIG_ONLY, source="rule") is None
        assert unfounded_reason(CLAIM, SIG_ONLY, source="user_rule") is None
        assert unfounded_reason(CLAIM, SIG_ONLY, source="llm_page") is not None


def _f(desc: str = CLAIM, **kw) -> dict:
    base = {"page": 2, "type": "completeness", "severity": "warning",
            "source": "llm_page", "description": desc}
    base.update(kw)
    return base


class TestGuardWiring:
    def test_suppressed_with_audit_trail(self):
        out, sup = apply_review([_f()], structured_by_page={2: {}}, raw_by_page={2: SIG_ONLY})
        assert out == []
        assert len(sup) == 1
        assert "签名栏表头" in sup[0]["reason"]
        assert sup[0]["evidence"]["guard_layer"] == "L1-header-date-rule"

    def test_kept_when_header_requires_date(self):
        out, sup = apply_review([_f()], structured_by_page={2: {}}, raw_by_page={2: SIG_DATE})
        assert len(out) == 1 and sup == []

    def test_kept_when_claim_is_mixed(self):
        out, _ = apply_review(
            [_f("步骤5和步骤7的签名缺少日期；步骤6缺少复核者签名。")],
            structured_by_page={2: {}}, raw_by_page={2: SIG_ONLY},
        )
        assert len(out) == 1

    def test_rule_layer_finding_is_untouched(self):
        out, sup = apply_review(
            [_f(source="rule")], structured_by_page={2: {}}, raw_by_page={2: SIG_ONLY}
        )
        assert len(out) == 1 and sup == []

    def test_layer_tag_is_unique(self):
        assert _layer_of("该页签名栏表头只写「签名」…") == "L1-header-date-rule"
