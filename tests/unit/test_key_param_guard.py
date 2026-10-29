"""TODO 0-72 护栏：**关键参数 + 手写**的「合格」判定必须交人工，不得静默通过。

由来（2026-10-29 实测，51 页真实批记录 p6）：原页「中和后 pH」手写为 **1.88**
（规格 6.8~7.0 ⇒ 越界），而 Paddle 整页、Paddle 400dpi 区域裁切、MinerU
独立引擎**三次都读成 6.88** ⇒ 规则层判"合格"、无 finding（假阴性）。

这条实测同时否证了"区域二次读"这条路：两引擎在这支手写体上**失效同源**，
双读一致反而是安慰剂。因此护栏只能建立在**确定性可见的信号**上 ——
原页把关键参数标了 ▲（关键工艺参数）/ ★（质量控制点），而"值读得对不对"
不可见 ⇒ 关键参数的手写读数即使落在规格内也标"需人工核对原值"。
"""
from __future__ import annotations

from core.rules.finding_aggregate import (
    CAUSE_KEY_PARAM_UNVERIFIED,
    UNVERIFIABLE_CAUSES,
    expected_type,
)
from core.rules.base import _normalize_pages
from core.rules.rule_spec import (
    _check_param_out_of_spec,
    _key_param_marked,
    _norm_literal,
)


def _page(raw_html: str, *, parameters=(), measurements=()):
    """构造与 pipeline 一致的页面结构（``raw_html`` 在页级，结构化数据在 data）。"""
    step = {"step_no": "4"}
    if parameters:
        step["parameters"] = list(parameters)
    if measurements:
        step["measurements"] = list(measurements)
    return {
        "page": 6,
        "raw_html": raw_html,
        "data": {"steps": [step], "findings": [], "page_info": {}},
    }


def _run(pages):
    llm_queue: list = []
    findings = _check_param_out_of_spec(_normalize_pages(pages), llm_queue)
    return findings, llm_queue


def _param(name, spec, value, unit="", source="handwritten"):
    return {"name": name, "spec_range": spec, "value": value, "unit": unit,
            "value_source": source}


#: 真实 p6 指导栏原文（HTML 表格里的一格）＋ 页脚图例 —— 两处都有 ▲/★，
#: 只有前者紧邻规格字面量，这正是判据要区分的。
_REAL_TEXT = (
    "<td>中和：用磷酸调节 R2101 预处理罐发酵液 ▲pH为 6.8~7.0。</td>"
    "<td>磷酸批号 A016368-241201Y</td>"
    "<td>中和后 pH</td><td>70 L</td>"
    "...页脚...备注：▲有下划线标的为关键工艺参数，★有下划线标的为质量控制点。"
)


class TestKeyParamMarked:
    def test_marker_before_spec_hits(self):
        assert _key_param_marked(_REAL_TEXT, "6.8~7.0") is True

    def test_footnote_legend_alone_does_not_hit(self):
        """页脚图例里同时有 ▲ 和 ★，但窗口内没有规格字面量 ⇒ 不命中。"""
        assert _key_param_marked("备注：▲关键工艺参数，★质量控制点。", "6.8~7.0") is False

    def test_spec_variants_normalize(self):
        """原页写 `6.8~7.0`、规格字段写 `6.8 - 7.0` ⇒ 仍命中（连接符归一）。"""
        assert _norm_literal("6.8 - 7.0") == _norm_literal("6.8~7.0")
        assert _key_param_marked(_REAL_TEXT, "6.8 - 7.0") is True

    def test_far_marker_does_not_hit(self):
        """标记与规格相距过远（> 窗口）⇒ 不命中，避免整页误标。"""
        text = "▲" + "无关文字" * 40 + "6.8~7.0"
        assert _key_param_marked(text, "6.8~7.0") is False

    def test_empty_or_short_inputs(self):
        assert _key_param_marked("", "6.8~7.0") is False
        assert _key_param_marked(_REAL_TEXT, "") is False
        assert _key_param_marked(_REAL_TEXT, "7") is False  # 太短，防误命中


class TestInSpecKeyParamGoesToHuman:
    def test_handwritten_key_param_in_spec_emits_unverifiable(self):
        """0-72 本体：判合格**且**是关键参数手写值 ⇒ 必须产出"需人工核对"。"""
        pages = [_page(_REAL_TEXT, parameters=[_param("中和后 pH", "6.8~7.0", "6.88")])]
        findings, llm_queue = _run(pages)
        assert llm_queue == [], "能判的不得推给 LLM 兜底"
        assert len(findings) == 1
        f = findings[0]
        assert f["type"] == "spec_unverifiable"
        assert f["severity"] == "info"
        assert CAUSE_KEY_PARAM_UNVERIFIED in f["description"] or "关键参数" in f["description"]
        assert "6.88" in f["ocr_text"]
        # 判定结果本身仍是 True：超差结论没有被伪造，只是不再"静默"
        assert pages[0]["data"]["steps"][0]["parameters"][0]["in_spec"] is True

    def test_handwritten_without_marker_no_finding(self):
        """同样的值，但原页没标 ▲/★ ⇒ 不产出（避免把手写一律降级）。"""
        text = "中和：用磷酸调节 R2101 预处理罐发酵液 pH为 6.8~7.0。"
        findings, _ = _run([_page(text, parameters=[_param("中和后 pH", "6.8~7.0", "6.88")])])
        assert findings == []

    def test_printed_key_param_no_finding(self):
        """印刷体数值不受影响（印刷体读数可信，无需人工核对）。"""
        findings, _ = _run([_page(_REAL_TEXT, parameters=[
            _param("中和后 pH", "6.8~7.0", "6.88", source="printed")])])
        assert findings == []

    def test_matrix_cell_path_also_guarded(self):
        """矩阵单元格（measurements）走同一判定实现，必须同样受护栏约束。"""
        cell = {"actual": "6.88", "spec": "6.8~7.0", "unit": "",
                "value_source": "handwritten"}
        pages = [_page(_REAL_TEXT, measurements=[{"time": "09:52", "values": {"中和后 pH": cell}}])]
        findings, _ = _run(pages)
        assert len(findings) == 1
        assert findings[0]["type"] == "spec_unverifiable"
        assert "09:52" in findings[0]["description"]


class TestCauseRegistration:
    def test_cause_maps_to_spec_unverifiable(self):
        """类型必须由根因派生（CR-12④），不得由调用方自选。"""
        assert expected_type(CAUSE_KEY_PARAM_UNVERIFIED) == "spec_unverifiable"
        assert CAUSE_KEY_PARAM_UNVERIFIED in UNVERIFIABLE_CAUSES
