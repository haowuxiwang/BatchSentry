"""R96 / CR-2 —— 签名角色词表必须覆盖抽取器实际会产出的 role。

## 为什么需要这组用例

`core/rules/rule_doc.py` 的 `_has_signed()` 用**三个元组**判断"这一页到底签没签"。
旧词表只认 5 个英文名（operator / reviewer / workshop_reviewer / qa / qa_reviewer），
而两份真实批记录（`试用批记录.pdf` 10 页 + `丝裂霉素提取批记录.pdf` 51 页）的
`page_cache.structured_json` 里实际出现了 **22 种** role —— 于是：

- `qa_approver`（p1 赵阿巧、p6 孙文莉）⇒ 报「未识别到 QA 签名」，可原页明明签了
- `review`（19 次）/ `checker`（6 次）/ `inspector` / `acceptor` ⇒ 报「未识别到复核人签名」

**这是"签了也判没签"**，属最严重的一类误报：复核员会去改一份本来就合规的记录。

## 这组用例**不是**什么

不是精确率证明。role 分布是统计出来的（见 `docs/CROSS_REVIEW_2026-10-22.md` §2.5 D1），
**没有标注集**，因此这里只钉"词表覆盖 + 归一族不重叠"这一条结构性不变式。
"""
from __future__ import annotations

import pytest

from core.rules.rule_doc import (
    _OPERATOR_ROLES_DOC,
    _QA_ROLES_DOC,
    _REVIEWER_ROLES_DOC,
    _check_completeness,
)

#: 两份真实记录里出现过的**全部** role 及其出现次数（`page_cache` 统计）。
#: 期望值 = 该 role 是否计入对应签名槽；`None` 表示**故意不收录**（见 CR-3）。
OBSERVED_ROLES: dict[str, str | None] = {
    # ── 操作族 ──
    "operator": "operator",
    "操作人": "operator",
    "操作员": "operator",
    "执行人": "operator",
    # ── 复核族 ──
    "reviewer": "reviewer",
    "review": "reviewer",
    "checker": "reviewer",
    "inspector": "reviewer",
    "acceptor": "reviewer",
    "workshop_reviewer": "reviewer",
    "复核人": "reviewer",
    "复核员": "reviewer",
    "检查人": "reviewer",
    # ── QA / 审批族 ──
    "qa": "qa",
    "qa_reviewer": "qa",
    "qa_approver": "qa",
    "qc_approval": "qa",
    "qc_approver": "qa",
    "approval": "qa",
    "质量保证": "qa",
    "qa审核": "qa",
    "批准人": "qa",
    "放行人": "qa",
    # ── 故意不收录（CR-3 以"模板无该栏"单独处理）──
    "负责人": None,
    "supervisor": None,
    "主管": None,
    # ── 与三个签名槽无关的抽取器标签 ──
    "draft": None,
    "issuer": None,
    "tester": None,
    "sampler": None,
    "receiver": None,
    "transferor": None,
    "other": None,
}

_FAMILIES = {
    "operator": _OPERATOR_ROLES_DOC,
    "reviewer": _REVIEWER_ROLES_DOC,
    "qa": _QA_ROLES_DOC,
}


def _steps(*sigs: dict) -> list[dict]:
    return [{
        "step_no": "1", "operation": "称量",
        "start_time": "10:00", "end_time": "11:00",
        "signatures": list(sigs),
    }]


def _sig(role: str, name: str) -> dict:
    return {"role": role, "name": name, "sign_time": "2025.06.03", "confidence": "low"}


def _descs(steps: list[dict]) -> list[str]:
    return [f["description"] for f in _check_completeness([{"page": 1, "steps": steps}])]


class TestRoleVocabularyCoversObservedRoles:
    """词表必须覆盖抽取器实际会产出的 role。"""

    @pytest.mark.parametrize("role,family", sorted(OBSERVED_ROLES.items()))
    def test_role_maps_to_expected_family(self, role: str, family: str | None):
        hits = [name for name, doc in _FAMILIES.items() if role in doc]
        if family is None:
            assert hits == [], f"{role!r} 应**不**计入任何签名槽，实际计入 {hits}"
        else:
            assert hits == [family], f"{role!r} 应只计入 {family}，实际 {hits}"

    def test_families_are_pairwise_disjoint(self):
        """一个 role 只能属于一个签名槽。

        若同一 role 同时算 operator 和 reviewer，一次签名就能把两个槽都"填满"，
        等于用一个签名掩盖另一个槽的真实缺失 —— 这类静默漏报最难发现。
        """
        for a, da in _FAMILIES.items():
            for b, db in _FAMILIES.items():
                if a >= b:
                    continue
                overlap = set(da) & set(db)
                assert not overlap, f"{a} 与 {b} 重叠：{sorted(overlap)}"


class TestSignedButReportedMissing:
    """真实记录里确认过的误报，逐条钉住。"""

    def test_qa_approver_counts_as_qa_signature(self):
        """p1 实测：`{'role':'qa_approver','name':'赵阿巧'}` ⇒ 旧词表报「缺少 QA 签名」。"""
        descs = _descs(_steps(
            _sig("operator", "王小雨"), _sig("reviewer", "卫丹丹"),
            _sig("qa_approver", "赵阿巧"),
        ))
        assert not any("QA 签名" in d for d in descs), descs

    def test_qc_approval_counts_as_qa_signature(self):
        descs = _descs(_steps(
            _sig("operator", "丁伟力"), _sig("reviewer", "吴伟胜"),
            _sig("qc_approval", "周丹萍"),
        ))
        assert not any("QA 签名" in d for d in descs), descs

    def test_review_role_counts_as_reviewer(self):
        """`review` 在 51 页里出现 19 次（17 次带姓名）。"""
        descs = _descs(_steps(
            _sig("operator", "丁伟力"), _sig("review", "吴伟胜"),
            _sig("qa_approver", "孙文莉"),
        ))
        assert not any("复核人签名" in d for d in descs), descs

    def test_checker_role_counts_as_reviewer(self):
        """`checker` 在 10 页 p8 出现 2 次。"""
        descs = _descs(_steps(
            _sig("operator", "赵阿巧"), _sig("checker", "熊乃可了"),
            _sig("qa_approver", "孙文莉"),
        ))
        assert not any("复核人签名" in d for d in descs), descs


class TestDeliberateExclusions:
    """`负责人` 等**故意**不算 QA —— 混进来会把"模板无 QA 栏"整条吞掉。"""

    def test_leader_signature_does_not_count_as_qa(self):
        descs = _descs(_steps(
            _sig("operator", "丁伟力"), _sig("reviewer", "吴伟胜"),
            _sig("负责人", "刘××"),
        ))
        assert any("QA 签名" in d for d in descs), descs


class TestEmptyNameStillReported:
    """回归 A8：词表扩展**不得**把"有行无值"当成已签。"""

    def test_empty_qa_name_still_reports_missing(self):
        descs = _descs(_steps(
            _sig("operator", "丁伟力"), _sig("reviewer", "吴伟胜"),
            _sig("qa_approver", ""),
        ))
        assert any("QA 签名" in d for d in descs), descs

    def test_empty_review_name_still_reports_missing(self):
        descs = _descs(_steps(
            _sig("operator", "丁伟力"), _sig("review", ""),
            _sig("qa_approver", "孙文莉"),
        ))
        assert any("复核人签名" in d for d in descs), descs
