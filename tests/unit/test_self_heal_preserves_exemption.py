# -*- coding: utf-8 -*-
"""F3 护栏：页豁免（`exemption`）必须**留在顶层**，不能被自愈重建吃掉。

背景（2026-10-19 复核 F3，实测）
------------------------------
`core/pipeline/self_heal.py::_self_heal_diag` 是**重建**语义：

    diag = {"self_healed": True, "recovered": ..., "source": prior["source"]}
    diag["prior_diagnostics"] = prior          # 旧的整份塞进子键

于是 `api/review.py::set_page_exemption` 写在**顶层**的 `exemption`
（人工确认的豁免 + 理由 + 时间，GMP 可追溯）在**该页随后走一次自愈**之后
只剩在 `prior_diagnostics.exemption` 里，而**唯一读者**
`api/report.py::_load_exemptions` 读的是**顶层** `diag["exemption"]`：

    if isinstance(diag, dict) and diag.get("exemption"):   # ← 顶层

⇒ 报告里该页豁免**静默消失**（数据还在库里，但读者看不见 ⇒ 等价于丢）。
门禁 2（每页必须 integrity=ok 或带人工豁免）因此可被绕过：豁免过、自愈过 ⇒ 又变成"无豁免"。

为什么不能递归搬运
------------------
有人会想"那就在 prior 里递归找 exemption"。**不行**：`set_page_exemption`
撤销时是 `diag.pop("exemption")`，若历史上某次自愈把豁免嵌进了
`prior_diagnostics`，递归搬运会把**已撤销的豁免**在下次自愈时**复活**。
⇒ 只搬 `prior` 的**顶层**一层。已经丢在更深处的不救（那需要一次性迁移，
且必须先确认没有被撤销过 —— 本护栏不做假设）。
"""
import json

from core.pipeline.self_heal import _self_heal_diag

_EX = {"reason": "原图为手写批注页，已对照纸质批记录人工确认", "created_at": "2026-10-20 09:12:00"}


def _diag(prior, **kw) -> dict:
    raw = _self_heal_diag(prior, **kw)
    assert raw is not None, "有 prior 时不得返回 None（否则整份诊断被清空）"
    out = json.loads(raw)
    assert isinstance(out, dict)
    return out


class TestExemptionSurvivesSelfHeal:
    def test_top_level_exemption_is_preserved(self):
        """**核心判据**：自愈后顶层仍有 exemption，且内容逐字段一致。"""
        out = _diag({"source": "paddle", "exemption": dict(_EX)},
                    recovered=True, content_len=1234)
        assert out.get("exemption") == _EX, (
            "自愈重建诊断 JSON 时丢了顶层 exemption ⇒ "
            "api/report.py::_load_exemptions 读顶层 ⇒ 报告里该页豁免消失")

    def test_exemption_survives_the_probed_branch(self):
        """未恢复（rotation_probed）也必须保住豁免 —— 两条分支共用同一构造函数。"""
        out = _diag({"source": "paddle", "exemption": dict(_EX)},
                    recovered=False, rotation_probed=True)
        assert out.get("exemption") == _EX
        assert out.get("rotation_probed") is True

    def test_exemption_coexists_with_rotation_blocked(self):
        """豁免（人工结论）与 rotation_blocked（上游状况）是两件事，可并存。"""
        out = _diag({"source": "paddle", "exemption": dict(_EX)},
                    recovered=False, rotation_blocked="upstream_congestion")
        assert out.get("exemption") == _EX
        assert out.get("rotation_blocked") == "upstream_congestion"

    def test_prior_evidence_is_still_nested(self):
        """原有证据链不能因为搬运 exemption 而消失。"""
        prior = {"source": "paddle", "exemption": dict(_EX), "rotation_deg": 90}
        out = _diag(prior, recovered=True, content_len=10)
        assert out["prior_diagnostics"] == prior, "prior_diagnostics 必须原样保留"
        assert out["self_healed"] is True
        assert out["source"] == "paddle"

    def test_no_invention_when_prior_has_no_exemption(self):
        """**反向对照**：prior 没有豁免时**不得**凭空造一个。"""
        out = _diag({"source": "paddle"}, recovered=True, content_len=10)
        assert "exemption" not in out

    def test_prior_is_not_mutated(self):
        """纯函数：不得就地改调用方传进来的 dict。"""
        prior = {"source": "paddle", "exemption": dict(_EX)}
        snapshot = json.dumps(prior, sort_keys=True)
        _self_heal_diag(prior, recovered=True, content_len=10)
        assert json.dumps(prior, sort_keys=True) == snapshot, "prior 被就地修改"

    def test_revoked_exemption_is_not_resurrected_from_history(self):
        """**反向对照**：只在 `prior_diagnostics` 深处存在的旧豁免**不得**被搬上来。

        撤销后 `set_page_exemption` 只 pop 顶层；历史深处那份是"撤销前的化石"。
        搬上来 = 已撤销的豁免复活（比丢失更危险）。
        """
        prior = {
            "source": "paddle",
            "self_healed": True,
            "prior_diagnostics": {"source": "paddle", "exemption": dict(_EX)},
        }
        out = _diag(prior, recovered=True, content_len=10)
        assert "exemption" not in out, (
            "把 prior_diagnostics 深处的旧豁免搬上来 ⇒ 已撤销的豁免会复活")
