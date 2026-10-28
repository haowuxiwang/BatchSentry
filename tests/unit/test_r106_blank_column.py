"""R106 / P1-3：整栏全空 = 抽取不可判，不是"记录缺失"。

事故（实测 `e50e9a7e` p3，三个 job 同型）：「关键设备清单」的**确认**栏，
`raw_html` 里每一行都是空 `<td></td>`（13 个），LLM 据此发了
「关键设备清单中所有设备的'确认'栏均为空白，缺少确认签名或标记」。
用户看原页：**那一列是打了钩的** —— 是 OCR 没取到手写勾选。

一整列所有行都空是**抽取失败**的形态（人手填一列极少一行都不填），但纯文本
分不出"没填"与"没识别" ⇒ 唯一诚实的结论是**不可判**。与 CR-4 同一条原则。
"""
from __future__ import annotations

from core.blank_column import (
    blank_columns,
    claimed_blank_column,
    retype_blank_column_claims,
)
from core.rules.llm_finding_guard import apply_review

#: 实测 p3 的原文（截取「关键设备清单」那张表，保留 rowspan 与 colspan 形态）。
REAL_P3 = (
    '<table><tr><td colspan="2"> 1m^3 </td></tr>'
    '<tr><td colspan="5">丝裂霉素提取关键设备清单</td></tr>'
    '<tr><td>设备名称</td><td>设备位号</td><td>设备编号</td><td>楼号</td><td>确认</td></tr>'
    '<tr><td>预处理罐</td><td>R2101</td><td>110301-103T-0017</td><td>Y33</td><td></td></tr>'
    '<tr><td>陶瓷膜机组</td><td>S2101</td><td>091004-103T-0005</td><td>Y33</td><td></td></tr>'
    '<tr><td rowspan="2">滤液储罐</td><td>V2102a</td><td>110302-103T-0065</td><td>Y33</td><td></td></tr>'
    '<tr><td>V2102b</td><td>110302-103T-0066</td><td>Y33</td><td></td></tr>'
    '<tr><td rowspan="4">交换机</td><td>T2101a</td><td>110106-103T-0011</td><td>Y33</td><td></td></tr>'
    '<tr><td>T2101b</td><td>110106-103T-0012</td><td>Y33</td><td></td></tr>'
    '<tr><td>T2101c</td><td>110106-103T-0013</td><td>Y33</td><td></td></tr>'
    '<tr><td>T2101d</td><td>110106-103T-0014</td><td>Y33</td><td></td></tr>'
    '<tr><td>SP-1 洗脱液罐</td><td>V2109</td><td>110302-103T-0138</td><td>Y33</td><td></td></tr>'
    '</table>'
)

REAL_CLAIM = "关键设备清单中所有设备的'确认'栏均为空白，缺少确认签名或标记。"


def _finding(desc: str, page: int = 3, ftype: str = "completeness") -> dict:
    return {"page": page, "type": ftype, "severity": "info",
            "source": "llm_page", "description": desc}


class TestBlankColumns:
    def test_real_p3_confirm_column_is_blank(self):
        """实测那一列：表头「确认」，**每一行都空**（含 rowspan 行）。

        夹具是原文的 9 行截取（真页 13 行），所以断言 9 而不是 13 ——
        断言真实页面的行数只会让夹具与真页绑死，改夹具就得改断言。
        """
        assert blank_columns(REAL_P3) == {"确认": 9}

    def test_rowspan_does_not_hide_the_column(self):
        """第一列有 rowspan ⇒ 后续行少一个 `<td>`；不展开网格就会把整表筛掉。

        这是本模块最容易写错的地方：按"列数相等"筛选，p3 这张表一行都留不下。
        """
        assert "确认" in blank_columns(REAL_P3)

    def test_filled_column_is_not_reported(self):
        cols = blank_columns(REAL_P3)
        assert "设备位号" not in cols and "设备名称" not in cols

    def test_single_data_row_is_ignored(self):
        """一行全空可能只是没填 —— 不够格当"抽取失败"。"""
        html = ('<table><tr><td>名称</td><td>确认</td></tr>'
                '<tr><td>预处理罐</td><td></td></tr></table>')
        assert blank_columns(html) == {}

    def test_numeric_header_is_not_a_header(self):
        """表头里有纯数字/日期 ⇒ 那不是表头（是数据行）⇒ 整表跳过。

        实测 `0c5cfb00` p8 的一行 `16:22 / 3.6 / 3.5` 被旧算法当成表头，
        于是"下面几列全空"成了假信号。
        """
        html = ('<table><tr><td>16:22</td><td>3.6</td><td>3.5</td></tr>'
                '<tr><td></td><td></td><td></td></tr>'
                '<tr><td></td><td></td><td></td></tr></table>')
        assert blank_columns(html) == {}

    def test_mostly_empty_table_is_ignored(self):
        """表格本身大半是空的 ⇒ 是"空表"，不是"某列空"。"""
        html = ('<table><tr><td>名称</td><td>位号</td><td>确认</td></tr>'
                '<tr><td></td><td></td><td></td></tr>'
                '<tr><td></td><td></td><td></td></tr></table>')
        assert blank_columns(html) == {}

    def test_no_table_and_none_are_safe(self):
        assert blank_columns("") == {}
        assert blank_columns(None) == {}
        assert blank_columns("<p>纯文本页</p>") == {}


class TestClaimDetection:
    def test_real_claim_matches(self):
        assert claimed_blank_column(REAL_CLAIM, {"确认": 13}) == "确认"

    def test_single_row_claim_does_not_match(self):
        """「第3行确认栏空白」是单格问题 —— 不能改判成整栏不可判。"""
        assert claimed_blank_column("第3行确认栏空白，未勾选。", {"确认": 13}) is None

    def test_claim_about_a_filled_column_does_not_match(self):
        assert claimed_blank_column("所有设备的设备位号栏均为空白。", {"确认": 13}) is None

    def test_cross_sentence_match_is_rejected(self):
        """系统性措辞与列名分处两句 ⇒ 不是同一个断言。

        实测文案常把"某格缺失"和"整栏空"写在一段里，跨句匹配会把单格问题
        误改成不可判。
        """
        desc = "所有设备的确认栏均已签字。另一处：楼号栏空白。"
        assert claimed_blank_column(desc, {"楼号": 3}) is None

    def test_no_columns_means_no_match(self):
        assert claimed_blank_column(REAL_CLAIM, {}) is None


class TestRetype:
    def test_retypes_to_ocr_noise_with_honest_wording(self):
        out, changes = retype_blank_column_claims([_finding(REAL_CLAIM)], {3: REAL_P3})
        assert len(changes) == 1
        f = out[0]
        assert f["type"] == "ocr_noise"
        assert f["severity"] == "info"
        assert "整列未识别到任何内容" in f["description"]
        assert "9 行全空" in f["description"]
        assert "无法判断是未填写还是 OCR 未识别" in f["description"]
        assert "请核对原页" in f["description"]

    def test_original_text_kept_in_guard_reason(self):
        """改判要可追溯：原文进 `guard_reason`（R97：内部语不拼进 description）。"""
        out, _ = retype_blank_column_claims([_finding(REAL_CLAIM)], {3: REAL_P3})
        assert "确认" in out[0]["guard_reason"]
        assert "R106" in out[0]["guard_reason"]
        assert "缺少确认签名" not in out[0]["description"]

    def test_input_is_not_mutated(self):
        """`apply_review` 的既有约定：不改入参 dict。"""
        src = _finding(REAL_CLAIM)
        retype_blank_column_claims([src], {3: REAL_P3})
        assert src["type"] == "completeness"
        assert src["description"] == REAL_CLAIM

    def test_non_matching_findings_pass_through_untouched(self):
        keep = _finding("第7步操作人签名缺失。", ftype="completeness")
        out, changes = retype_blank_column_claims([keep], {3: REAL_P3})
        assert changes == []
        assert out[0] is keep

    def test_page_without_raw_html_is_safe(self):
        out, changes = retype_blank_column_claims([_finding(REAL_CLAIM)], {})
        assert changes == [] and out[0]["type"] == "completeness"

    def test_non_dict_entries_pass_through(self):
        out, _ = retype_blank_column_claims([None, "x"], {3: REAL_P3})
        assert out == [None, "x"]


class TestGuardWiring:
    """判据写好了还得接进写路径 —— 否则等于没修。"""

    def test_apply_review_retypes_the_real_case(self):
        out, suppressed = apply_review(
            [_finding(REAL_CLAIM)], structured_by_page={3: {}}, raw_by_page={3: REAL_P3},
        )
        assert suppressed == []
        assert out[0]["type"] == "ocr_noise"
        assert "整列未识别到任何内容" in out[0]["description"]

    def test_apply_review_keeps_a_single_row_claim(self):
        """反向控制：单格缺失照常保留（那是真的记录不完整）。"""
        out, _ = apply_review(
            [_finding("第3行确认栏空白，未勾选。")],
            structured_by_page={3: {}}, raw_by_page={3: REAL_P3},
        )
        assert out[0]["type"] == "completeness"
