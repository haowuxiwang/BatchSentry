"""R102 · CR-29：`signature_time_anomaly` 是垃圾桶类型，按**文案**重分类。

## 为什么不能按类型处理

实测库里 14 条 `signature_time_anomaly`（10 页 4 + 51 页 10；文档记的 28 条是
CR-28 落地前的口径）里，只有 2 条真是"两处签名日期的先后"，其余混着：

| 实际缺陷 | 应走 |
|---|---|
| 姓名不一致（王小雨 vs 王涛雨） | `signature_mismatch` |
| 操作人与复核人同一人、时间相同 | `self_review` |
| 签名日期缺失 / 全部无日期 | `completeness` |
| 未来日期 / 相差 10 年（年份误读） | `suspicious_date` |
| 记录发放日早于文件起草/批准日 | `doc_version` |

按类型一刀切抑制会**静默漏识别**（§3.1d 的结论）。所以入库前先按文案送回各自的
类型，剩下的才是 CR-28 该判的"先后"。

## 钉住的契约

1. **先后断言优先**：文案本身是"两处签名日期的先后"时，**不改类型**。
   反例（p18）「签名日期（2025.04.21）晚于复核者签名日期（**无日期**）」——
   句子里顺带出现"无日期"，若先匹配"缺失/无日期"就会被改成 `completeness`，
   而 `completeness` 不在 CR-28 的白名单里 ⇒ **把本该抑制的条目变成新的可见噪声**。
2. **只动垃圾桶类型**：`param_out_of_spec` 文案里出现"缺失"不得被改成 `completeness`
   （那是 R95 过宽类型纠正的地盘，两套规则各管一段）。
3. **单一真值**：判据只有 `core.finding_quality.is_signature_order_claim` 一份，
   `llm_finding_guard` 里的 `_is_signature_order_claim` 必须是**同一个对象**
   （结构断言，防止有人再复制一份导致两处漂移）。
"""
from __future__ import annotations

import pytest

from core.finding_quality import (
    is_signature_order_claim,
    normalize_finding,
)


def _f(ftype: str, desc: str) -> dict:
    return {"type": ftype, "description": desc, "severity": "warning", "page": 1}


def _reclass(ftype: str, desc: str) -> tuple[str, str | None]:
    f = _f(ftype, desc)
    return normalize_finding(f)


class TestReclassifyByDescription:
    """§3.1d 表逐行对照 —— 每条都是库里真实文案。"""

    def test_name_mismatch(self):
        canonical, raw = _reclass(
            "signature_time_anomaly",
            "同一操作步骤（称量前环境检查与称量后环境检查）中，检查人签名姓名不一致"
            "（王小雨 vs 王涛雨），可能为OCR识别错误或实际签名人不同，需人工复核。",
        )
        assert canonical == "signature_mismatch"
        assert raw == "signature_time_anomaly"

    def test_self_review(self):
        canonical, raw = _reclass(
            "signature_time_anomaly",
            "步骤8、9、10中，操作人与复核人均为'王小雨'且签名时间相同，存在自我复核风险。",
        )
        assert canonical == "self_review"
        assert raw == "signature_time_anomaly"

    def test_missing_date(self):
        canonical, _ = _reclass(
            "signature_time_anomaly",
            "操作者签名时间缺失，无法与操作时间进行比对。",
        )
        assert canonical == "completeness"

    def test_all_signatures_missing_date(self):
        canonical, _ = _reclass(
            "signature_time_anomaly",
            "所有签名均缺失日期，无法确认签名时间与操作时间的先后关系。",
        )
        assert canonical == "completeness"

    def test_future_date(self):
        canonical, _ = _reclass(
            "signature_time_anomaly",
            "记录发放人签字日期（2025.01.1）晚于执行日期（2022.06.01），"
            "且为未来日期（相对于当前2026年）。",
        )
        assert canonical == "suspicious_date"

    def test_ten_year_gap(self):
        canonical, _ = _reclass(
            "signature_time_anomaly",
            "接受人签名日期（2025.01.18）晚于清洗生产日期（2015.01.18）超过10年，"
            "可能为OCR识别错误或实际日期异常。",
        )
        assert canonical == "suspicious_date"

    def test_decade_difference(self):
        canonical, _ = _reclass(
            "signature_time_anomaly",
            "指令发出日期（2015.01.20）早于接收日期（2025.01.20），且相差10年，"
            "可能为OCR识别错误或实际记录异常。",
        )
        assert canonical == "suspicious_date"

    def test_record_before_sop(self):
        canonical, _ = _reclass(
            "signature_time_anomaly",
            "记录发放人签字日期（2020.01.17）早于文件起草/批准日期（2022.05.07/2022.05.09）。",
        )
        assert canonical == "doc_version"


class TestReverseControls:
    """**不许**被改走的几类 —— 每一条都对应一次真实误伤风险。"""

    def test_pure_signature_order_stays(self):
        canonical, raw = _reclass(
            "signature_time_anomaly", "复核人签名日期早于检查人签名日期。"
        )
        assert canonical == "signature_time_anomaly"
        assert raw is None

    def test_order_claim_wins_over_missing_word(self):
        """p18：句子里有"无日期"，但主体是先后 ⇒ 必须留在原类型（交给 CR-28）。"""
        canonical, _ = _reclass(
            "signature_time_anomaly",
            "步骤4中，操作者王志清的签名日期（2025.04.21）晚于复核者吴伟胜的签名日期"
            "（无日期），且存在签名'A 转清'的日期（2025.01.21）早于操作者签名日期。",
        )
        assert canonical == "signature_time_anomaly"

    def test_order_claim_predicate_is_the_guard(self):
        """顺序守卫用的就是 CR-28 那一份判据，不是另写的近似版。"""
        assert is_signature_order_claim(
            "步骤4中，操作者王志清的签名日期（2025.04.21）晚于复核者吴伟胜的签名日期（无日期）"
        )

    def test_other_types_untouched(self):
        """非垃圾桶类型不动：过宽类型的纠正归 R95 管，两套规则不许互相越界。"""
        canonical, raw = _reclass(
            "param_out_of_spec", "温度 45.6 超出规格，且签名缺失"
        )
        assert canonical == "param_out_of_spec"
        assert raw is None

    def test_unknown_trash_text_keeps_type(self):
        """匹配不上任何具体缺陷 ⇒ 留在原类型（不猜）。"""
        canonical, _ = _reclass(
            "signature_time_anomaly", "签名相关情况需要人工进一步核实。"
        )
        assert canonical == "signature_time_anomaly"

    def test_empty_description_is_noop(self):
        canonical, _ = _reclass("signature_time_anomaly", "   ")
        assert canonical == "signature_time_anomaly"


class TestSingleSourceOfTruth:
    def test_guard_alias_is_the_same_object(self):
        """结构断言：`llm_finding_guard._is_signature_order_claim` 必须是同一个对象。

        防的是"实现上移了、但某处又复制一份"——两份判据必然漂移（PITFALLS §二十六）。
        """
        from core.rules import llm_finding_guard as guard

        assert guard._is_signature_order_claim is is_signature_order_claim

    def test_no_local_regex_copy_left_in_guard(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        src = (root / "core" / "rules" / "llm_finding_guard.py").read_text(encoding="utf-8")
        assert "_SIGNATURE_WORD_RE = re.compile" not in src
        assert "def _is_signature_order_claim" not in src


@pytest.mark.parametrize("ftype", ["signature_time_anomaly"])
def test_raw_type_records_original(ftype):
    """改写必须留痕：`raw_type` 记原类型，供审计追溯。"""
    f = _f(ftype, "操作者签名时间缺失，无法与操作时间进行比对。")
    canonical, raw = normalize_finding(f)
    assert (canonical, raw) == ("completeness", ftype)
    assert f["type"] == "completeness"
