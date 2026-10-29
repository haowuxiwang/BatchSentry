"""R95 —— 用户 2026-10-21 手工标注（《试用批记录.pdf》10 页）的回归护栏。

## 这组用例是什么 / 不是什么

**是**：10 条**具体误报/漏报**的复现。每条用例的 docstring 都写着用户原话，
样本字段取自那次真实运行的结构化数据（`%APPDATA%\\PBC\\data.db`，
job `4cffb358-3b3`，10 页 / 47 findings），只保留复现所需的最小结构。

**不是**：精确率/召回率达标证明。真实标注集只有 10 条、且只覆盖一份文档，
任何 P/R 数字都不具统计意义（见 MEMORY §12「未验证边界」）。

## 标注编号（用户原话 → 本文件）

| # | 用户原话 | 判据落点 |
|---|---|---|
| A1 | "时间一致没有问题，但如果 QA 批准的时间早于执行人/检查人日期就不可以" | `TestSameDayIsNotReversal` |
| A2 | "为什么批号不一样，标题会是参数超标？" | `TestTypeMatchesContent` |
| A3 | "时间一致，是没有问题的，检查人和复核人日期都是同一天" | `TestSameDayIsNotReversal` |
| A4 | "这里有问题"（同一栏两次签名被读成两个名字） | `TestNameVariantIsNotADifferentPerson` |
| A5 | "执行人都是王小雨，检查人都是沈雅晨" | `TestSelfReviewAggregation` |
| A6 | "湿度要求范围是 24-65，湿度 35 是没有问题的" | `TestTypeMatchesContent` |
| A7 | "这里的执行人和检查人都是有签字的，我们是 OCR 漏识别了？" | `TestSignatureSlotCompleteness` |
| A8 | "复核人以及日期没有写，但是我们没有提示" | `TestSignatureSlotCompleteness` |
| A9 | "这里问题有点多"（同一缺陷多源多类型重复） | `TestSelfReviewAggregation` |
| A10 | "他们有签名和日期" | `TestSignatureSlotCompleteness` |
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.finding_quality import normalize_finding, type_from_description
from core.pipeline.stage3 import _overlap_covered_by
from core.rules.llm_checks import _llm_based_check, _system_prompt
from core.rules.llm_finding_guard import review_llm_findings
from core.rules.rule_doc import _check_completeness
from core.rules.rule_gmp import _check_self_review


def _page(pno: int, steps: list[dict]) -> dict:
    return {"page": pno, "steps": steps}


def _sig(role: str, name: str, time: str = "2025.06.03", conf: str = "low") -> dict:
    return {"role": role, "name": name, "sign_time": time, "confidence": conf}


# ===========================================================================
# A7 / A8 / A10 —— 签名槽位的"有/无"判定
# ===========================================================================

#: p8 实测（第二张环境监测表的复核人栏确实空白，但同一 step 里第一张表
#: 有「陈一」⇒ 旧的布尔判据看不见这个空槽）。
P8_STEPS = [{
    "step_no": "", "operation": "生产前检查与称量环境确认",
    "start_time": "10:23", "end_time": "21:00",
    "signatures": [
        _sig("checker", "熊乃可了", "2025.06.30"),
        _sig("reviewer", "陈一", "2025.06.30"),
        _sig("checker", "张珂", ""),
        _sig("reviewer", "", "2015.6.03"),
    ],
}]

#: p6 实测：执行人/检查人栏有手写签名，PaddleOCR-VL 整列漏识别 ⇒ 只有 QA 一条。
P6_STEPS = [{
    "step_no": "", "operation": "称量岗位清场",
    "start_time": "23:10", "end_time": "23:52",
    "signatures": [_sig("qa_approver", "孙文莉", "2025.05.20")],
}]


class TestSignatureSlotCompleteness:
    """A7/A8/A10 —— 空槽位要报，但**不许把"没识别到"写成"记录没签"。"""

    def test_blank_name_is_not_a_signature(self):
        """只有一个姓名为空的 reviewer ⇒ 必须判为"没有复核人"（A8 的根因）。"""
        steps = [{"step_no": "1", "start_time": "10:00", "end_time": "11:00",
                  "signatures": [_sig("reviewer", "")]}]
        got = _check_completeness([_page(8, steps)])
        assert any("未识别到复核人签名" in f["description"] for f in got), got

    def test_blank_slot_alongside_signed_reviewer_is_reported(self):
        """同一 step 里"有人签 + 有空槽" ⇒ 补报逐行核对（A8 真正命中的形态）。"""
        got = _check_completeness([_page(8, P8_STEPS)])
        blanks = [f for f in got if "复核人签名栏" in f["description"]]
        assert len(blanks) == 1, got
        assert "1 处" in blanks[0]["description"]
        assert blanks[0]["severity"] == "warning"

    def test_signed_reviewer_slot_is_quiet(self):
        """反例：全部签了 ⇒ 不许报（防"一律报警"式变异）。"""
        steps = [{"step_no": "1", "start_time": "10:00", "end_time": "11:00",
                  "signatures": [_sig("operator", "甲"), _sig("reviewer", "乙"),
                                 _sig("qa", "丙")]}]
        got = _check_completeness([_page(1, steps)])
        assert not [f for f in got if "签名" in f["description"]], got

    def test_missing_signature_copy_attributes_to_ocr(self):
        """A7：p6 的签字被整列漏识别 —— 文案必须说"未识别到"并指明可能是 OCR。"""
        got = _check_completeness([_page(6, P6_STEPS)])
        ops = [f for f in got if "操作人签名" in f["description"]]
        assert ops, got
        assert "未识别到" in ops[0]["description"]
        assert "OCR 漏识别" in ops[0]["description"]

    def test_old_wording_is_gone(self):
        """旧文案「缺少复核人签名」把 OCR 问题说成记录缺陷，不得回归。"""
        got = _check_completeness([_page(6, P6_STEPS)])
        assert not any("缺少复核人签名" in f["description"] for f in got), got

    # ── R112 / P8：**没有执行时间**的步骤也要查签名空槽 ──────────────────

    def test_blank_slot_is_reported_even_without_execution_time(self):
        """R112 的真实形态：p8 那张表整行**没写时间**，复核人栏空白。

        ⚠️ 上面 `test_blank_slot_alongside_signed_reviewer_is_reported` 用的是
        带 `start_time` 的 P8_STEPS，走的是"有时间"分支 ⇒ 一直是绿的，
        而用户看到的 p8 根本没时间，走的是另一条 `continue`。这就是
        "测试绿、线上漏"的经典形态：同一个缺陷的两条路径只测了一条。

        修法：`_check_completeness` 在"无时间"分支不再整段 `continue`，
        补跑 `_blank_slot_findings`（只补空槽，不补"整类缺失"—— 后者依赖
        "这一步已执行"这个前提，标题行会因此凭空多出噪声）。
        """
        steps = [{
            "step_no": "", "operation": "生产前检查与称量环境确认",
            "start_time": "", "end_time": "",
            "signatures": [
                _sig("checker", "熊乃可了", "2025.06.30"),
                _sig("reviewer", "陈一", "2025.06.30"),
                _sig("checker", "张珂", ""),
                _sig("reviewer", "", "2015.6.03"),
            ],
        }]
        got = _check_completeness([_page(8, steps)])
        blanks = [f for f in got if "复核人签名栏" in f["description"]]
        assert len(blanks) == 1, got
        assert "1 处" in blanks[0]["description"]
        assert blanks[0]["severity"] == "warning"

    def test_no_time_step_still_reports_missing_time(self):
        """反向控制：补了签名检查**不能**把"缺执行时间"那条顶掉。"""
        steps = [{"step_no": "3", "operation": "称量",
                  "start_time": "", "end_time": "",
                  "signatures": [_sig("operator", "甲"), _sig("reviewer", "乙")]}]
        got = _check_completeness([_page(3, steps)])
        assert any("缺少执行时间" in f["description"] for f in got), got
        assert not [f for f in got if "签名栏为空" in f["description"]], got

    def test_title_row_without_time_or_sigs_is_silent(self):
        """反向控制：封面/目录/纯标题行（无 operation、无签名）⇒ 一条都不报。

        这条钉的是 R112 放宽后的边界：条件从 `if not has_operation` 改成
        `if not has_operation and not sigs`，放宽的只是"有签名但没时间"的
        步骤；两者都没有的行仍然跳过。
        """
        steps = [{"step_no": "二", "operation": "", "start_time": "",
                  "end_time": "", "signatures": []}]
        got = _check_completeness([_page(1, steps)])
        assert got == [], got


# ===========================================================================
# A5 / A9 —— 自核自检：聚合 + 归因
# ===========================================================================

def _self_review_steps(conf: str = "high") -> list[dict]:
    """默认 `high`：R98/CR-4 起，`low` 走 `signature_unreadable`（info）而非 critical。

    旧默认是 `low`（当时只加一句"可能是误读"的注，severity 仍 critical）；
    CR-4 把"低置信"从**注**提升为**结论类型**，默认值随之改到正常态。
    """
    return [{"step_no": n,
             "signatures": [_sig("operator", "王小雨", conf=conf),
                            _sig("reviewer", "王小雨", conf=conf)]}
            for n in ("8", "9", "10")]


class TestSelfReviewAggregation:
    """A5/A9 —— 同一对签名在三个工序各报一条，是复核页噪声的主要来源。"""

    def test_same_person_reported_once_per_page(self):
        got = _check_self_review([_page(4, _self_review_steps())])
        assert len(got) == 1, [f["description"] for f in got]
        assert "工序8、9、10" in got[0]["description"]

    def test_low_confidence_downgrades_to_signature_unreadable(self):
        """CR-4：低置信 ⇒ 不出 self_review critical，改出 signature_unreadable info。

        事实依据：真实 p4 的「同一人『王小雨』」是 OCR 把另一位草书签名读成了
        执行人的名字（逐页核图）。低置信时「真同一人」与「读成同一人」无法区分。
        """
        got = _check_self_review([_page(4, _self_review_steps("low"))])
        assert len(got) == 1, [f["description"] for f in got]
        assert got[0]["type"] == "signature_unreadable"
        assert got[0]["severity"] == "info"
        assert not any(f["type"] == "self_review" for f in got), (
            "低置信不得再产出 self_review（critical）")

    def test_low_confidence_prints_both_raw_strings(self):
        """CR-4 ③：文案必须**同时印出**两个原始签名串（只引用，不断言）。"""
        steps = [{"step_no": "8",
                  "signatures": [_sig("operator", "王小雨", conf="low"),
                                 _sig("reviewer", "王小雨", conf="low")]}]
        got = _check_self_review([_page(4, steps)])
        desc = got[0]["description"]
        assert "操作人「王小雨」" in desc
        assert "复核人「王小雨」" in desc
        assert "核对原页笔迹" in desc

    def test_low_confidence_does_not_assert_a_person(self):
        """CR-4：`operator` 必须留空 —— 不得把低置信的姓名当成"被归因的人"。"""
        got = _check_self_review([_page(4, _self_review_steps("low"))])
        assert got[0]["operator"] == ""

    def test_one_side_low_is_enough_to_downgrade(self):
        """任一侧低置信即不可判 —— 门槛看"两侧都不低"，不是"有一侧不低"。"""
        steps = [{"step_no": "8",
                  "signatures": [_sig("operator", "王小雨", conf="high"),
                                 _sig("reviewer", "王小雨", conf="low")]}]
        got = _check_self_review([_page(4, steps)])
        assert [f["type"] for f in got] == ["signature_unreadable"]

    def test_one_reliable_step_keeps_critical(self):
        """聚合后共用一条结论 ⇒ 门槛是"有没有一对可靠证据"。

        同页工序 8 低置信、工序 9 高置信 ⇒ 工序 9 这对是硬证据，
        不得因为 8 是低置信就把整条降成 info（那会丢掉真缺陷）。
        """
        steps = [
            {"step_no": "8",
             "signatures": [_sig("operator", "王小雨", conf="low"),
                            _sig("reviewer", "王小雨", conf="low")]},
            {"step_no": "9",
             "signatures": [_sig("operator", "王小雨", conf="high"),
                            _sig("reviewer", "王小雨", conf="high")]},
        ]
        got = _check_self_review([_page(4, steps)])
        assert [f["type"] for f in got] == ["self_review"]
        assert got[0]["severity"] == "critical"

    def test_high_confidence_has_no_caveat(self):
        """反例：签名置信度不低时不得挂"可能是误读"的免责话（会稀释真缺陷）。"""
        got = _check_self_review([_page(4, _self_review_steps("high"))])
        assert "OCR 置信度低" not in got[0]["description"]
        assert got[0]["type"] == "self_review"

    def test_different_people_not_reported(self):
        """A5 的事实：执行人王小雨 / 检查人沈雅晨 是两个人 ⇒ 不该报。"""
        steps = [{"step_no": "8",
                  "signatures": [_sig("operator", "王小雨"),
                                 _sig("reviewer", "沈雅晨")]}]
        assert _check_self_review([_page(4, steps)]) == []

    def test_severity_stays_critical(self):
        """聚合不得顺手降级 —— 高置信下两人同名就是 critical 事实。"""
        got = _check_self_review([_page(4, _self_review_steps("high"))])
        assert got[0]["severity"] == "critical"


# ===========================================================================
# A1 / A3 —— 同一天不构成"先后倒置"
# ===========================================================================

def _review(*findings: dict):
    return review_llm_findings(list(findings))


def _f(**kw) -> dict:
    base = {"page": 1, "type": "signature_time_anomaly", "severity": "critical",
            "description": "", "ocr_text": "", "operator": "", "source": "user_rule"}
    base.update(kw)
    return base


class TestReviewCheckDateMustMatch:
    """R111 —— 用户 2026-10-26 **改判**：复核人与检查人的签署日期必须相同。

    ## 这一改判推翻了什么

    R96/CR-28 曾按用户 2026-10-22 的裁定，把「两处签名日期的先后」**一律抑制**，
    理由写的是"同一表单不同栏目本来就不同步填写"，并把 p7（复核 06.04 /
    检查 06.21）判为**正确行为**。R111 撤销该抑制：用户 2026-10-26 亲自纠正 ——
    "复核人和检查人的日期是一致的（一样，早于或者是晚于都需要报异常）"。
    也就是说 10-22 那次把 p7 判反了，被抑制的那条正是真缺陷。

    ## 现在的口径

    ① **复核 ⇄ 检查**：日期不一致（早于/晚于都算）⇒ 必须报，且至少 `warning`
       （后置 `_raise_review_check_date`，因为 LLM 多数只给 `info`）；
    ② 其他角色对（操作人 ⇄ 复核人、复核人 ⇄ 批准人…）**未裁定** ⇒ 恢复原判，
       既不抑制也不抬级 —— 抑制等于替用户下"这不算问题"的结论；
    ③ 「同日却断言先后」仍由 `_check_same_day_contradiction` 收掉（逻辑自证其伪，
       与角色无关），A1/A3 属这一类，**不受**本次改判影响。

    ⚠️ 判据始终是**文案内容**（`is_review_check_date_claim`），不是 `type`：
    实测 28 条 `signature_time_anomaly` 里混着姓名不一致 / 签名缺失 /
    发放日早于起草日 / 年份写错，按类型一刀切会连真缺陷一起吞掉
    ⇒ `TestSignatureOrderDoesNotSwallowRealDefects` 专门钉这些反例。
    """

    #: A1 原文（p1）：两侧日期都是 2025.06.03，文案却断言"早于"，还自带"三者日期相同"
    A1 = ("清场记录中，操作人（陈诗西）与复核人（卫丹丹）签名日期（2025.06.03）"
          "早于QA批准人（赵阿巧）签名日期（2025.06.03），但三者日期相同，"
          "需确认是否为同一天完成。")

    #: A3 原文（p2）：关系词右边没有日期，相同性由"均为"给出
    A3 = ("生产前检查单中，检查人（王小雨）与复核人（蒋俊）签名日期均为2025.06.03，"
          "但复核人签名日期不应早于检查人签名日期，需确认时间顺序。")

    def test_a1_equal_dates_on_both_sides_is_suppressed(self):
        """A1 由 `_check_same_day_contradiction` 收掉（逻辑自证其伪），与角色无关。

        ⚠️ 这条**不受 R111 影响**：它说的不是"复核⇄检查日期不一致"，而是
        "文案自己承认两侧是同一天却断言早于" —— 那个断言本身就不可证伪。
        """
        kept, _down, sup = _review(_f(description=self.A1))
        assert not kept, kept
        assert sup, "必须留下抑制台账"
        assert "不构成" in sup[0]["reason"]

    def test_a3_same_day_stated_in_words_is_suppressed(self):
        kept, _down, sup = _review(_f(description=self.A3))
        assert not kept, kept
        assert sup and "不构成" in sup[0]["reason"]

    def test_same_day_pair_suppressed_even_with_other_dates_present(self):
        """A1 原文里只有**一个**日期值，两条检查会重叠；这里塞一个无关日期打破后者前提。

        剩下那条（`_check_declared_order`）同样判"同一天却断言先后"⇒ 仍抑制。
        两侧是「操作人 ⇄ QA批准人」，与 R111 的复核⇄检查无关。
        """
        desc = ("清场记录中，操作人签名日期（2025.06.03）早于QA批准人签名日期"
                "（2025.06.03），生产日期为 2025.06.01。")
        kept, _down, sup = _review(_f(description=desc, source="llm_cross"))
        assert not kept, kept
        assert sup, "同一天却断言先后，必须抑制"
        assert "同一天" in sup[0]["reason"] or "不构成" in sup[0]["reason"], sup[0]["reason"]

    # ── 以下四条是 R111 的**核心翻转**：从"抑制"改成"必须报" ────────────

    def test_reviewer_before_checker_is_kept(self):
        """p7 实测：复核人 06.04 早于检查人 06.21 —— 用户 2026-10-26 判为**缺陷**。

        ⚠️ 这条在 R96→R111 之间被写成"必须抑制"，是本文件里被改判**两次**的用例。
        现在的期望来自用户原话："复核人和检查人的日期是一致的（一样，早于或者
        是晚于都需要报异常）"。
        """
        desc = "复核人签名日期（2025.06.04）早于检查人签名日期（2025.06.21），存在时间倒置。"
        kept, _down, sup = _review(_f(description=desc))
        assert kept, "复核⇄检查日期不一致必须报出来"
        assert not sup, "不得再抑制"
        # `_f` 默认 severity=critical；R111 只抬不降 ⇒ 断言"至少 warning"。
        assert kept[0]["severity"] in ("warning", "critical")

    def test_p7_verbatim_without_any_date_literal(self):
        """p7 库里那条**一个日期都没有**：锚点只能是签名语，不能是日期字面。

        若判据写成"两侧都要有日期"就会漏掉用户亲口点名的这一条 —— 变异 M7 钉它。
        """
        desc = "复核人签名日期早于检查人签名日期。"
        kept, _down, sup = _review(_f(description=desc, severity="warning"))
        assert kept, "无日期字面也必须认得出这一对"
        assert not sup, sup

    def test_reviewer_after_checker_is_kept(self):
        """p8 实测：检查人 06.03 / 复核人 06.30 —— 方向相反，**同样**是缺陷。

        用户原话是"一样、早于、晚于"三种情况里只有"一样"算正常 ⇒ 晚于也要报。
        """
        desc = "复核人签名日期（2025.06.30）晚于检查人签名日期（2025.06.03），存在时间倒置。"
        kept, _down, sup = _review(_f(description=desc))
        assert kept, "晚于同样必须报"
        assert not sup, sup
        assert kept[0]["severity"] in ("warning", "critical")

    def test_user_rule_source_also_kept(self):
        """`user_rule` 来源同样是 LLM 判定 —— 实测 1619/1620 就是 user_rule critical。"""
        desc = "复核人签名日期（2025.06.04）早于检查人签名日期（2025.06.21），需确认时间顺序。"
        kept, _down, sup = _review(_f(source="user_rule", severity="critical", description=desc))
        assert kept, "来源不是豁免理由"
        assert not sup, sup
        assert kept[0]["severity"] == "critical", "已是 critical，不得被抬级逻辑改动"

    def test_info_level_is_raised_to_warning(self):
        """R111 的后置：LLM 只给 `info` 时也必须抬到 `warning`。

        为什么单独钉：撤销抑制只做了一半 —— 旧实现把这些断言整条抑制，撤销后
        它们按 LLM 自己给的等级出现，实测多数是 `info`。停在 info 会被复核页
        折叠，等于换一种方式继续静默。抬级走 `downgraded` 通道（唯一写路径
        `apply_review` 靠它落 `severity` 与 `guard_reason`），所以断言看的是
        **改判记录**而不是 kept。
        """
        desc = "复核人签名日期（2025.06.04）早于检查人签名日期（2025.06.21）。"
        kept, down, sup = _review(_f(description=desc, severity="info"))
        assert not sup, sup
        assert down, "必须留下改判记录"
        assert down[0]["to_severity"] == "warning"
        assert "2026-10-26" in down[0]["reason"], "理由里要写清是哪次裁定"
        assert kept == [], "同一条不得既留在 kept 又写进 downgraded（会入库两次）"

    def test_rule_source_is_not_re_levelled(self):
        """确定性层自己定的等级，守卫层不替它改判（`source == "rule"` 豁免）。"""
        desc = ("第7页 reviewer 杨月萍 签名时间(2025.06.04) 早于 "
                "operator 韩思萍(2025.06.21),复核/审批顺序异常")
        kept, down, sup = _review(_f(description=desc, source="rule", severity="info"))
        assert kept and not sup, (kept, sup)
        assert kept[0]["severity"] == "info", "规则层自己给 info 就不抬"
        assert not down, down

    def test_time_reversal_still_survives(self):
        """工序时刻的倒序（`time_reversal`）语义不同，**不得**被签名先后规则吞掉。

        ⚠️ R99/CR-6 起，本用例改用**带日期**的形态：原来那条
        「开始 14:12 晚于结束 08:18」（两侧都无日期）现在按**跨零点**处理、
        不再报（p24 原页是 01-22 14:12 → 01-23 08:18，属正常跨日生产）。
        但"跨零点豁免"不是"time_reversal 一律不报" —— 日期在字面上时照报，
        本条钉的就是这一点。
        """
        desc = ("工序3 开始时间 2025-01-23 14:12 晚于结束时间 2025-01-22 08:18，"
                "时间逻辑颠倒。")
        kept, _down, sup = _review(_f(type="time_reversal", description=desc))
        assert kept and not sup, (kept, sup)

    def test_suspicious_date_still_survives(self):
        """日期**字面**异常（2015/2035）语义不同，**不得**被本规则吞掉。"""
        desc = "发现未来日期 '2035.01.25'，超出当前年份合理范围。"
        kept, _down, sup = _review(_f(type="suspicious_date", description=desc))
        assert kept and not sup, (kept, sup)

    def test_non_order_finding_untouched(self):
        desc = "签名姓名不一致（王小雨 vs 沈雅晨），需人工复核。"
        kept, _down, sup = _review(_f(type="signature_mismatch", description=desc))
        assert kept and not sup, (kept, sup)

    def test_cross_year_signature_pair_is_downgraded_not_suppressed(self):
        """p27 实测：「复核者 2025.01.29 早于操作者 2015.01.25」——跨度 10 年。

        R111 撤销抑制后本条**回到 CR-28 之前的口径**：`_check_declared_order`
        发现断言自相矛盾（说"早于"，实际 2025 晚于 2015）⇒ 降级到 `info`，
        但**不抑制** —— 年份误读线索要留着（p27 原页没有 2015，只可能是 OCR
        把 2025 读成 2015）。

        两侧是「复核者 ⇄ 操作者」，**不是**复核⇄检查 ⇒ R111 的抬级不适用。

        ⚠️ 必须显式给 `source="llm_page"`：`_f` 默认的 `user_rule` **整类绕过**
        本模块的 L1–L4（见 `LLM_SOURCES` 的来源闸门），用默认值会让本条
        测不到降级路径 —— 那正是"测试看着绿、其实没走到"的典型。
        """
        desc = "复核者签名时间 2025.01.29 早于操作者签名时间 2015.01.25"
        kept, down, sup = _review(_f(description=desc, severity="warning",
                                     source="llm_page"))
        assert not sup, "R111 后不再抑制"
        assert down, "声明方向与实际日期相反，应降级"
        assert down[0]["to_severity"] == "info"
        assert kept == [], "降级走 downgraded 通道，kept 里不能留副本"


class TestSignatureOrderDoesNotSwallowRealDefects:
    """边界：同样是 `signature_time_anomaly`，但缺陷不是"先后"。

    全部取自 R96 离线回放里被旧版（按类型一刀切）误抑制的真实条目。

    ⚠️ R111 撤销抑制后，这组用例**在实现上**已经自动成立（什么都不抑制）。
    保留它们的价值是**防回归**：哪天有人又把"签名类 finding"按 `type` 一刀切
    收掉，这些真缺陷（姓名不符 / 年份写错 / 发放日早于起草日 / 自我复核）
    会立刻变红。判据必须始终是文案内容。
    """

    def test_document_issue_date_before_drafting_is_kept(self):
        """51 页 p2：记录发放 2020.01.17 早于文件起草/批准 2022.05.07 —— 真矛盾。"""
        desc = ("记录发放人签字日期（2020.01.17）早于文件起草/批准日期"
                "（2022.05.07/2022.05.09）。")
        kept, _down, sup = _review(_f(description=desc))
        assert kept, "发放日早于文件起草日必须留下"
        assert not sup, sup

    def test_signature_after_wash_date_by_ten_years_is_kept(self):
        """51 页 p37：接受人 2025.01.18 / 清洗日 2015.01.18 —— 年份写错，真缺陷。"""
        desc = ("接受人签名日期（2025.01.18）晚于清洗生产日期（2015.01.18）"
                "超过10年，可能为OCR识别错误或实际日期异常。")
        kept, _down, sup = _review(_f(description=desc))
        assert kept, "跨 10 年的年份错必须留下"
        assert not sup, sup

    def test_missing_signature_date_is_kept(self):
        """51 页 p24/p46：说的是"缺失"，不是"先后"。"""
        for desc in ("操作者签名时间缺失，无法与操作时间进行比对。",
                     "所有签名均缺失日期，无法确认签名时间与操作时间的先后关系。"):
            kept, _down, sup = _review(_f(description=desc))
            assert kept, desc
            assert not sup, (desc, sup)

    def test_name_mismatch_typed_as_time_anomaly_is_kept(self):
        """10 页 p3：姓名不一致被标成 `signature_time_anomaly` —— 不是先后问题。"""
        desc = ("同一操作步骤（称量前环境检查与称量后环境检查）中，检查人签名姓名"
                "不一致（王小雨 vs 王涛雨），可能为OCR识别错误或实际签名人不同。")
        kept, _down, sup = _review(_f(description=desc))
        assert kept, "姓名不一致不得因签名类抑制被吞"
        assert not sup, sup

    def test_same_year_document_date_pair_is_kept(self):
        """**同年**的「发放日 vs 起草日」——两侧不是签名语，属文档时序矛盾。

        跨年放行只能挡住 p2 那种 2020/2022；同年变体必须由"两侧都紧贴签名语"
        这条判据挡住。变异 M8 钉它（把判据放宽成"全文出现签名语即可"）。
        """
        desc = "记录发放人签字日期（2025.01.05）早于文件起草日期（2025.01.10）。"
        kept, _down, sup = _review(_f(description=desc))
        assert kept, "发放日早于起草日必须留下"
        assert not sup, sup

    def test_same_person_self_review_is_kept(self):
        """10 页 p4：同一人签名被标成 `signature_time_anomaly` —— 是 self_review。"""
        desc = "步骤8、9、10中，操作人与复核人均为'王小雨'且签名时间相同，存在自我复核风险。"
        kept, _down, sup = _review(_f(description=desc))
        assert kept, "自我复核不得因签名类抑制被吞"
        assert not sup, sup


# ===========================================================================
# A4 —— 同一人名的 OCR 变体不是"不同签名人"
# ===========================================================================

class TestNameVariantIsNotADifferentPerson:
    """A4 —— p3 同一栏两次签名被读成「王小雨」「王涛雨」，只差一个字。"""

    def test_vs_form_is_downgraded(self):
        desc = ("同一操作步骤中，检查人签名姓名不一致（王小雨 vs 王涛雨），"
                "可能为OCR识别错误或实际签名人不同。")
        # 名字变体判定是**描述驱动**的，与 type 无关；这里用 `signature_mismatch`
        # 是因为它才是这类缺陷的规范类型（`signature_time_anomaly` 只是 LLM 常错标）。
        _kept, down, _sup = _review(_f(source="llm_page", type="signature_mismatch",
                                       severity="warning", description=desc))
        assert down, "名字变体应降级"
        assert "仅差一个字" in down[0]["reason"]

    def test_paren_form_is_downgraded(self):
        desc = ("称量前环境检查的检查人（王小雨）与称量后环境检查的检查人（王涛雨）"
                "签名姓名不一致，可能为OCR识别错误。")
        _kept, down, _sup = _review(_f(source="llm_cross", type="signature_mismatch",
                                       severity="warning", description=desc))
        assert down, "括号形态的名字变体也应降级"
        assert "仅差一个字" in down[0]["reason"]

    def test_genuinely_different_names_untouched(self):
        desc = "检查人（王小雨）与复核人（蒋俊）签名姓名不一致。"
        kept, down, _sup = _review(_f(source="llm_page", type="signature_mismatch",
                                      severity="warning", description=desc))
        assert kept and not down, (kept, down)


# ===========================================================================
# A2 / A6 —— 类型必须与内容相符
# ===========================================================================

class TestTypeMatchesContent:
    """A2/A6 —— `param_out_of_spec` 长期被当万能兜底，标题与内容不符。"""

    def test_a2_batch_content_is_retyped(self):
        f = {"type": "param_out_of_spec",
             "description": "第1页批号（50808219）与第2页批号（50008219）不一致，"
                            "可能为跨页批号漂移或OCR识别错误。"}
        canonical, raw = normalize_finding(f)
        assert canonical == "batch_inconsistency"
        assert raw == "param_out_of_spec"       # 原始类型必须留痕
        assert f["type"] == "batch_inconsistency"

    def test_a6_unit_duplication_is_retyped(self):
        f = {"type": "param_out_of_spec",
             "description": "步骤10中，湿度记录为35%RH%RH，单位重复，可能为OCR识别错误。"}
        assert normalize_finding(f)[0] == "completeness"

    def test_real_out_of_spec_keeps_its_type(self):
        """文案确实在断言超差 ⇒ 类型没错，不许被"纠正"。"""
        f = {"type": "param_out_of_spec",
             "description": "工序10 温度实测 35 超出规格范围 (18-26)°C。"}
        assert normalize_finding(f)[0] == "param_out_of_spec"

    def test_non_overbroad_types_are_not_rewritten(self):
        """只纠正过宽类型 —— LLM 明确选的 completeness 不按文案覆盖。"""
        f = {"type": "completeness",
             "description": "第1页批号与第2页批号不一致。"}
        assert normalize_finding(f)[0] == "completeness"

    def test_type_from_description_returns_none_when_unsure(self):
        assert type_from_description("") is None
        assert type_from_description("某处存在异常") is None


# ===========================================================================
# A2 —— 跨页提示的类型枚举
# ===========================================================================

class TestCrossPagePromptTypes:
    """A2 的根因：枚举里没有 `batch_inconsistency`，模型只能就近塞进"参数超标"。"""

    def test_enum_offers_batch_inconsistency(self):
        assert "batch_inconsistency" in _system_prompt(False)

    def test_enum_offers_signature_and_self_review(self):
        p = _system_prompt(False)
        assert "signature_time_anomaly" in p and "self_review" in p

    def test_user_rule_only_when_rules_exist(self):
        """A1/A3/A7 的根因：没有规则时模型仍输出 user_rule。"""
        assert "user_rule" not in _system_prompt(False)
        assert "user_rule" in _system_prompt(True)

    def test_prompt_forbids_reusing_param_out_of_spec(self):
        assert "只用于" in _system_prompt(False)


# ===========================================================================
# A1 / A3 —— 无指称的 user_rule 不再冒充"确定性判据"
# ===========================================================================

class TestUnbackedUserRule:
    """config `user_rules=[]` 时仍产出 7 条 `type=user_rule` 的 critical。"""

    @pytest.fixture
    def llm(self):
        client = MagicMock()
        client.chat_json = AsyncMock(return_value={"findings": []})
        with patch("core.rules.llm_checks.get_llm_client", return_value=client), \
                patch("core.rules.llm_checks._load_review_exemplars",
                      new=AsyncMock(return_value=[])):
            yield client

    @pytest.mark.asyncio
    async def test_unbacked_user_rule_is_downgraded_to_llm_cross(self, llm):
        llm.chat_json.return_value = {"findings": [{
            "page": 2, "type": "user_rule", "severity": "critical",
            "description": "检查人（王小雨）与复核人（蒋俊）签名日期均为2025.06.03，"
                           "但复核人签名日期不应早于检查人签名日期。",
            "ocr_text": "", "operator": "王小雨, 蒋俊", "rule_id": None,
        }]}
        out = await _llm_based_check("摘要", job_id="t", user_rules=[])
        assert len(out) == 1
        assert out[0]["source"] == "llm_cross"      # 不再是"确定性判据"
        assert out[0]["type"] == "signature_time_anomaly"   # 按文案回填
        assert out[0]["rule_id"] is None

    @pytest.mark.asyncio
    async def test_backed_user_rule_keeps_its_source(self, llm):
        rules = [{"id": "R-7", "text": "QA 批准日期不得早于执行日期", "active": True}]
        llm.chat_json.return_value = {"findings": [{
            "page": 1, "type": "user_rule", "severity": "critical",
            "description": "QA 批准日期早于执行日期。",
            "ocr_text": "", "operator": "", "rule_id": "R-7",
        }]}
        out = await _llm_based_check("摘要", job_id="t", user_rules=rules)
        assert out[0]["source"] == "user_rule"
        assert out[0]["rule_id"] == "R-7"

    @pytest.mark.asyncio
    async def test_hallucinated_rule_id_is_downgraded(self, llm):
        rules = [{"id": "R-7", "text": "某规则", "active": True}]
        llm.chat_json.return_value = {"findings": [{
            "page": 1, "type": "user_rule", "severity": "critical",
            "description": "执行人签名缺失。", "ocr_text": "",
            "operator": "", "rule_id": "R-999",
        }]}
        out = await _llm_based_check("摘要", job_id="t", user_rules=rules)
        assert out[0]["source"] == "llm_cross"
        assert out[0]["rule_id"] is None
        assert out[0]["type"] == "completeness"


class TestCrossSourceDedup:
    """A9：同一缺陷在 rule / llm_page / llm_cross / user_rule 各出一份。

    去重的**两侧口径刻意不对称**，这里把不对称本身钉住 —— 否则日后"统一套族"
    会让一条规则发现静默吞掉 LLM 的另一类签名缺陷（**漏报**，比噪声严重）。
    """

    @staticmethod
    def _cov(*pairs):
        return {(p, t) for p, t in pairs}

    def test_llm_cross_is_covered_by_llm_page_of_the_same_family(self):
        """实测 p3：页级标 `signature_time_anomaly`、跨页标 `signature_mismatch`。"""
        assert _overlap_covered_by(
            3, "signature_mismatch", "llm_cross",
            self._cov(), self._cov((3, "signature")),
        ) == "llm_page"

    def test_rule_covers_only_the_exact_type(self):
        """规则层用精确类型：同族但不同缺陷**不得**被吞。"""
        rule = self._cov((3, "signature_time_anomaly"))
        assert _overlap_covered_by(
            3, "signature_time_anomaly", "llm_cross", rule, self._cov(),
        ) == "rule"
        assert _overlap_covered_by(
            3, "signature_mismatch", "llm_cross", rule, self._cov(),
        ) is None, "日期倒置的规则发现不得吞掉「姓名不符」—— 那是另一个缺陷"

    def test_user_rule_source_is_exempt(self):
        rule = self._cov((4, "self_review"))
        page = self._cov((4, "self_review"))
        assert _overlap_covered_by(
            4, "self_review", "user_rule", rule, page,
        ) is None, "用户显式规则与规则层撞 (page,type) 是正常共存"

    def test_llm_fallback_is_deduped_too(self):
        assert _overlap_covered_by(
            8, "completeness", "llm_fallback",
            self._cov((8, "completeness")), self._cov(),
        ) == "rule"

    def test_different_page_or_type_survives(self):
        rule = self._cov((3, "signature_time_anomaly"))
        page = self._cov((3, "signature"))
        assert _overlap_covered_by(
            5, "signature_mismatch", "llm_cross", rule, page,
        ) is None
        assert _overlap_covered_by(
            3, "completeness", "llm_cross", rule, page,
        ) is None


class TestSameDateLiteralDedup:
    """R132 / 0-75：同一页 + 同一日期字面量的日期类主张只留一条。

    实测 p8：同一个 OCR 误读 `2015.6.03` 被 llm_page 报成
    `signature_time_anomaly`、被 llm_cross 又报成 `suspicious_date`。
    """

    @staticmethod
    def _cross(desc, ocr=""):
        return {"page": 8, "description": desc, "ocr_text": ocr}

    def test_same_literal_on_same_page_is_deduped(self):
        from core.pipeline.stage3 import _same_literal_covered_by

        covered = {(8, "2015.6.03"): "llm_page"}
        f = self._cross(
            "复核人签名日期 2015.6.03 早于生产日期 2026.06.30，存在日期异常",
            "复核人/日期：2015.6.03",
        )
        assert _same_literal_covered_by(
            f, "suspicious_date", "llm_cross", covered,
        ) == "llm_page_same_literal"

    def test_different_literal_survives(self):
        from core.pipeline.stage3 import _same_literal_covered_by

        covered = {(8, "2015.6.03"): "llm_page"}
        f = self._cross("复核人签名日期 2016.7.04 早于生产日期")
        assert _same_literal_covered_by(
            f, "signature_time_anomaly", "llm_cross", covered,
        ) is None

    def test_non_date_claim_survives(self):
        """同字面量但类型不属日期主张 ⇒ 不套用（避免吞掉独立缺陷）。"""
        from core.pipeline.stage3 import _same_literal_covered_by

        covered = {(8, "2015.6.03"): "llm_page"}
        f = self._cross("签名栏日期 2015.6.03 旁的无姓名栏位")
        assert _same_literal_covered_by(f, "completeness", "llm_cross", covered) is None

    def test_rule_source_exempt(self):
        """规则层的日期条目不得被 LLM 同字面量条目吞掉（方向反了）。"""
        from core.pipeline.stage3 import _same_literal_covered_by

        covered = {(8, "2015.6.03"): "llm_page"}
        f = self._cross("2015.6.03 日期异常")
        assert _same_literal_covered_by(f, "suspicious_date", "rule", covered) is None

    def test_relation_claims_not_deduped(self):
        """反例（51 页 p2 实测）：共享字面量但断言**关系**的条目不得被吞。

        #5 suspicious_date 与 #7 year_contradiction 的 OCR 文本都含
        2025.01.30，但 #7 讲的是 review 事件年份不一致 —— 是另一件事。
        """
        from core.pipeline.stage3 import _same_literal_covered_by

        covered = {(2, "2025.01.30"): "llm_page"}
        f = self._cross(
            "车间负责人审核年份（2025）与QA产品负责人审核年份（2022）不一致",
            "QA：2022.05.09；车间负责人审核/日期：王2025.01.30",
        )
        assert _same_literal_covered_by(f, "year_contradiction", "llm_cross", covered) is None
        g = self._cross("开始时间晚于结束时间 2025.01.30", "2025.01.30")
        assert _same_literal_covered_by(g, "time_reversal", "llm_cross", covered) is None
