"""审计面板 `static/review-audit.js` 的两档护栏（TODO 0-53 / 0-54）。

## 为什么需要

面板本身不写数据、只读三个端点，看起来"错了也炸不了"。但它有**三类静默失效**：

1. **漏接线**：`review-audit.js` 存在但模板没引 ⇒ `openAuditPanel` undefined，
   点击「审计记录」按钮 `ReferenceError`（控制台报错，界面无反应）。这类
   缺陷没有任何测试会自己红 —— 与 R63 拆分时"漏引一个模块，整块职责消失"
   同一形态。
2. **行渲染错位**：把 `archiveRow` 的页/类型/状态顺序写反 ⇒ 表格列名与内容
   对不上，而页面看起来"有数据、很正常"。这是**读错数据**，比报错更糟。
3. **空表伪装成结论**：取不到数据时静默成空表 ⇒ 复核者读成"没有变更"。
   本文件钉住"空态文案存在且三句各不相同"（每句都在解释为什么会空）。

## 两档判据

- **node 实跑**（强）：`run_js(["findings-map.js", "review-audit.js"], probe)`
  直接调三个行渲染纯函数，断言单元格内容（含 `null` → `—`、`success=0` → 失败、
  finding 类型走中文单一真值）。
- **源码面结构**（弱但必要）：模板引用与加载顺序、**不得出现 `innerHTML`**、
  三个端点 URL 各自出现。

`innerHTML` 一条是硬纪律而不是洁癖：审计内容含 OCR 原文与上游 `error` 文本，
拼字符串就是 XSS 面（见文件头注纪律 1）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from tests.js_harness import run_js

REPO = Path(__file__).resolve().parents[2]
STATIC = REPO / "static"
AUDIT_JS = STATIC / "review-audit.js"
REVIEW_HTML = REPO / "templates" / "review.html"


def _src() -> str:
    return AUDIT_JS.read_text(encoding="utf-8")


def _script_srcs(html: str) -> list[str]:
    return re.findall(r'<script[^>]*src="/static/([\w.-]+\.js)[?"]', html)


class TestWiring:
    """漏接线 = 按钮点了没反应（无任何测试会自己红）。"""

    def test_file_exists(self):
        assert AUDIT_JS.is_file(), "review-audit.js 不存在 —— 审计入口无处实现"

    def test_template_references_it(self):
        srcs = _script_srcs(REVIEW_HTML.read_text(encoding="utf-8"))
        assert "review-audit.js" in srcs, (
            "review.html 未引入 review-audit.js —— openAuditPanel 会 ReferenceError"
        )

    def test_loads_before_entry(self):
        """入口 review.js 必须最后（它编排全部模块）。"""
        srcs = _script_srcs(REVIEW_HTML.read_text(encoding="utf-8"))
        pos = {n: i for i, n in enumerate(srcs)}
        assert "review-audit.js" in pos and "review.js" in pos
        assert pos["review-audit.js"] < pos["review.js"], (
            "review-audit.js 必须排在入口之前（defer 按文档序执行）"
        )

    def test_button_calls_the_global(self):
        html = REVIEW_HTML.read_text(encoding="utf-8")
        assert "openAuditPanel(event)" in html, "头部 nav 没有审计入口按钮"
        assert "global.openAuditPanel = open" in _src(), (
            "openAuditPanel 未挂到 window —— 模板内联 onclick 会 ReferenceError"
        )

    def test_no_innerhtml_with_data(self):
        """硬纪律：只用 DOM API 渲染（XSS 面 + 躲开转义登记）。

        判据是**赋值**（`.innerHTML =` / `.innerHTML +=`）而不是词频：文件头注里
        写着"不用 innerHTML 拼数据"，按词频判会把自己那段说明判红（首版就是
        这么红的 —— 断言过宽同样是缺陷）。
        """
        src = _src()
        hits = re.findall(r"\.innerHTML\s*\+?=", src)
        assert not hits, (
            f"review-audit.js 有 {len(hits)} 处 innerHTML 赋值 —— 审计内容含 "
            f"OCR 原文/上游 error，必须用 textContent"
        )
        assert "textContent" in src, "没有 textContent 说明根本没走 DOM API 渲染"

    def test_innerhtml_detector_bites(self):
        """判别力：正则必须抓得到真赋值，且不被散文里的词命中。"""
        pat = r"\.innerHTML\s*\+?="
        assert re.findall(pat, "el.innerHTML = data;")
        assert re.findall(pat, "el.innerHTML += row;")
        assert not re.findall(pat, "// 不用 innerHTML 拼数据")

    def test_three_endpoints_are_wired(self):
        src = _src()
        for frag in ("/audit?limit=", "/llm_audit?limit=", "/archived_findings?limit="):
            assert frag in src, f"缺端点 {frag} —— 对应 tab 会永远空"


class TestRowRendering:
    """node 实跑三个行渲染函数（列顺序写反不会报错，只会读错数据）。"""

    @staticmethod
    def _run() -> dict:
        probe = """
const A = globalThis.PbcAudit;
// R116：单元格可以是 {text, title} —— 断言只关心 text，另测 title 单独取。
const texts = (row) => row.map((c) => (c && typeof c === "object") ? c.text : c);
const titles = (row) => row.map((c) => (c && typeof c === "object") ? (c.title || "") : "");
const out = {
  audit: texts(A.auditRow({ created_at: "2026-10-21 10:00:00",
                            action: "pipeline_start", detail: "uploaded test.pdf" })),
  auditTitles: titles(A.auditRow({ created_at: "2026-10-21 10:00:00",
                                   action: "pipeline_start", detail: "uploaded test.pdf" })),
  auditEmpty: texts(A.auditRow({})),
  llmOk: texts(A.llmRow({ created_at: "2026-10-21 10:01:00", stage: "page_analysis",
                    provider: "deepseek", model: "deepseek-chat",
                    prompt_version: "v3", latency_ms: 1200, total_tokens: 900,
                    success: 1 })),
  llmFail: texts(A.llmRow({ success: 0, error: "ReadTimeout" })),
  archive: texts(A.archiveRow({ archived_at: "2026-10-21 10:02:00", page: 6,
                          type: "param_out_of_spec", status: "confirmed",
                          description: "温度超标", reviewer_note: "已与车间确认" })),
  archiveNoNote: texts(A.archiveRow({ page: 2, type: "completeness", status: "pending" })),
  empty: A.EMPTY,
  tabs: A.TABS.map(function (t) { return t.key; }),
  hints: A.TABS.map(function (t) { return t.hint || ""; }),
  columns: A.TABS.map(function (t) { return t.columns.length; }),
  rowLens: [A.auditRow({}).length, A.llmRow({}).length, A.archiveRow({}).length],
  limit: A.LIMIT,
  openType: typeof globalThis.openAuditPanel,
};
console.log(JSON.stringify(out));
"""
        return run_js(["findings-map.js", "review-audit.js"], probe)

    def test_audit_row_shape(self):
        got = self._run()["audit"]
        # R116：动作与详情都已人类化（`pipeline_start` → 「开始处理」）
        assert got == ["2026-10-21 10:00:00", "开始处理", "uploaded test.pdf"]

    def test_raw_machine_text_is_kept_in_title(self):
        """R116 的**信息不丢失**约束：机器码与原始 detail 必须在 title 里。

        人类化只是默认视图 —— 审计员按 GMP 台账核对时要能看到原文，
        否则"好看"就变成了"查不到"。
        """
        t = self._run()["auditTitles"]
        assert t[1] == "pipeline_start", f"机器码丢了：{t}"
        assert t[2] == "uploaded test.pdf", f"原始 detail 丢了：{t}"

    def test_missing_fields_become_dash_not_undefined(self):
        """`undefined` 会渲染成字符串 "undefined"，比空白更糟（看着像真值）。"""
        out = self._run()
        assert out["auditEmpty"] == ["—", "—", "—"]
        assert "undefined" not in json.dumps(out, ensure_ascii=False)

    def test_llm_success_and_failure(self):
        out = self._run()
        # R116：`1200 ms` → `1 秒`（复核员读的是"多久"，不是毫秒数）
        assert out["llmOk"] == [
            "2026-10-21 10:01:00", "page_analysis", "deepseek / deepseek-chat",
            "v3", "1 秒", "900", "成功",
        ], out["llmOk"]
        assert out["llmFail"][-1] == "失败：ReadTimeout", out["llmFail"]

    def test_every_tab_explains_why_it_exists(self):
        """R116：三个 tab 都必须有一句"我该在这里找什么"。

        用户打开审计面板时缺的不是数据，是**这张表为什么存在**。
        """
        hints = self._run()["hints"]
        assert len(hints) == 3
        for i, h in enumerate(hints):
            assert len(h) >= 15, f"第 {i} 个 tab 的说明太短：{h!r}"

    def test_archive_row_uses_chinese_single_source(self):
        """类型/状态必须走 findings-map.js 的单一真值（不是本地另抄一份）。"""
        got = self._run()["archive"]
        assert got[1] == "第 6 页"
        assert got[2] == "参数超标", f"type 未走中文单一真值：{got[2]}"
        assert got[3] == "已确认", f"status 未走中文单一真值：{got[3]}"
        assert got[4] == "温度超标" and got[5] == "已与车间确认"

    def test_archive_row_without_note(self):
        got = self._run()["archiveNoNote"]
        assert got[5] == "—", "没有复核意见时应显示 —，不能是空字符串"

    def test_column_count_matches_row_length(self):
        """表头列数 == 行长度 —— 错位会让内容整体右移一列（且页面看起来正常）。"""
        out = self._run()
        assert out["columns"] == out["rowLens"], (
            f"表头 {out['columns']} 列 vs 行 {out['rowLens']} 个单元格"
        )
        assert out["tabs"] == ["audit", "llm", "archived"]

    def test_empty_copies_are_distinct_and_explain_why(self):
        out = self._run()
        empty = out["empty"]
        assert set(empty) == {"audit", "llm", "archived"}
        assert len(set(empty.values())) == 3, "三句空态文案不该相同"
        for k, text in empty.items():
            assert len(text) >= 10, f"{k} 的空态文案太短，没有解释为什么会空"
        assert "重新分析" in empty["archived"], (
            "「已归档」的空态必须说明**什么动作**才会产生归档，否则用户以为坏了"
        )

    def test_limit_is_declared(self):
        assert self._run()["limit"] == 200


# ===========================================================================
# R116：审计详情的人类化（用户 2026-10-26「前端显示太过工程化」）
# ===========================================================================
#
# 下面每条 `detail` 都是**真实库里的原样字符串**（`%APPDATA%/PBC/data.db`
# 的 audit_log 表，job e50e9a7e-30c 等）。用真样本而不是编造样例，是因为
# 人类化最容易犯的错是"只认得自己编的那一种格式"。
class TestDetailHumanised:
    REAL = {
        "pipeline_complete": "status=review findings=204 failed=0 total=1054199ms",
        "stage1_complete": "pages=51 duration=57091ms backend=cached",
        "stage2_complete": "duration=775027ms failed=[]",
        "stage1_skipped": "reuse 51 cached pages (no re-OCR)",
        "ocr_input_normalized": "pages=[1, 2, 3, 4, 5]",
        "findings_overlap_suppressed": "count=1 (其中前提不可判 unfounded_self_review=0) — llm findings whose (page,type) already covered",
        "llm_guard_suppressed": "count=21 — B1-6 收权：跨页 LLM 结论被规则层确定性复核否定（判定不成立）",
        "spec_guard_dropped": "page=39 suppressed=2: param_out_of_spec: 步骤4液碱用量7800L超出规格范围800~1000L。; param_out_of_spec: 步骤5",
        "feishu_notify": "status=review ok=False token code=10003 msg=invalid param",
        "stuck_recovery": "recovered on startup: analyzing → error",
        "status_transition": "analyzing → review: 流水线完成：204 条问题，0 页失败",
    }

    @staticmethod
    def _run() -> dict:
        probe = """
const A = globalThis.PbcAudit;
const R = %s;
const out = {};
for (const k in R) out[k] = A.detailZh(k, R[k]);
out.fmt = [A.fmtMs(500), A.fmtMs(57091), A.fmtMs(775027), A.fmtMs(4200000),
           A.fmtMs(null), A.fmtMs(-1)];
out.labels = [A.actionLabel("pipeline_complete"), A.actionLabel("brand_new_action"),
              A.actionLabel(null)];
console.log(JSON.stringify(out));
""" % json.dumps(TestDetailHumanised.REAL, ensure_ascii=False)
        return run_js(["findings-map.js", "review-audit.js"], probe)

    def test_no_raw_engineering_tokens_leak(self):
        """**核心判据**：人类化之后不得再出现 `duration=` / `ms` / `count=` 这类机器串。

        这是用户投诉的直接形态 —— 表格里全是 `key=value`。
        """
        out = self._run()
        bad = []
        for k, text in out.items():
            if k in ("fmt", "labels"):
                continue
            for token in ("duration=", "findings=", "failed=", "count=", "ms"):
                if token in text:
                    bad.append((k, token, text))
        assert not bad, f"仍有机器串漏出：{bad}"

    def test_durations_are_readable(self):
        out = self._run()
        assert "17 分 34 秒" in out["pipeline_complete"], out["pipeline_complete"]
        assert "57 秒" in out["stage1_complete"], out["stage1_complete"]
        assert "12 分 55 秒" in out["stage2_complete"], out["stage2_complete"]

    def test_counts_survive(self):
        """人类化**不得丢数字** —— 204 条问题、21 条被否定都是审计事实。"""
        out = self._run()
        assert "204" in out["pipeline_complete"]
        assert "21" in out["llm_guard_suppressed"]
        assert "第 39 页" in out["spec_guard_dropped"]

    def test_chinese_detail_passes_through(self):
        """`status_transition` 的 detail 本来就是中文句子 —— 不得被改写。"""
        out = self._run()
        assert out["status_transition"] == self.REAL["status_transition"]

    def test_unknown_action_falls_back_to_raw(self):
        """未知 action ⇒ 原样显示 detail（fail-open，别把新记录显示成空白）。"""
        probe = """
const A = globalThis.PbcAudit;
console.log(JSON.stringify({ t: A.detailZh("some_future_action", "a=1 b=2"),
                             l: A.actionLabel("some_future_action") }));
"""
        got = run_js(["findings-map.js", "review-audit.js"], probe)
        assert got["t"] == "a=1 b=2"
        assert got["l"] == "some_future_action", "未知 action 至少要把机器码显示出来"

    def test_fmt_ms_edges(self):
        """边界：<1s 说「<1 秒」、负数/null 说「—」，不能出现 `NaN` / `-1 秒`。"""
        assert self._run()["fmt"] == ["<1 秒", "57 秒", "12 分 55 秒", "1 小时 10 分",
                                      "—", "—"]
