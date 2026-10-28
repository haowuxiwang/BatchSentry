"""R109 / CR-24：`production_date` 只取**生产**语义。

实测 `0c5cfb00` p1：整页没有「生产日期」栏，只有「执行日期：2022.06.01」
（SOP 执行日期）与「产品批号：1127011N 25010…」。LLM 把执行日期填进了
`production_date` ⇒ 该页日期锚点比真实生产日期早 **2.5 年**，下游所有
"时间先后"判定都建在这个错锚点上。

用户 2026-10-22 已确认：**2022 年生效的空白记录用于 2025 年批次是合规的** ——
所以不能靠"文件过期"解释，只能把**字段**分清楚。

⚠️ 本模块**不修 OCR 读错的年份**：`0c5cfb00` p2/p3 原文写的就是 `2015.01.20`
（批号却是 `1127011N250101` ⇒ 2025）。那是识别错误，由 `suspicious_date`
报"相差 10 年"给复核员；悄悄改成 2025 就是编造。
"""
from __future__ import annotations

import inspect


from core.production_date import apply_to_page, resolve_production_date
from core.pipeline import stage2

#: 真实 p1 形态：只有执行日期，没有生产日期栏。
REAL_P1 = (
    "<table><tr><td>产品负责人审核： 孙俊燕 2022.05.09</td>"
    "<td>QA 经理审核： 闫鑫 2022.05.12</td></tr>"
    "<tr><td>执行日期：2022.06.01</td><td>产品批号：1127011N 250101</td></tr></table>"
)

#: 真实 p2 形态：开始/结束生产日期写在同一行。
REAL_P2 = (
    "<table><tr><td>开始生产日期：2015.01.20 理论产量：1300~3200</td></tr>"
    "<tr><td>结束生产日期：2015.01.25 实际产量：1550.32</td></tr></table>"
)

#: 真实 p3 形态：标签与日期被单元格分开。
REAL_P3 = "<table><tr><td>开始生产日期</td><td>2015.01.20</td></tr></table>"


class TestFieldDisambiguation:
    def test_execution_date_is_not_production_date(self):
        """真实 p1：只有「执行日期」⇒ 清空，不猜。"""
        assert resolve_production_date(REAL_P1, "2022.06.01") == ("", "cleared_nonproduction")

    def test_start_production_date_wins(self):
        assert resolve_production_date(REAL_P2, "2015.01.20") == ("2015.01.20", "literal")

    def test_label_in_separate_cell(self):
        """`开始生产日期</td><td>2015.01.20` —— 标签与日期隔着标签。"""
        assert resolve_production_date(REAL_P3, "") == ("2015.01.20", "literal")

    def test_same_day_different_format_keeps_original(self):
        """同一天不同写法 ⇒ **不动格式**（否则 80 页的日期写法全被改一遍）。"""
        html = "<table><tr><td>生产日期：2025年01月20日</td></tr></table>"
        assert resolve_production_date(html, "2025-01-20") == ("2025-01-20", "literal")

    def test_different_day_adopts_the_literal(self):
        """真不一致时以原文为准（模型读错了）。"""
        html = "<table><tr><td>生产日期：2025.01.27</td></tr></table>"
        assert resolve_production_date(html, "2025-01-20") == ("2025.01.27", "literal")

    def test_empty_value_is_filled_from_literal(self):
        """当前为空而原文有字面 ⇒ 填上（此前空锚点会让时序检查静默失效，CR-5）。"""
        html = "<table><tr><td>生产日期：2025.01.25</td></tr></table>"
        assert resolve_production_date(html, "") == ("2025.01.25", "literal")


class TestAbstain:
    def test_no_label_and_no_current(self):
        assert resolve_production_date("<table><tr><td>参数</td></tr></table>", "") == ("", "absent")

    def test_no_label_keeps_model_value(self):
        """原文没有日期标签，当前值也不在非生产标签旁 ⇒ 保留（弃权）。"""
        assert resolve_production_date("<table><tr><td>参数 25.5</td></tr></table>", "2025-01-20") == (
            "2025-01-20", "model"
        )

    def test_empty_html_keeps_value(self):
        assert resolve_production_date("", "2025-01-20") == ("2025-01-20", "model")
        assert resolve_production_date(None, "") == ("", "absent")

    def test_none_current(self):
        """模型本来就没填 ⇒ `absent`（没有值可清，不该记成"清空了一条错值"）。"""
        assert resolve_production_date(REAL_P1, None) == ("", "absent")

    def test_ocr_year_error_is_not_silently_fixed(self):
        """原文就是 2015（批号却是 250101）⇒ 照原文，**不改**成年份 2025。"""
        assert resolve_production_date(REAL_P2, "2015.01.20")[0] == "2015.01.20"


class TestApplyToPage:
    def test_writes_value_and_source(self):
        page = {"page_info": {"production_date": "2022.06.01"}}
        assert apply_to_page(page, REAL_P1) == "cleared_nonproduction"
        assert page["page_info"]["production_date"] == ""
        assert page["page_info"]["production_date_source"] == "cleared_nonproduction"

    def test_missing_page_info_is_safe(self):
        assert apply_to_page({}, REAL_P1) == "absent"
        assert apply_to_page(None, REAL_P1) == "absent"
        assert apply_to_page({"page_info": None}, REAL_P1) == "absent"

    def test_does_not_mutate_other_fields(self):
        page = {"page_info": {"title": "批记录", "batch_no": "1127011N250101"}}
        apply_to_page(page, REAL_P2)
        assert page["page_info"]["title"] == "批记录"
        assert page["page_info"]["batch_no"] == "1127011N250101"


class TestStage2Wiring:
    def test_stage2_applies_it_before_persisting(self):
        """判据写好了但没接进 Stage 2 ⇒ 等于没修（规则层读的是落库后的值）。"""
        src = inspect.getsource(stage2)
        assert "apply_production_date(structured, raw_html)" in src
        # 必须在 payload 序列化之前：之后改就等于没改
        assert src.index("apply_production_date(structured, raw_html)") < src.index(
            "payload = json.dumps(structured"
        )
