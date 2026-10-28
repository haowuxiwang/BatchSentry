"""R101 · CR-10 / CR-11：文档级事实收敛为一条陈述。

## 这两条治的是什么

不是"噪声多"，而是**同一件文档级事实被按页复述**，且复述时把**系统时钟**
写进了结论：

- CR-10（10 页语料）：p5、p10 各一条「批生产记录缺少生产日期信息。」——
  缺的是**页眉字段本身**（模板级），不是"某几页漏填"。
- CR-11（51 页语料 12 条）：`suspicious_date` 全部以「距今/当前年份」为基准
  ——「生产日期（2015.01.20）距今（2026-09-24）已超过10年」。2026-09-24 是
  **跑批那天的系统时间**，不是记录里的任何事实；同一份文档里 2015 是 2025 的
  OCR 误读，该说的是"年份读数不可信"。

## 钉住的契约（改实现前先读）

1. `year_baseline_today` 的**门槛是 1**：哪怕只有一条也必须重写，因为它含
   系统时钟；`missing_production_date` 的门槛是 2（单条已是文档级口吻）。
2. 摘要文案**不得**出现系统时钟年份（CR-11 明写"禁止把今天写进结论"）。
   本文件用 `current_year=` 显式注入，测试不依赖跑批日期。
3. 聚合后的条目带 `aggregated=True`，**不得**被二次聚合（否则年份清单会被洗掉）。
"""
from __future__ import annotations

from pathlib import Path

from core.finding_noise import (
    document_fact_kind,
    reduce_document_level_facts,
)

CUR = 2026  # 测试里"跑批那天的年份"，显式传入，不读系统时钟
_ROOT = Path(__file__).resolve().parents[2]


def _f(page: int, ftype: str, desc: str, severity: str = "info") -> dict:
    return {"page": page, "type": ftype, "severity": severity,
            "description": desc, "source": "llm_page"}


class TestDocumentFactKind:
    def test_missing_production_date_short_form(self):
        assert document_fact_kind(
            _f(5, "completeness", "批生产记录缺少生产日期信息。")
        ) == "missing_production_date"

    def test_missing_production_date_mixed_page_info(self):
        """51 页 p10 的真实写法：和版本号/批号写在一句里。"""
        assert document_fact_kind(
            _f(10, "completeness", "页面信息缺失：版本号、批号、生产日期。")
        ) == "missing_production_date"

    def test_qa_missing_is_not_a_document_fact(self):
        assert document_fact_kind(
            _f(1, "completeness", "第1页 工序 称量岗位清场 缺少 QA 签名")
        ) is None

    def test_today_baseline_by_distance(self):
        assert document_fact_kind(_f(
            3, "suspicious_date",
            "生产日期（2015.01.20）距今（2026-09-24）已超过10年，可能存在记录归档或版本管理问题。",
        )) == "year_baseline_today"

    def test_today_baseline_by_current_year(self):
        assert document_fact_kind(_f(
            19, "suspicious_date",
            "记录日期2015.01.23早于当前年份(2026)超过10年，需确认是否为历史批记录或日期填写错误。",
        )) == "year_baseline_today"

    def test_same_page_year_misread_is_not_today_baseline(self):
        """CR-33 明写这类**仍要报**（同页同年却读成 2015），不得被 CR-11 吞掉。"""
        assert document_fact_kind(_f(
            23, "suspicious_date",
            "取样时间 '2025年01月201109时05分' 格式异常，疑似OCR将'20日'误识别为'2011'。",
        )) is None

    def test_already_aggregated_is_skipped(self):
        f = _f(5, "completeness", "全份记录的记录页页眉无生产日期字段")
        f["aggregated"] = True
        assert document_fact_kind(f) is None


class TestReduceDocumentLevelFacts:
    def test_two_missing_date_become_one(self):
        """CR-10 验收：10 页语料 2 条 → 1 条。"""
        items = [
            _f(5, "completeness", "批生产记录缺少生产日期信息。", "warning"),
            _f(10, "completeness", "批生产记录缺少生产日期信息。", "warning"),
        ]
        kept, rep = reduce_document_level_facts(items, current_year=CUR)
        assert len(kept) == 1
        assert kept[0]["page_list"] == [5, 10]
        assert kept[0]["aggregated"] is True
        assert "页眉" in kept[0]["description"]
        assert "生产日期字段" in kept[0]["description"]
        assert rep["aggregated"]["missing_production_date"]["count"] == 2

    def test_single_missing_date_is_left_alone(self):
        items = [_f(5, "completeness", "批生产记录缺少生产日期信息。", "warning")]
        kept, rep = reduce_document_level_facts(items, current_year=CUR)
        assert len(kept) == 1
        assert kept[0] is items[0]
        assert rep["aggregated_total"] == 0

    def test_twelve_today_based_years_become_one(self):
        """CR-11 验收：51 页语料 12 条 → 1 条，且文案不含系统时钟年份。"""
        items = [
            _f(p, "suspicious_date",
               f"记录日期2015.01.{p:02d}早于当前年份(2026)超过10年，需确认。")
            for p in range(2, 14)
        ]
        kept, rep = reduce_document_level_facts(items, current_year=CUR)
        assert len(kept) == 1
        desc = kept[0]["description"]
        assert "2026" not in desc, "CR-11：结论里不得出现系统时钟年份"
        assert "2015" in desc
        assert kept[0]["aggregated"] is True
        assert rep["aggregated"]["year_baseline_today"]["count"] == 12

    def test_single_today_based_year_is_still_rewritten(self):
        """门槛为 1 的理由：即使只有一条，它含系统时钟，必须重写。"""
        items = [_f(3, "suspicious_date",
                    "生产日期（2015.01.20）距今（2026-09-24）已超过10年。")]
        kept, rep = reduce_document_level_facts(items, current_year=CUR)
        assert len(kept) == 1
        assert kept[0] is not items[0]
        assert "2026" not in kept[0]["description"]
        assert rep["aggregated_total"] == 0  # 条数没减，只是被重写

    def test_current_year_is_injectable(self):
        """年份集合里只剔除"注入的当前年"，不硬编码 2026。"""
        items = [_f(3, "suspicious_date", "记录日期2019.05.01距今（2031-01-01）已超过10年。")]
        kept, _ = reduce_document_level_facts(items, current_year=2031)
        assert "2031" not in kept[0]["description"]
        assert "2019" in kept[0]["description"]

    def test_other_findings_untouched(self):
        items = [
            _f(1, "param_out_of_spec", "参数 温度=45.6 不在 0~5 内", "warning"),
            _f(2, "signature_mismatch", "姓名不一致", "critical"),
        ]
        kept, rep = reduce_document_level_facts(items, current_year=CUR)
        assert kept == items
        assert rep["aggregated_total"] == 0

    def test_not_double_aggregated(self):
        """聚合摘要不得被第二次聚合（否则年份清单被洗掉）。"""
        items = [
            _f(2, "suspicious_date", "记录日期2015.01.02早于当前年份(2026)超过10年。"),
            _f(3, "suspicious_date", "记录日期2015.01.03早于当前年份(2026)超过10年。"),
        ]
        once, _ = reduce_document_level_facts(items, current_year=CUR)
        twice, rep2 = reduce_document_level_facts(once, current_year=CUR)
        assert twice == once
        assert rep2["aggregated_total"] == 0


class TestStage3Wiring:
    def test_stage3_calls_it_before_completeness_noise(self):
        """接线契约：必须先于 completeness 降噪（否则被通用桶吞掉）。"""
        src = (_ROOT / "core" / "pipeline" / "stage3.py").read_text(encoding="utf-8")
        assert "reduce_document_level_facts(findings)" in src
        i_fact = src.index("reduce_document_level_facts(findings)")
        i_noise = src.index("reduce_completeness_noise(\n        findings,")
        assert i_fact < i_noise, "文档级事实必须先于 completeness 降噪"
