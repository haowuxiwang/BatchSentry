"""R119 / TODO 0-48（R111 遗留 #146）：签名栏「姓名 + 日期」成对判定。

用户 2026-10-26 第 12 条裁定后半句：表头写"签名+日期"⇒ 两者必须都有。
`core/signature_date_need.py` 只答了**要不要**日期（用于抑制无据断言），
本组用例盯的是**这一格缺了哪一半**。

⚠️ 每条用例的形态都来自真实语料（`page_cache.raw_html` 逐格核对），不是编的：

    p29/p30  「QC 批准/日期：2025.02.21」        —— QC 与栏名之间**有空格**
    p31/p32  「复核人：2025.02.21」              —— 只填日期
    p2       「车间负责人审核/日期：王2025.01.30」—— 姓名只认到姓
    p13      「检验日期：2025.02.10」            —— 日期格，**不是**签名格
    p4       「中控QA签名/日期：赵阿珍」          —— 只填姓名
"""
from __future__ import annotations


from core.rules.rule_doc import _check_completeness
from core.signature_pairing import pairing_findings


def _one(raw: str, page: int = 4) -> dict:
    got = pairing_findings(page, raw)
    assert len(got) == 1, got
    return got[0]


class TestDateOnly:
    def test_inline_slot_with_date_but_no_name(self):
        """p31/p32 真实形态：复核人栏只有日期。

        ⚠️ 严重度是 **info**，不是 warning —— TODO 0-63 已登记「连笔手写姓名
        系统性漏识别、日期正常」，"有日期没姓名"正是那个 OCR 缺口的形态。
        按 warning 报等于把 OCR 缺陷记到记录员头上。
        """
        f = _one("<table><tr><td>复核人：2025.02.21</td></tr></table>", page=31)
        assert f["type"] == "completeness"
        assert f["severity"] == "info"
        assert f["source"] == "rule"
        assert "「复核人」" in f["description"]
        assert "2025.02.21" in f["description"]
        assert "没有签名姓名" in f["description"]
        # 归因必须带 OCR 可能，不能写成"未签"
        assert "OCR" in f["description"]
        assert "0-63" in f["description"]

    def test_qc_prefix_with_space_still_recognised(self):
        """p29/p30：`QC 批准/日期：` 中间有空格，栏名正则跨不过去。"""
        f = _one("<table><tr><td>QC 批准/日期：2025.02.21</td></tr></table>", page=29)
        assert "QC批准/日期" in f["description"]

    def test_qc_prefix_without_space(self):
        f = _one("<table><tr><td>QC批准/日期：2025.02.21</td></tr></table>", page=31)
        assert "QC批准/日期" in f["description"]

    def test_multiple_occurrences_are_aggregated(self):
        raw = "<table><tr><td>复核人：2025.02.21</td></tr><tr><td>复核人：2025.02.22</td></tr></table>"
        f = _one(raw, page=31)
        assert "共 2 处" in f["description"]


class TestNameOnly:
    def test_name_without_date_when_label_demands_both(self):
        """p4 真实形态：中控QA 只签了名、没写日期（用户 2026-10-26 亲自指出的那条）。"""
        f = _one("<table><tr><td>中控QA签名/日期：赵阿珍</td></tr></table>", page=4)
        assert "要求签名与日期" in f["description"]
        assert "赵阿珍" in f["description"]
        assert "没有填写日期" in f["description"]

    def test_severity_split_is_deliberate(self):
        """两半的证据强度不同 ⇒ 严重度必须不同，不能图省事统一。

        日期识别可靠（0-63：日期/印刷体正常）、姓名识别不可靠 ⇒
        「有日期没姓名」是 info，「有姓名没日期」才是 warning。
        """
        date_only = _one("<table><tr><td>复核人：2025.02.21</td></tr></table>")
        name_only = _one("<table><tr><td>中控QA签名/日期：赵阿珍</td></tr></table>")
        assert date_only["severity"] == "info"
        assert name_only["severity"] == "warning"

    def test_name_without_date_is_fine_when_label_has_no_date(self):
        """表头只写「签名」⇒ 不要求日期（用户第 12 条裁定前半句）。"""
        assert pairing_findings(7, "<table><tr><td>操作者：郑旭</td></tr></table>") == []


class TestAbstain:
    def test_both_halves_present(self):
        assert pairing_findings(30, "<table><tr><td>复核人：付付 2025.02.21</td></tr></table>") == []

    def test_single_char_name_counts_as_present(self):
        """p2：姓名只认到「王」。按 2–4 字收会报"没有签名姓名"—— 那是拿 OCR 残缺当缺陷。"""
        assert pairing_findings(2, "<table><tr><td>车间负责人审核/日期：王2025.01.30</td></tr></table>") == []

    def test_plain_date_label_is_not_a_signature_slot(self):
        """p13/p29：「检验日期」「生产日期」是日期格，收进来会一片误报。"""
        assert pairing_findings(13, "<table><tr><td>检验日期：2025.02.10</td></tr></table>") == []
        assert pairing_findings(29, "<table><tr><td>生产日期：2025.01.25</td></tr></table>") == []

    def test_na_value_is_not_a_defect(self):
        assert pairing_findings(9, "<table><tr><td>复核人：N/A</td></tr></table>") == []

    def test_long_value_is_a_paragraph_not_a_cell(self):
        raw = "<table><tr><td>负责人：本工序由当班组长负责复核全部记录并签字确认</td></tr></table>"
        assert pairing_findings(5, raw) == []

    def test_empty_input(self):
        assert pairing_findings(1, "") == []
        assert pairing_findings(1, None) == []


class TestCompletenessWiring:
    """`_check_completeness` 必须在 page 带 `raw_html` 时把判据接进来。"""

    def test_normalize_pages_keeps_raw_html(self):
        """`_normalize_pages` 会重建页面 dict —— 漏带 `raw_html` 就整条判据静默失效。

        这是本仓库踩过的形态（R105：文档写过≠实现做到）。判据层绿、管道层丢字段，
        端到端等于没上线，所以这里必须直接盯归一化那一步。
        """
        from core.rules.base import _normalize_pages

        pages = _normalize_pages([{
            "page": 31,
            "data": {"steps": [], "page_info": {}},
            "raw_html": "<table><tr><td>复核人：2025.02.21</td></tr></table>",
        }])
        assert pages[0]["raw_html"], pages[0]
        got = _check_completeness(pages)
        assert [f for f in got if "只识别到日期" in f["description"]]

    def _page(self, raw):
        page = {"page": 31, "steps": [], "page_info": {}}
        if raw is not None:
            page["raw_html"] = raw
        return page

    def test_picked_up_when_raw_html_present(self):
        got = _check_completeness([self._page("<table><tr><td>复核人：2025.02.21</td></tr></table>")])
        assert [f for f in got if "复核人" in f["description"]]

    def test_skipped_when_raw_html_absent(self):
        """调用方没带原文 ⇒ 整段跳过，既有判据行为不变（不能凭空多报）。"""
        assert _check_completeness([self._page(None)]) == []

    def test_no_duplicate_when_called_twice_on_same_page(self):
        raw = "<table><tr><td>复核人：2025.02.21</td></tr></table>"
        got = _check_completeness([self._page(raw), self._page(raw)])
        pairing = [f for f in got if "只识别到日期" in f["description"]]
        assert len(pairing) == 1, pairing

class TestCrossSourceDedup:
    """本判据与既有 `(page, type)` 跨源去重的**实际**关系（不是推演）。

    写这段是因为我先在 CHANGELOG 里断言"规则与 LLM 会各出一条"，那是个**未经
    核对的推演**。真实机制在 `core/pipeline/stage3._overlap_covered_by`：
    `rule_covered` 用**精确类型**（不是族）⇒ 结论取决于 LLM 把同一格报成什么类型。
    """

    def test_llm_completeness_duplicate_is_already_suppressed(self):
        """LLM 若把同一格也报成 completeness ⇒ 既有去重直接抑制，不会各出一条。"""
        from core.pipeline.stage3 import _overlap_covered_by

        assert _overlap_covered_by(
            4, "completeness", "llm_cross", {(4, "completeness")}, set(),
        ) == "rule"

    def test_llm_signature_time_anomaly_is_not_suppressed(self):
        """报成 signature_time_anomaly ⇒ 精确类型不同 ⇒ **仍会各出一条**。

        这是真实缺口，但**不应**把 completeness 并进 signature 族来堵：
        `_overlap_covered_by` 的注释写明了原因 —— 规则层一旦允许"同族"覆盖，
        一条「签名日期倒置」就会静默吞掉 LLM 独立观察到的「姓名不符」，
        那是两个不同缺陷，属漏报，比噪声严重。
        """
        from core.pipeline.stage3 import _overlap_covered_by

        assert _overlap_covered_by(
            4, "signature_time_anomaly", "llm_cross", {(4, "completeness")}, set(),
        ) is None
