"""#136 空清单文案的"标记新鲜度"契约 —— 行为 + 渲染顺序双护栏。

**要防的失效模式**（对抗审查发现，与 #136 同族）：

`renderFindings([])` 经 `emptyFindingsNote()` 读 `state.currentPageFlags`
区分三种"清单为空"的原因（分析失败 / OCR 空页 / 确实无问题）。该标记的
**唯一写点**是 `review-pageinfo.updatePageLevelUI`。而 `loadPageData` /
`refreshCurrentPageFindings` 曾经**先渲染后写标记** —— 翻页时会拿
**上一页**的标记渲染本页空清单：

    上一页无异常 → 翻到一页 `_parse_error` 的页 → 显示"本页无问题"
    ⇒ 解析失败页被伪装成合规页（GMP 假阴性，#136 要消灭的正是这个形态）。

本文件钉两件事，缺一即洞重开：

1. **行为**（node 实跑 `emptyFindingsNote`）：三种标记组合输出互不相同，
   clean 分支不得含失败/空页措辞，两标记同真时取"更严重"的解析失败。
2. **顺序**（源码相对位置）：两个数据层函数里 `updatePageLevelUI` 的
   **调用点**必须先于 `renderFindings` 的调用点。

⚠️ 顺序断言必须锚定**调用点**（`R.pageinfo.updatePageLevelUI(` /
`R.findings.renderFindings(`），不能只查标识符 —— 函数内注释也会出现
这两个名字，锚错就会变成假绿（本文件第一版即踩此坑）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STATIC = REPO / "static"
REVIEW_JS = STATIC / "review.js"
PAGEINFO_JS = STATIC / "review-pageinfo.js"
FINDINGS_JS = STATIC / "review-findings.js"
FINDINGS_MAP_JS = STATIC / "findings-map.js"

# 顺序断言的调用点锚（唯一、无歧义；注释里出现同名标识符不会误命中）
_WRITE_CALL = "R.pageinfo.updatePageLevelUI("
_READ_CALL = "R.findings.renderFindings("


def _run_probe(js_files: list[Path], stub: str, probe: str):
    """stub + 目标 JS 文件 + 探针拼接后交给 node，返回探针末行 JSON。"""
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    src = (
        stub
        + "\n"
        + "\n".join(p.read_text(encoding="utf-8") for p in js_files)
        + "\n"
        + probe
    )
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "probe.js"
        p.write_text(src, encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def _empty_notes() -> dict:
    """四种 currentPageFlags 组合下 emptyFindingsNote 的原始 HTML。"""
    stub = (
        "globalThis.PbcReview = { log: function(){}, "
        "state: { currentPageFlags: {} } };"
    )
    probe = """
const R = globalThis.PbcReview;
const out = {};
for (const [name, flags] of Object.entries({
  parseError: {parseError:true, ocrEmpty:false},
  ocrEmpty: {parseError:false, ocrEmpty:true},
  clean: {parseError:false, ocrEmpty:false},
  both: {parseError:true, ocrEmpty:true},
})) { R.state.currentPageFlags = flags; out[name] = R.findings.emptyFindingsNote(); }
console.log(JSON.stringify(out));
"""
    return _run_probe([FINDINGS_MAP_JS, FINDINGS_JS], stub, probe)


def _inner_text(html: str) -> str:
    """剥掉外层 div 与内联标签，取可见文案。"""
    m = re.search(r"<div[^>]*>(.*)</div>\s*$", html, re.S)
    assert m, f"emptyFindingsNote 输出结构变了（外层 div 不匹配）：{html!r}"
    return re.sub(r"<[^>]+>", "", m.group(1)).strip()


def _fn_body(src: str, name: str) -> str:
    """花括号配平截取函数体（含声明行）—— 与既有护栏同款提取法。"""
    m = re.search(rf"(?:async\s+)?function\s+{name}\s*\([^)]*\)\s*\{{", src)
    assert m, f"review.js 未找到 function {name} —— 改名/移动了？本护栏需同步"
    i = m.end()
    depth = 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"function {name} 的花括号未配平（解析失败）"
    return src[m.start():i]


class TestEmptyNoteIsBehavioral:
    """三种"清单为空"的原因必须视觉上不可混淆（#136 的核心语义）。"""

    def test_probe_is_not_vacuous(self):
        """防空转：提取器/探针坏了会让下面的断言变成空断言。"""
        notes = _empty_notes()
        assert set(notes) == {"parseError", "ocrEmpty", "clean", "both"}
        for v in notes.values():
            assert isinstance(v, str) and v, "emptyFindingsNote 返回空串"

    def test_clean_branch_is_exactly_the_reassuring_text(self):
        assert _inner_text(_empty_notes()["clean"]) == "本页无问题"

    def test_parse_error_branch_warns_the_list_is_not_conclusive(self):
        txt = _inner_text(_empty_notes()["parseError"])
        assert "未能分析" in txt
        assert "不代表本页无问题" in txt

    def test_ocr_empty_branch_says_analysis_was_skipped(self):
        txt = _inner_text(_empty_notes()["ocrEmpty"])
        assert "无 OCR 内容" in txt
        assert "未执行分析" in txt

    def test_three_branches_are_distinct(self):
        notes = {k: _inner_text(v) for k, v in _empty_notes().items()}
        assert len({notes["clean"], notes["parseError"], notes["ocrEmpty"]}) == 3, (
            "三种空清单原因出现相同文案 ⇒ 失败页会被误读为合规页"
        )

    def test_parse_error_wins_over_ocr_empty(self):
        """两标记同真时取"更严重"的解析失败（判据顺序不可反转）。"""
        notes = _empty_notes()
        assert _inner_text(notes["both"]) == _inner_text(notes["parseError"])

    def test_clean_branch_never_masquerades_as_failure(self):
        """反向控制：合规页不得出现失败/空页措辞（否则复核者去翻没问题的页）。"""
        txt = _inner_text(_empty_notes()["clean"])
        assert "未能分析" not in txt
        assert "无 OCR 内容" not in txt

    def test_note_carries_stable_id(self):
        """SSR 首屏兜底（applyInitialPageLevelUI）按 id 替换该节点 ⇒ id 必须稳定。"""
        for html in _empty_notes().values():
            assert 'id="findings-empty-note"' in html


class TestFlagWriterPrecedesReader:
    """翻页/刷新路径里，currentPageFlags 的写点必须先于读点。

    写点 = `review-pageinfo.updatePageLevelUI`（唯一）；
    读点 = `review-findings.renderFindings` → `emptyFindingsNote`。
    顺序颠倒 ⇒ 用上一页标记渲染本页空清单 ⇒ 解析失败页显示"本页无问题"。
    """

    def test_single_writer_and_reader_are_where_we_think(self):
        """正向锚点：写点/读点各归其位，且入口文件不得出现第二个写点。"""
        pageinfo = PAGEINFO_JS.read_text(encoding="utf-8")
        findings = FINDINGS_JS.read_text(encoding="utf-8")
        entry = REVIEW_JS.read_text(encoding="utf-8")
        assert "state.currentPageFlags =" in pageinfo, (
            "currentPageFlags 的写点不在 review-pageinfo.js —— 本护栏锚点失效"
        )
        assert "currentPageFlags" in findings, (
            "renderFindings 不再读 currentPageFlags —— 空清单文案判据被删？"
        )
        assert "currentPageFlags =" not in entry, (
            "review.js 出现了第二个 currentPageFlags 写点 —— 多写点必然漂移"
        )

    def test_load_page_data_writes_flags_before_rendering(self):
        body = _fn_body(REVIEW_JS.read_text(encoding="utf-8"), "loadPageData")
        i_write = body.find(_WRITE_CALL)
        i_render = body.find(_READ_CALL)
        assert i_write >= 0 and i_render >= 0, (
            "loadPageData 缺少 updatePageLevelUI 或 renderFindings **调用点**"
            "（提取器失效？）"
        )
        assert i_write < i_render, (
            "loadPageData 先 renderFindings 后 updatePageLevelUI —— 翻页会用"
            "**上一页**的 currentPageFlags 渲染本页空清单（解析失败页显示"
            "'本页无问题'，GMP 假阴性）。必须先写标记再渲染。"
        )

    def test_refresh_writes_flags_before_rendering(self):
        body = _fn_body(
            REVIEW_JS.read_text(encoding="utf-8"), "refreshCurrentPageFindings"
        )
        i_write = body.find(_WRITE_CALL)
        i_render = body.find(_READ_CALL)
        assert i_write >= 0 and i_render >= 0, (
            "refreshCurrentPageFindings 缺少写点或读点调用（提取器失效？）"
        )
        assert i_write < i_render, (
            "refreshCurrentPageFindings 顺序颠倒：先渲染后写标记 ⇒ 本页"
            "重分析写入的 _parse_error 无法即时反映到空清单文案。"
        )
