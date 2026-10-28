"""R111 —— 复核人 ⇄ 检查人 的签署日期必须相同（用户 2026-10-26 裁定）。

## 裁定原文

> 一般来说，复核人和检查人的日期是一致的（一样，早于或者是晚于都需要报异常），
> 检查人日期和复核人日期是一致的也就是相同的日期。

## 这条裁定推翻了什么

R96/CR-28 曾按 2026-10-22 的裁定把「两处签名日期的先后」**一律抑制**，并把
p7（复核 2025.06.04 / 检查 2025.06.21）判成**正确行为**。R111 撤销抑制 ——
那次判反了，被抑制的那条正是用户现在点名要报的。

## 三层落点（本文件按层分三组，缺一层就是静默）

| 层 | 落点 | 文件 |
|---|---|---|
| 判定 | `is_review_check_date_claim` 认得出「复核⇄检查」这一对 | `core/finding_quality.py` |
| 确定性 | **R9b** 直接比对两个角色的日期 | `core/rules/rule_time.py` |
| LLM 兜底 | 后置 `_raise_review_check_date` 抬到 `warning` | `core/rules/llm_finding_guard.py` |

⚠️ 三层都要有**反向控制**。判据是「角色归属」，写宽了会把
「操作人 ⇄ 复核人」（跨角色，先后属正常时序）一起报成缺陷 —— 那等于把
CR-28 之前那批误报原样搬回来。
"""
from __future__ import annotations

import pytest

from core.finding_quality import is_review_check_date_claim
from core.rules.llm_finding_guard import review_llm_findings


# ===========================================================================
# 第 1 组：判定层 —— 认得出「复核 ⇄ 检查」这一对
# ===========================================================================

class TestClaimDetector:
    """`is_review_check_date_claim` 的正向与反向控制。

    ⚠️ 这一组**只判角色归属，不判是不是异常**。它回答的是"这句话说的是不是
    复核人和检查人之间的日期比较"，不是"这句话对不对"。
    """

    #: 真实语料（10 页 p7 / p8、51 页若干）里出现过的形态
    POSITIVES = [
        "复核人签名日期（2025.06.04）早于检查人签名日期（2025.06.21），存在时间倒置。",
        "复核人签名日期（2025.06.30）晚于检查人签名日期（2025.06.03）。",
        "检查人签名日期（2025.06.21）晚于复核人签名日期（2025.06.04）。",
        # p7 库里那条**一个日期都没有** —— 锚点只能是签名语
        "复核人签名日期早于检查人签名日期。",
        # 英文 role（结构化数据里 checker / reviewer 是规范键）
        "reviewer signed 2025.06.04 before checker signed 2025.06.21",
        # 复核员 / 检查员 变体
        "复核员签署日期早于检查员签署日期",
    ]

    #: 反向控制 —— 全部必须 False。前三条是**跨角色**（先后属正常时序），
    #: 后面几条是"同一侧出现两个角色"、"说的是事情不是人"、"根本没比日期"。
    NEGATIVES = [
        "操作人签名日期（2025.06.21）晚于复核人签名日期（2025.06.04）。",
        "复核人签名日期（2025.06.21）晚于批准人签名日期（2025.06.04）。",
        "生产日期（2025.06.21）晚于复核人签名日期（2025.06.04）。",
        # 「检查完成日期」说的是**事情**，不是"检查人签署的日期"
        "复核人签名早于检查完成日期。",
        # 对称的陷阱：裸 `复核` 不能把「复核完成日期」当成人
        "复核完成日期早于检查人签名日期。",
        "审核结果记录时间晚于检查人签名日期。",
        # 没有先后关系词
        "复核人与检查人签名姓名不一致。",
        # 角色在**下一个小句**里 —— 关系词右侧必须按标点截断，否则会串味
        "检查人签名日期早于生产日期，复核人签名日期为 2025.06.21。",
        # 角色在**上一个小句**里 —— 左侧同样要截断。不截的话左边会凭空
        # 得到「复核人」，把「生产日期 vs 检查人」误判成这一对。
        "复核人签名齐全，生产日期早于检查人签名日期。",
        # 空串
        "",
    ]

    @pytest.mark.parametrize("text", POSITIVES)
    def test_positive(self, text):
        assert is_review_check_date_claim(text), text

    @pytest.mark.parametrize("text", NEGATIVES)
    def test_negative(self, text):
        assert not is_review_check_date_claim(text), text

    def test_window_is_bounded(self):
        """关系词两侧各只看 24 字 —— 不能因为**全文**出现角色语就命中。

        这条钉的是"把判据放宽成全文搜索"这种退化：一旦放宽，
        任何提到"复核人"的长句里只要出现"早于"就会被抬级。
        """
        text = ("复核人签名齐全，检查人签名齐全。" + "无关内容" * 20
                + "生产日期早于批记录封面日期。")
        assert not is_review_check_date_claim(text), text


# ===========================================================================
# 第 2 组：守卫层 —— LLM 给的 info 必须抬到 warning
# ===========================================================================

def _f(page, ftype, sev, desc, source="llm_page", **kw):
    return {"page": page, "type": ftype, "severity": sev,
            "description": desc, "source": source, **kw}


def _review(one):
    return review_llm_findings([one], structured_by_page={}, raw_by_page={})


class TestRaiseToWarning:
    """撤销抑制只做了一半 —— 不抬级，异常就停在 `info` 被复核页折叠。

    ⚠️ 抬级走 `downgraded` 通道（唯一写路径 `apply_review` 靠它落
    `severity` 与 `guard_reason`），不是就地改 `kept`：两边都放会让同一条
    finding 入库两次。
    """

    DESC = "复核人签名日期（2025.06.04）早于检查人签名日期（2025.06.21）。"

    def test_info_is_raised(self):
        kept, down, sup = _review(_f(7, "signature_time_anomaly", "info", self.DESC))
        assert not sup, sup
        assert kept == [], "抬级后由 downgraded 承载，kept 里不能留副本"
        assert down and down[0]["to_severity"] == "warning"
        assert "2026-10-26" in down[0]["reason"], "理由要写明是哪次裁定"

    def test_warning_is_untouched(self):
        """已经是 warning ⇒ 不产生改判记录（别为了"统一"重复写台账）。"""
        kept, down, sup = _review(_f(7, "signature_time_anomaly", "warning", self.DESC))
        assert not sup, sup
        assert kept and kept[0]["severity"] == "warning"
        assert not down, down

    def test_critical_is_not_downgraded(self):
        """critical 高于目标等级 ⇒ **不得**被"抬级"逻辑拉低。

        抬级的实现是 `rank < rank(warning)`，写反成 `!=` 就会把 critical 降成
        warning —— 这是"看似顺手"的经典错误。
        """
        kept, down, sup = _review(_f(7, "user_rule", "critical", self.DESC,
                                     source="user_rule"))
        assert not sup, sup
        assert kept and kept[0]["severity"] == "critical"
        assert not down, down

    def test_rule_source_is_exempt(self):
        """确定性层自己定的等级，守卫层不替它改判。"""
        kept, down, sup = _review(_f(7, "signature_time_anomaly", "info", self.DESC,
                                     source="rule"))
        assert not sup, sup
        assert kept and kept[0]["severity"] == "info"
        assert not down, down

    @pytest.mark.parametrize("desc", [
        "操作人签名日期（2025.06.21）晚于复核人签名日期（2025.06.04）。",
        "复核人签名日期（2025.06.21）晚于批准人签名日期（2025.06.04）。",
        "生产日期（2025.06.21）晚于复核人签名日期（2025.06.04）。",
    ])
    def test_other_role_pairs_are_not_raised(self, desc):
        """跨角色比较**未裁定** ⇒ 既不抑制也不抬级，原样保留。

        这条是 R111 最容易被写坏的地方：把判据写成"出现复核人 + 出现先后词"
        就会把 CR-28 之前那批误报原样搬回来。
        """
        kept, down, sup = _review(_f(7, "signature_time_anomaly", "info", desc))
        assert not sup, sup
        assert not down, down
        assert kept and kept[0]["severity"] == "info"


# ===========================================================================
# 第 3 组：确定性层 —— R9b 直接比对两个角色（不依赖 LLM）
# ===========================================================================

def _sig(role, name, when, **kw):
    """构造一条签名（形状取自 `rule_time` 实际消费的结构）。

    ⚠️ 字段名是 `sign_time` 而不是 `date` —— 与抽取器的结构化输出保持一致，
    写成 `date` 会让规则静默取不到值、全部跳过（测试假绿）。
    """
    s = {"role": role, "name": name, "sign_time": when}
    s.update(kw)
    return s


def _page(pno, sigs):
    return {"page": pno, "page_no": pno, "page_info": {"production_date": ""},
            "steps": [{"step_no": 1, "signatures": sigs}]}


class TestR9bRule:
    """R9b：`_check_review_check_same_day`。

    为什么需要确定性层：R9a 的 `_ROLE_RANK` 只有 operator/reviewer/qa，
    **「检查人」根本没有 rank** ⇒ 那一对从来没被比过。p7 当年唯一的报警机会
    是 LLM，而 CR-28 又把它抑制了 —— 两层都漏，才有这次用户报的"没提示"。
    """

    def _run(self, pages):
        from core.rules.rule_time import _check_review_check_same_day
        return _check_review_check_same_day(pages)

    def test_different_dates_raise_one_warning(self):
        """p7 原样：检查人 2025.06.21 / 复核人 2025.06.04 ⇒ 1 条 warning。"""
        out = self._run([_page(7, [
            _sig("checker", "韩思萍", "2025.06.21"),
            _sig("reviewer", "杨月萍", "2025.06.04"),
        ])])
        assert len(out) == 1, out
        f = out[0]
        assert f["type"] == "signature_time_anomaly"
        assert f["severity"] == "warning"
        assert "2025.06.04" in f["description"] and "2025.06.21" in f["description"]

    def test_later_reviewer_also_raises(self):
        """方向无关：复核人**晚于**检查人同样是异常（用户："早于或者晚于"）。"""
        out = self._run([_page(8, [
            _sig("checker", "熊乃可", "2025.06.03"),
            _sig("reviewer", "陈一", "2025.06.30"),
        ])])
        assert len(out) == 1, out

    def test_same_day_is_silent(self):
        out = self._run([_page(7, [
            _sig("checker", "韩思萍", "2025.06.21"),
            _sig("reviewer", "杨月萍", "2025.06.21"),
        ])])
        assert out == [], out

    def test_operator_pair_is_silent(self):
        """反向控制：操作人 ⇄ 复核人 不在 R9b 的范围内。"""
        out = self._run([_page(7, [
            _sig("operator", "韩思萍", "2025.06.21"),
            _sig("reviewer", "杨月萍", "2025.06.04"),
        ])])
        assert out == [], out

    def test_unequal_counts_are_skipped(self):
        """1 检查人 + 2 复核人（p8 的形状）⇒ **不比**。

        为什么：多出来的那个复核人可能是另一行的、也可能是同一轮的两个复核
        （车间 + QA）。按"两两都比"会凭空造出"不一致"，而按"取第一个"又是
        任意选。数量不等 ⇒ 无法配对 ⇒ 不判，交给 LLM 层按上下文看。
        这是**有意的保守**，不是漏实现。

        ⚠️ 用例里第一对必须**日期不同**：若首对恰好同一天，去掉数量闸门也
        不会报（zip 只配到第一对）⇒ 用例假绿。变异 N10 钉的就是这一点。
        """
        out = self._run([_page(8, [
            _sig("checker", "熊乃可了", "2025.06.30"),
            _sig("reviewer", "陈一", "2025.06.03"),
            _sig("reviewer", "王五", "2025.06.30"),
        ])])
        assert out == [], out

    def test_blank_date_on_one_side_is_skipped(self):
        """一侧日期空 ⇒ 无可比。缺失由 `completeness` 负责，别在这里重复报。"""
        out = self._run([_page(8, [
            _sig("checker", "张珂", ""),
            _sig("reviewer", "陈一", "2025.06.30"),
        ])])
        assert out == [], out

    def test_time_of_day_is_ignored(self):
        """只比**日期**：同一天 08:00 与 17:30 不算不一致。

        用户说的是"日期一致"，不是"时刻一致"。签名时间带时分的形态在
        结构化数据里很常见（`2025.06.21 08:12`）。
        """
        out = self._run([_page(7, [
            _sig("checker", "韩思萍", "2025.06.21 08:12"),
            _sig("reviewer", "杨月萍", "2025.06.21 17:30"),
        ])])
        assert out == [], out

    def test_interval_signature_uses_start_date(self):
        """签名日期是区间（`2025.06.21-2025.06.22`）⇒ 取**起点**比。

        区间是"这段工作跨了两天"的写法，起点就是签署动作发生的那天。
        """
        out = self._run([_page(7, [
            _sig("checker", "韩思萍", "2025.06.21-2025.06.22"),
            _sig("reviewer", "杨月萍", "2025.06.21"),
        ])])
        assert out == [], out


# ===========================================================================
# 第 4 组：与 LLM 表述去重 —— 同一事实只留信息量大的那条
# ===========================================================================

class TestR9bDedup:
    """R9b 与 LLM 会对**同一事实**各说一遍，这是用户 A9 抱怨的重复噪声。

    实测 10 页 p7 原始输出两条并列：
      · `复核人签名日期早于检查人签名日期。`（llm_page，无姓名无日期）
      · `第7页 工序 检查人韩思萍签署日期（2025.06.21）与复核人杨月萍签署日期
        （2025.06.04）不一致；检查与复核应在同一日期完成`（rule / R9b）

    信息量严格小于规则那条 ⇒ 抑制 LLM 那条。**只在同一页**、**只抑制同一对
    角色**：同页的姓名不符 / 缺日期照常保留（它们不是同一事实）。
    """

    RULE = {
        "page": 7, "type": "signature_time_anomaly", "severity": "warning",
        "rule_id": "R9b", "source": "rule",
        "description": ("第7页 工序 检查人韩思萍签署日期（2025.06.21）与复核人"
                        "杨月萍签署日期（2025.06.04）不一致；检查与复核应在同一日期完成"),
    }
    TERSE = ("复核人签名日期早于检查人签名日期。")

    def _run(self, findings):
        return review_llm_findings(findings, structured_by_page={}, raw_by_page={})

    def test_terse_llm_copy_is_suppressed(self):
        llm = _f(7, "signature_time_anomaly", "warning", self.TERSE)
        kept, _down, sup = self._run([dict(self.RULE), llm])
        assert len(kept) == 1 and kept[0].get("rule_id") == "R9b", kept
        assert len(sup) == 1, sup
        assert sup[0]["evidence"]["covered_by"] == "R9b"

    def test_llm_copy_on_other_page_survives(self):
        """反向控制：别的页没有 R9b ⇒ 不得被这张页的规则结果连带抑制。"""
        llm = _f(8, "signature_time_anomaly", "warning", self.TERSE)
        kept, _down, sup = self._run([dict(self.RULE), llm])
        assert len(kept) == 2, kept
        assert not sup, sup

    def test_other_role_pair_on_same_page_survives(self):
        """反向控制：同页但说的是**另一对角色**（操作人⇄复核人）⇒ 不是同一事实。"""
        llm = _f(7, "signature_time_anomaly", "warning",
                 "操作人签名日期（2025.06.21）晚于复核人签名日期（2025.06.04）。")
        kept, _down, sup = self._run([dict(self.RULE), llm])
        assert len(kept) == 2, kept
        assert not sup, sup

    def test_user_rule_copy_survives(self):
        """反向控制：`user_rule` 是用户自己写的判据，不替它做去重。"""
        llm = _f(7, "user_rule", "warning", self.TERSE, source="user_rule")
        kept, _down, sup = self._run([dict(self.RULE), llm])
        assert len(kept) == 2, kept
        assert not sup, sup
