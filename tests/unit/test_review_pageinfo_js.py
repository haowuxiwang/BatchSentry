"""review-pageinfo.js 的行为护栏（此前只有**源码字符串**断言，零行为覆盖）。

**为什么必须测**：本模块是翻页时"页面级 UI 同步"的唯一落点。它此前只有
`test_config_error_visibility.py::test_page_banner_surfaces_actual_error_text`
这类**源码扫描**断言 —— 能证明"文案写在代码里"，但不能证明"横幅真的会显示/
隐藏"。而这里每一条横幅都对应一个 GMP 误导场景：

- 置信度徽章在 parse-error 页仍显示 → 复核者以为这页解析可信；
- 上一页的 critical 计数残留到当前页 → 漏判严重缺陷；
- OCR 不完整横幅没更新 → 复核者按"完整数据"下结论（假阴性）；
- 参数矩阵残留上一页 → 按错误数值判定超标。

用 `tests/js_harness.py` 的假 DOM 真实调用 `updatePageLevelUI`，断言落在
**可观测决策**上（class 是否含 hidden、textContent、请求体、toast）。
"""
from __future__ import annotations

import pytest

from tests.js_harness import run_js_async

FILES = ["findings-map.js", "review-state.js", "review-pageinfo.js"]

# 服务端注入上下文 + 可编程的 fetch / PBC 弹窗桩。
# 加载序：DOM_STUB → 本桩 → findings-map → review-state → review-pageinfo
_STUB = r"""
globalThis.__PBC__ = { job_id: "J1", page: 1, total_pages: 3 };
globalThis.__fetchQueue = [];
globalThis.__promptQueue = [];
globalThis.__confirmQueue = [];
globalThis.__loadPageCalls = [];
globalThis.fetch = async (url, opts) => {
  __calls.fetch.push({ url, opts });
  const spec = __fetchQueue.length ? __fetchQueue.shift() : { ok: true, status: 200, json: {} };
  return { ok: spec.ok, status: spec.status, json: async () => (spec.json || {}) };
};
window.PBC.promptDialog = async () => (__promptQueue.length ? __promptQueue.shift() : null);
window.PBC.confirmDialog = async () => (__confirmQueue.length ? __confirmQueue.shift() : false);
"""

# 公共前导：断言助手 + 元素构造助手。`settle()` 排空微任务（setImmediate 未被接管）。
_PRE = r"""
const R = window.PbcReview;
const UI = R.pageinfo;
globalThis.__calls = { fetch: [] };
R.loadPageData = (p) => { __loadPageCalls.push(p); };

const mk = (id, tag) => __el(id, tag);
const kid = (parent, tag, cls) => {
  const c = document.createElement(tag);
  if (cls) c.className = cls;
  parent.appendChild(c);
  return c;
};
const settle = () => new Promise((r) => setImmediate(r));
const shown = (id) => {
  const e = document.getElementById(id);
  return !!e && !e.classList.contains("hidden");
};
const flags = () => R.state.currentPageFlags;
"""


def _probe(body: str):
    return run_js_async(FILES, _PRE + "\n" + body, stub=_STUB)


# ─────────────────────────────────────────────────────────────────────
# #136：currentPageFlags 必须**无条件**写入（空 findings 文案的唯一依赖）
# ─────────────────────────────────────────────────────────────────────
class TestCurrentPageFlagsAlwaysWritten:
    """`emptyFindingsNote()` 靠 `currentPageFlags` 区分"解析失败 / 空页 / 无问题"。

    翻页后若标记没更新，空 findings 会套用上一页的判据 —— 把"解析失败"
    显示成"确实无异常"（GMP 假阴性）。故标记的写入**不得**被任何 DOM
    前置条件或提前 return 挡住。
    """

    def test_flags_written_when_no_dom_present_at_all(self):
        """页面上**一个元素都没有**时也必须写入标记。

        这是最容易被重构破坏的形态：若有人在函数开头加
        `if (!confEl) return;` 之类的前置守卫，标记写入就被跳过，
        而页面上不会报任何错 —— 只有翻页后的空 findings 文案悄悄错。
        """
        body = """
R.state.currentPageFlags = { parseError: false, ocrEmpty: false };
UI.updatePageLevelUI({ structured: { _parse_error: true, _ocr_empty: "true" } }, [], {});
console.log(JSON.stringify(flags()));
"""
        assert _probe(body) == {"parseError": True, "ocrEmpty": True}

    def test_flags_reset_on_a_clean_page(self):
        """反向：从失败页翻到干净页，标记必须**回落**为 false。

        若只写 true 不写 false（例如 `if (x) state.x = true`），上一页的
        失败标记会永久残留 —— 干净页的空 findings 被误报为"解析失败"。
        """
        body = """
R.state.currentPageFlags = { parseError: true, ocrEmpty: true };
UI.updatePageLevelUI({ structured: {} }, [], {});
console.log(JSON.stringify(flags()));
"""
        assert _probe(body) == {"parseError": False, "ocrEmpty": False}

    def test_string_truthy_forms_are_coerced(self):
        """后端 JSON 里 `_parse_error` 可能是 `"true"`/`1`，一律按 R.bool 归一。"""
        body = """
UI.updatePageLevelUI({ structured: { _parse_error: 1, _ocr_empty: "true" } }, [], {});
const a = flags();
UI.updatePageLevelUI({ structured: { _parse_error: "false", _ocr_empty: 0 } }, [], {});
console.log(JSON.stringify([a, flags()]));
"""
        assert _probe(body) == [{"parseError": True, "ocrEmpty": True}, {"parseError": False, "ocrEmpty": False}]


# ─────────────────────────────────────────────────────────────────────
# 置信度徽章
# ─────────────────────────────────────────────────────────────────────
class TestConfidenceBadge:
    def test_maps_to_chinese_and_shows(self):
        body = """
mk("page-confidence-badge");
UI.updatePageLevelUI({ structured: { overall_confidence: "high" } }, [], {});
const b = document.getElementById("page-confidence-badge");
console.log(JSON.stringify({ text: b.textContent, shown: shown("page-confidence-badge") }));
"""
        assert _probe(body) == {"text": "置信度 高", "shown": True}

    def test_hidden_on_parse_error_even_if_confidence_present(self):
        """解析失败页**不得**展示置信度 —— 那是上一轮成功解析的残留值，
        展示它等于给失败页盖"可信"戳（GMP 误导）。"""
        body = """
mk("page-confidence-badge");
UI.updatePageLevelUI({ structured: { overall_confidence: "high", _parse_error: true } }, [], {});
console.log(JSON.stringify({ shown: shown("page-confidence-badge") }));
"""
        assert _probe(body) == {"shown": False}

    def test_hidden_when_confidence_absent(self):
        body = """
mk("page-confidence-badge");
UI.updatePageLevelUI({ structured: {} }, [], {});
console.log(JSON.stringify({ shown: shown("page-confidence-badge") }));
"""
        assert _probe(body) == {"shown": False}


# ─────────────────────────────────────────────────────────────────────
# parse-error 横幅（#127：必须写出**实际原因**）
# ─────────────────────────────────────────────────────────────────────
class TestParseErrorBanner:
    def test_shows_with_actual_reason_trimmed(self):
        body = """
mk("parse-error-banner"); mk("parse-error-text");
UI.updatePageLevelUI({ structured: { _parse_error: true, _error: "  网关超时  " } }, [], {});
const t = document.getElementById("parse-error-text");
console.log(JSON.stringify({ shown: shown("parse-error-banner"), text: t.textContent }));
"""
        assert _probe(body) == {"shown": True, "text": "此页 LLM 解析失败：网关超时"}

    def test_fallback_text_is_read_from_dom_not_hardcoded(self):
        """通用兜底文案的**唯一副本在模板里**：JS 从首帧 textContent 读并缓存。

        这条锁"单一真值"——若有人在 JS 里再抄一份字面量，模板改文案后
        这里就漂移（用户看到两套说法）。
        """
        body = """
mk("parse-error-banner"); const t = mk("parse-error-text");
t.textContent = "模板里的兜底文案";
UI.updatePageLevelUI({ structured: { _parse_error: true } }, [], {});
const first = t.textContent;
// 再翻一次：应复用缓存的兜底，而不是空字符串或硬编码
UI.updatePageLevelUI({ structured: { _parse_error: true } }, [], {});
console.log(JSON.stringify({ first, second: t.textContent, cached: t.dataset.fallback }));
"""
        assert _probe(body) == {
            "first": "模板里的兜底文案",
            "second": "模板里的兜底文案",
            "cached": "模板里的兜底文案",
        }

    def test_hidden_on_clean_page(self):
        body = """
mk("parse-error-banner"); mk("parse-error-text");
UI.updatePageLevelUI({ structured: {} }, [], {});
console.log(JSON.stringify({ shown: shown("parse-error-banner") }));
"""
        assert _probe(body) == {"shown": False}


# ─────────────────────────────────────────────────────────────────────
# 幻觉 / 截断 / schema 横幅
# ─────────────────────────────────────────────────────────────────────
class TestGroundingAndIntegrityBanners:
    def test_grounding_banner_lists_warnings(self):
        body = """
mk("grounding-warn-banner"); kid(mk("grounding-warn-banner"), "div", "grounding-warn-text");
UI.updatePageLevelUI(
  { structured: { _grounding_warn: ["批号 X 未见于原文", "数值 12.5 未见于原文"] } }, [], {});
const t = document.getElementById("grounding-warn-banner").querySelector(".grounding-warn-text");
console.log(JSON.stringify({ shown: shown("grounding-warn-banner"), text: t.textContent }));
"""
        assert _probe(body) == (
            {"shown": True, "text": "批号 X 未见于原文；数值 12.5 未见于原文"}
        )

    def test_grounding_banner_hidden_when_empty(self):
        body = """
mk("grounding-warn-banner"); kid(mk("grounding-warn-banner"), "div", "grounding-warn-text");
UI.updatePageLevelUI({ structured: {} }, [], {});
console.log(JSON.stringify({ shown: shown("grounding-warn-banner") }));
"""
        assert _probe(body) == {"shown": False}

    def test_llm_truncation_message(self):
        body = """
mk("llm-integrity-banner");
const p = kid(mk("llm-integrity-banner"), "p", "text-xs font-medium");
UI.updatePageLevelUI({ structured: { _truncated_warn: true } }, [], {});
console.log(JSON.stringify({ shown: shown("llm-integrity-banner"), text: p.textContent }));
"""
        got = _probe(body)
        assert got["shown"] is True
        assert "截断" in got["text"], got["text"]

    def test_schema_warn_only(self):
        body = """
mk("llm-integrity-banner");
const b = mk("llm-integrity-banner");
const p = kid(b, "p", "text-xs font-medium");
const st = kid(b, "div", "llm-schema-warn-text");
UI.updatePageLevelUI({ structured: { _schema_warn: ["字段 A 类型不符"] } }, [], {});
console.log(JSON.stringify({ shown: shown("llm-integrity-banner"), text: p.textContent,
                             schema: st.textContent, schemaHidden: st.classList.contains("hidden") }));
"""
        got = _probe(body)
        assert got["shown"] is True
        assert "结构校验未完全通过" in got["text"], got["text"]
        assert got["schema"] == "字段 A 类型不符"
        assert got["schemaHidden"] is False

    def test_both_truncation_and_schema_joined_with_semicolon(self):
        body = """
mk("llm-integrity-banner");
const b = mk("llm-integrity-banner");
const p = kid(b, "p", "text-xs font-medium");
UI.updatePageLevelUI({ structured: { _truncated_warn: true, _schema_warn: ["x"] } }, [], {});
console.log(JSON.stringify({ text: p.textContent }));
"""
        got = _probe(body)
        assert "截断" in got["text"] and "结构校验未完全通过" in got["text"]
        assert "）；" in got["text"], got["text"]

    def test_clean_page_hides_and_clears_schema_text(self):
        """从有 schema 告警的页翻到干净页：横幅隐藏**且**残留文案被清空
        （只加 hidden 不清文案，下次显示会闪出上一页的告警）。"""
        body = """
mk("llm-integrity-banner");
const b = mk("llm-integrity-banner");
kid(b, "p", "text-xs font-medium");
const st = kid(b, "div", "llm-schema-warn-text");
st.textContent = "上一页的告警";
UI.updatePageLevelUI({ structured: {} }, [], {});
console.log(JSON.stringify({ shown: shown("llm-integrity-banner"),
                             schema: st.textContent,
                             schemaHidden: st.classList.contains("hidden") }));
"""
        assert _probe(body) == {"shown": False, "schema": "", "schemaHidden": True}


# ─────────────────────────────────────────────────────────────────────
# OCR 状态横幅
# ─────────────────────────────────────────────────────────────────────
class TestOcrBanners:
    def test_empty_and_sparse_toggle_independently(self):
        body = """
mk("ocr-empty-banner"); mk("ocr-sparse-banner");
UI.updatePageLevelUI({ structured: { _ocr_empty: true, _ocr_sparse: true } }, [], {});
const a = [shown("ocr-empty-banner"), shown("ocr-sparse-banner")];
UI.updatePageLevelUI({ structured: {} }, [], {});
const b = [shown("ocr-empty-banner"), shown("ocr-sparse-banner")];
console.log(JSON.stringify([a, b]));
"""
        assert _probe(body) == [[True, True], [False, False]]

    def test_warning_banner_text_includes_reason_and_verdict(self):
        body = """
const b = mk("ocr-warning-banner");
const sp = kid(b, "span", "text-xs");
UI.updatePageLevelUI({ structured: { _ocr_warning: "右侧装订线裁切" } }, [], {});
console.log(JSON.stringify({ shown: shown("ocr-warning-banner"), text: sp.textContent }));
"""
        got = _probe(body)
        assert got["shown"] is True
        assert "右侧装订线裁切" in got["text"]
        assert "以 PDF 原图核对" in got["text"]

    def test_integrity_banner_shows_reasons(self):
        body = """
mk("ocr-integrity-banner"); mk("ocr-integrity-text"); mk("ocr-detail-text");
UI.updatePageLevelUI(
  { structured: {}, ocr_diagnostics: { integrity: "incomplete", reasons: ["DPI 过低", "文本量异常"] } },
  [], {});
const t = document.getElementById("ocr-integrity-text");
console.log(JSON.stringify({ shown: shown("ocr-integrity-banner"), text: t.textContent }));
"""
        got = _probe(body)
        assert got["shown"] is True
        assert "DPI 过低" in got["text"] and "文本量异常" in got["text"]

    def test_integrity_banner_suppressed_when_ocr_warning_present(self):
        """`_ocr_warning` 与 integrity=incomplete 同时存在时，**只显示警告横幅**。

        两者讲的是同一件事；同时弹出会让复核者以为有两类独立问题
        （重复告警稀释注意力）。这条锁住 `&& !structured._ocr_warning` 的抑制逻辑。
        """
        body = """
mk("ocr-integrity-banner"); mk("ocr-integrity-text"); mk("ocr-detail-text");
mk("ocr-warning-banner"); kid(mk("ocr-warning-banner"), "span", "text-xs");
UI.updatePageLevelUI(
  { structured: { _ocr_warning: "裁切" },
    ocr_diagnostics: { integrity: "incomplete", reasons: ["x"] } }, [], {});
console.log(JSON.stringify({ integrity: shown("ocr-integrity-banner"),
                             warning: shown("ocr-warning-banner") }));
"""
        assert _probe(body) == {"integrity": False, "warning": True}

    def test_diagnostic_detail_joins_self_heal_and_rotation(self):
        """门禁 1 诊断详情：自愈/旋转探测必须透出 —— 复核者要知道"此页文本
        是恢复产物"，否则会把恢复误差当成原始识别结果。"""
        body = """
mk("ocr-integrity-banner"); mk("ocr-integrity-text"); mk("ocr-detail-text");
UI.updatePageLevelUI({ structured: {}, ocr_diagnostics: {
  integrity: "incomplete", reasons: ["r"], effective_dpi: 96, low_dpi: true,
  media_box_pt: [595.4, 841.7], rotation: 90, self_healed: true, rotation_deg: 90,
  rotation_probed: true, aspect_ratio: 0.71, text_chars: 12,
} }, [], {});
console.log(JSON.stringify({ detail: document.getElementById("ocr-detail-text").textContent }));
"""
        d = _probe(body)["detail"]
        for needle in [
            "有效 DPI=96（低）", "页面盒=595×842pt", "旋转=90°",
            "已自愈（横置页按 90° 重渲染后识别）",
            "已尝试 90/270/180° 旋转恢复未果", "长宽比=0.71", "文本=12 字符",
        ]:
            assert needle in d, f"诊断详情缺少 {needle!r}：{d}"


# ─────────────────────────────────────────────────────────────────────
# critical 横幅 + 参数矩阵
# ─────────────────────────────────────────────────────────────────────
class TestCriticalBanner:
    def test_counts_only_critical(self):
        body = """
const b = mk("critical-banner");
const s = kid(b, "strong");
UI.updatePageLevelUI({ structured: {} },
  [{ severity: "critical" }, { severity: "critical" }, { severity: "warning" }], {});
console.log(JSON.stringify({ shown: shown("critical-banner"), n: s.textContent }));
"""
        assert _probe(body) == {"shown": True, "n": "2"}

    def test_hidden_when_no_critical(self):
        body = """
const b = mk("critical-banner"); kid(b, "strong");
UI.updatePageLevelUI({ structured: {} }, [{ severity: "warning" }], {});
console.log(JSON.stringify({ shown: shown("critical-banner") }));
"""
        assert _probe(body) == {"shown": False}


class TestMeasurementsMatrix:
    @staticmethod
    def _dom() -> str:
        return """
mk("measurements-section"); mk("measurements-header-row");
mk("measurements-body"); mk("measurements-shape");
"""

    def test_header_body_and_shape(self):
        body = self._dom() + """
UI.updatePageLevelUI({ structured: {} }, [], {
  columns: ["温度", "压力"],
  measurements: [
    { time: "08:00", values: { "温度": { actual: "25", in_spec: true },
                               "压力": { actual: "0.1", in_spec: false } } },
    { time: "12:00", values: {} },
  ],
});
const hdr = document.getElementById("measurements-header-row");
const bod = document.getElementById("measurements-body");
console.log(JSON.stringify({
  shown: shown("measurements-section"),
  header: hdr.children.map((c) => c.textContent),
  rows: bod.children.length,
  cellsInRow0: bod.children[0].children.length,
  shape: document.getElementById("measurements-shape").textContent,
}));
"""
        got = _probe(body)
        assert got["shown"] is True
        assert got["header"] == ["时间", "温度", "压力"], got["header"]
        assert got["rows"] == 2
        assert got["cellsInRow0"] == 3, "每行应为 时间 + 各列"
        assert got["shape"] == "2 × 2"

    def test_in_spec_tristate_drives_cell_class(self):
        """`in_spec` 三态（true / false / 缺失）必须映射到三个不同 class ——
        "未判定"绝不能长得像"合格"（否则复核者漏掉无法核定的项）。"""
        body = self._dom() + """
UI.updatePageLevelUI({ structured: {} }, [], {
  columns: ["A", "B", "C"],
  measurements: [{ time: "t", values: {
    A: { actual: "1", in_spec: true },
    B: { actual: "2", in_spec: false },
  } }],
});
const tds = document.getElementById("measurements-body").children[0].children;
console.log(JSON.stringify(tds.slice(1).map((td) => td.className)));
"""
        classes = _probe(body)
        assert "cell-ok" in classes[0]
        assert "cell-bad" in classes[1]
        assert "cell-unknown" in classes[2], f"未判定的单元格 class 不是 unknown：{classes}"

    def test_cell_title_carries_spec_actual_unit(self):
        body = self._dom() + """
UI.updatePageLevelUI({ structured: {} }, [], {
  columns: ["温度"],
  measurements: [{ time: "08:00", values: {
    "温度": { actual: "25", spec: "20~30", unit: "℃", in_spec: true } } }],
});
const td = document.getElementById("measurements-body").children[0].children[1];
console.log(JSON.stringify({ title: td.title, text: td.textContent }));
"""
        got = _probe(body)
        assert got["title"] == "规格: 20~30 | 实测: 25 | 单位: ℃"
        assert got["text"] == "25"

    def test_hidden_and_cleared_when_no_measurements(self):
        """无参数页必须隐藏矩阵，且**不得残留上一页的行**（隐藏但仍挂着旧行，
        一旦 CSS 变化或有脚本读 DOM，就会把上一页数值当成当前页）。"""
        body = self._dom() + """
UI.updatePageLevelUI({ structured: {} }, [], {
  columns: ["A"], measurements: [{ time: "t", values: {} }] });
const before = document.getElementById("measurements-body").children.length;
UI.updatePageLevelUI({ structured: {} }, [], { columns: [], measurements: [] });
console.log(JSON.stringify({ shown: shown("measurements-section"), before,
                             after: document.getElementById("measurements-body").children.length }));
"""
        got = _probe(body)
        assert got["shown"] is False
        assert got["before"] == 1

    def test_empty_measurements_data_object_does_not_crash(self):
        """`applyInitialPageLevelUI` 传的就是 `{}` —— 缺 columns/measurements
        时不得抛 TypeError（那会让整个首屏初始化中断）。"""
        body = self._dom() + """
UI.updatePageLevelUI({ structured: {} }, [], {});
console.log(JSON.stringify({ ok: true, shown: shown("measurements-section") }));
"""
        assert _probe(body) == {"ok": True, "shown": False}


# ─────────────────────────────────────────────────────────────────────
# applyInitialPageLevelUI（首屏与翻页同一条代码路径）
# ─────────────────────────────────────────────────────────────────────
class TestApplyInitialPageLevelUI:
    def test_reroutes_first_paint_through_update_page_level_ui(self):
        body = """
mk("parse-error-banner"); mk("parse-error-text");
R.state.ctx = { page_parse_error: true, page_parse_error_reason: "模型凭据失效" };
UI.applyInitialPageLevelUI();
const t = document.getElementById("parse-error-text");
console.log(JSON.stringify({ shown: shown("parse-error-banner"), text: t.textContent }));
"""
        got = _probe(body)
        assert got["shown"] is True
        assert "模型凭据失效" in got["text"]

    def test_no_dom_writes_when_first_page_is_clean(self):
        """干净首屏不得触碰任何横幅（否则会闪一下不该出现的告警）。"""
        body = """
const b = mk("parse-error-banner"); mk("parse-error-text");
UI.applyInitialPageLevelUI();
console.log(JSON.stringify({ cls: b.className, shown: shown("parse-error-banner") }));
"""
        assert _probe(body) == {"cls": "", "shown": True}

    def test_empty_note_replaced_only_when_findings_list_is_empty(self):
        """SSR 已渲染 findings 时**不得**覆盖清单；清单为空才补兜底文案。"""
        body = """
R.findings = { emptyFindingsNote: () => "SENTINEL_NOTE" };
const list = mk("findings-list");
const note = mk("findings-empty-note");
R.state.ctx = {};
UI.applyInitialPageLevelUI();
console.log(JSON.stringify({ note: note.outerHTML || "" }));
"""
        assert _probe(body) == {"note": "SENTINEL_NOTE"}

    def test_empty_note_untouched_when_a_finding_exists(self):
        body = """
R.findings = { emptyFindingsNote: () => "SENTINEL_NOTE" };
const list = mk("findings-list");
const card = document.createElement("div");
card.setAttribute("id", "finding-7");
list.appendChild(card);
const note = mk("findings-empty-note");
R.state.ctx = {};
UI.applyInitialPageLevelUI();
console.log(JSON.stringify({ note: note.outerHTML === undefined }));
"""
        assert _probe(body) == {"note": True}


# ─────────────────────────────────────────────────────────────────────
# OCR 豁免区（门禁 2）— 含真实点击 → 请求体 → toast 的闭环
# ─────────────────────────────────────────────────────────────────────
class TestOcrExemptionZone:
    def test_missing_zone_is_a_noop(self):
        body = """
UI.renderOcrExemptionZone({ integrity: "incomplete" }, 2);
console.log(JSON.stringify({ ok: true }));
"""
        assert _probe(body) == {"ok": True}

    def test_exempt_badge_escapes_reason(self):
        """reason 是**人工输入**，注入 HTML 前必须转义 —— 否则存储型 XSS
        （GMP 记录里塞脚本，复核者一打开就执行）。"""
        body = """
mk("ocr-exemption-zone");
UI.renderOcrExemptionZone(
  { integrity: "incomplete", exemption: { reason: '<img src=x onerror=alert(1)>', created_at: "2026-01-02" } }, 1);
const zone = document.getElementById("ocr-exemption-zone");
const html = zone.children[0].innerHTML;
console.log(JSON.stringify({ html, hasRawTag: html.includes("<img") }));
"""
        got = _probe(body)
        assert got["hasRawTag"] is False, f"reason 未转义，存储型 XSS：{got['html']}"
        assert "&lt;img" in got["html"]
        assert "已确认豁免" in got["html"]

    def test_exempt_badge_offers_revoke_that_posts(self):
        """撤销豁免：确认后 POST `revoke=1`，成功则 toast + 重载当前页。"""
        body = """
mk("ocr-exemption-zone");
__confirmQueue.push(true);
UI.renderOcrExemptionZone(
  { integrity: "incomplete", exemption: { reason: "已核对", created_at: "2026-01-02" } }, 4);
const btn = document.getElementById("ocr-exemption-zone").querySelector("[data-exempt-revoke]");
const found = !!btn;
if (btn) btn.dispatchEvent({ type: "click", stopPropagation() {} });
await settle(); await settle(); await settle();
console.log(JSON.stringify({ found, fetch: __calls.fetch.map((c) => ({ url: c.url, method: c.opts.method, body: c.opts.body })),
                             toast: __toasts.map((t) => [t.msg, t.type]), reload: __loadPageCalls }));
"""
        got = _probe(body)
        assert got["found"] is True, "撤销按钮未被渲染/无法查询"
        assert got["fetch"] == [
            {"url": "/api/jobs/J1/pages/4/exemption", "method": "POST", "body": "revoke=1"}
        ], got["fetch"]
        assert got["toast"] == [["已撤销豁免", "ok"]]
        assert got["reload"] == [4]

    def test_revoke_aborts_when_user_declines(self):
        """用户点"取消" ⇒ **不发请求**（否则误触即撤销 GMP 审计豁免）。"""
        body = """
mk("ocr-exemption-zone");
__confirmQueue.push(false);
UI.renderOcrExemptionZone(
  { integrity: "incomplete", exemption: { reason: "r", created_at: "c" } }, 1);
document.getElementById("ocr-exemption-zone")
        .querySelector("[data-exempt-revoke]")
        .dispatchEvent({ type: "click", stopPropagation() {} });
await settle(); await settle(); await settle();
console.log(JSON.stringify({ fetch: __calls.fetch.length, toast: __toasts.length }));
"""
        assert _probe(body) == {"fetch": 0, "toast": 0}

    def test_revoke_surfaces_server_detail_on_failure(self):
        body = """
mk("ocr-exemption-zone");
__confirmQueue.push(true);
__fetchQueue.push({ ok: false, status: 403, json: { detail: "无权限" } });
UI.renderOcrExemptionZone(
  { integrity: "incomplete", exemption: { reason: "r", created_at: "c" } }, 1);
document.getElementById("ocr-exemption-zone")
        .querySelector("[data-exempt-revoke]")
        .dispatchEvent({ type: "click", stopPropagation() {} });
await settle(); await settle(); await settle();
console.log(JSON.stringify({ toast: __toasts.map((t) => [t.msg, t.type]), reload: __loadPageCalls }));
"""
        got = _probe(body)
        assert got["toast"] == [["撤销豁免失败: 无权限", "err"]], got["toast"]
        assert got["reload"] == [], "失败时不应重载页面"

    def test_grant_button_posts_encoded_reason(self):
        """未豁免 + integrity=incomplete ⇒ 提供"确认豁免"入口，
        原因经 `encodeURIComponent` 后以表单体提交。"""
        body = """
mk("ocr-exemption-zone");
__promptQueue.push("已对照原图核对，装订线裁切不影响判定");
UI.renderOcrExemptionZone({ integrity: "incomplete" }, 7);
const zone = document.getElementById("ocr-exemption-zone");
const btn = zone.children[0];
btn.dispatchEvent({ type: "click" });
await settle(); await settle(); await settle();
console.log(JSON.stringify({ text: btn.textContent, tag: btn.tagName,
                             fetch: __calls.fetch.map((c) => ({ url: c.url, body: c.opts.body })),
                             toast: __toasts.map((t) => [t.msg, t.type]), reload: __loadPageCalls }));
"""
        got = _probe(body)
        assert got["tag"] == "BUTTON"
        assert got["text"] == "已人工核对原图，确认豁免"
        assert got["fetch"] == [{
            "url": "/api/jobs/J1/pages/7/exemption",
            "body": "reason=" + "%E5%B7%B2%E5%AF%B9%E7%85%A7%E5%8E%9F%E5%9B%BE%E6%A0%B8%E5%AF%B9%EF%BC%8C%E8%A3%85%E8%AE%A2%E7%BA%BF%E8%A3%81%E5%88%87%E4%B8%8D%E5%BD%B1%E5%93%8D%E5%88%A4%E5%AE%9A",
        }]
        assert got["toast"] == [["已记录豁免", "ok"]]
        assert got["reload"] == [7]

    def test_grant_aborts_on_empty_reason(self):
        """空原因 ⇒ 不发请求（`if (!reason) return;`）—— 豁免必须留痕，
        无原因的豁免在 GMP 审计里等于没记录。"""
        body = """
mk("ocr-exemption-zone");
__promptQueue.push("");
UI.renderOcrExemptionZone({ integrity: "incomplete" }, 1);
document.getElementById("ocr-exemption-zone").children[0].dispatchEvent({ type: "click" });
await settle(); await settle(); await settle();
console.log(JSON.stringify({ fetch: __calls.fetch.length, toast: __toasts.length }));
"""
        assert _probe(body) == {"fetch": 0, "toast": 0}

    def test_no_zone_content_when_integrity_ok(self):
        """完整性达标页不得出现豁免入口（否则用户会去豁免一个没问题页，
        污染豁免台账）。"""
        body = """
mk("ocr-exemption-zone");
UI.renderOcrExemptionZone({ integrity: "ok" }, 1);
console.log(JSON.stringify({ children: document.getElementById("ocr-exemption-zone").children.length }));
"""
        assert _probe(body) == {"children": 0}
