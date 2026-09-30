"""findings 截断：单页 >limit 条时**不可达**的条目 + 误导性提示文案。

**要防的失效模式**（对抗审查实测确认，非推测）：

`GET /api/jobs/{id}/findings` 在 `order=confidence` 分支先把该页全部
findings（≤2000 行）取回、按置信度排序，再 `findings[offset:offset+limit]`
切片。而复核页的两处 fetch **都不传 offset** ⇒ `limit` 就是本页可见条数的
**硬上限**，默认 50。

于是"单页 >50 条"时，第 51 条起在 UI 里**完全不可达**：

  · 翻页离开再回来 → 仍是同一批前 50 条（findings 是页内集合）；
  · 裁决几条再刷新   → 已裁决的条目只在渲染时变淡（`opacity-50`），
                       **不会移出列表**，被截断的条目照样进不来。

仓库自带的后端测试已经实测过这个量级：
`tests/integration/test_api_review.py::TestFindingsList::
test_has_more_scoped_to_current_filter` —— page=1 造 57 条，断言
`count == 50` / `total == 57` / `has_more is True`。前端此前把这个
`has_more` 渲染成「请逐页翻页或处理后刷新」，**两句都是无效指引**。

本文件钉三件事，缺一即洞重开：

1. **请求上限**：两处 findings fetch 都必须显式带 `limit=`，且值取自
   单一常量 `FINDINGS_PAGE_LIMIT`（= 后端上限 200）。新增第三处 fetch
   若忘了带 limit，`order=confidence(?!&limit=)` 的零匹配断言会变红。
2. **提示文案报真实数字**：`total` 传入后必须显示"共 N 条 / 尚有 M 条未显示"。
3. **提示文案不再指错路**：不得出现"逐页翻页"这类**做不到**的建议，
   且必须指向真含全部 findings 的出口（报告导出）。

⚠️ 每条断言都配了防空转用例：`renderFindings` 必须真的能往 `#findings-list`
里写出内容（若 DOM 接线断了，所有"不含某字符串"的断言都会**假绿**）。
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.js_harness import run_js_async

REPO = Path(__file__).resolve().parents[2]
REVIEW_JS = REPO / "static" / "review.js"
FINDINGS_JS = REPO / "static" / "review-findings.js"

FILES = ["findings-map.js", "review-state.js", "review-findings.js"]

# 服务端注入上下文（review-state.js 读 window.__PBC__）
_STUB = r"""
globalThis.__PBC__ = { job_id: "J1", page: 1, total_pages: 3 };
"""

_PRE = r"""
const R = window.PbcReview;
globalThis.__calls = { fetch: [] };

// 防空转前提：renderFindings 只有拿到 #findings-list 才会写任何东西。
const list = __el("findings-list");

const mkFinding = (i) => ({
  id: i,
  page: 1,
  type: "参数越界",
  severity: "warning",
  source: "rule",
  description: "批量投料记录 " + i,
  status: "pending",
  tier: "rule",
  confidence: 0.9,
});

const render = (n, hasMore, total) => {
  const fs = [];
  for (let i = 1; i <= n; i += 1) fs.push(mkFinding(i));
  R.findings.renderFindings(fs, hasMore, total);
  return list.innerHTML;
};
"""


def _probe(body: str):
    return run_js_async(FILES, _PRE + "\n" + body, stub=_STUB)


# ─────────────────────────────────────────────────────────────────────
# 防空转：证明提取器与 DOM 接线真的在工作
# ─────────────────────────────────────────────────────────────────────
class TestHarnessIsNotVacuous:
    def test_render_actually_writes_rows_into_the_list(self):
        """1 条 finding → 清单里必须出现它的描述文本。

        若 DOM 接线断了（`getElementById` 返回 null），`renderFindings` 会
        静默 return，此后所有"不含 XX 字样"的断言都会**假绿**。
        """
        out = _probe(
            """
const html = render(1, false);
console.log(JSON.stringify({
  hasList: !!list,
  containsDesc: html.includes("批量投料记录 1"),
  length: html.length,
}));
"""
        )
        assert out["hasList"] is True, "#findings-list 未被 harness 注册"
        assert out["containsDesc"] is True, "renderFindings 未写出 finding 描述 —— 接线断了"
        assert out["length"] > 0

    def test_the_source_extractor_finds_both_fetch_call_sites(self):
        """源码提取器必须真的命中 2 处 findings fetch（否则下面的零匹配断言空转）。"""
        src = REVIEW_JS.read_text(encoding="utf-8")
        sites = re.findall(r"findings\?page=\$\{[A-Za-z]+\}&order=confidence", src)
        assert len(sites) == 2, f"期望 2 处 findings fetch，实得 {len(sites)} —— 提取器或代码变了"

    def test_the_limit_constant_is_defined_once(self):
        """单一真值：常量必须存在且等于后端上限 200。"""
        src = REVIEW_JS.read_text(encoding="utf-8")
        m = re.search(r"const FINDINGS_PAGE_LIMIT\s*=\s*(\d+)\s*;", src)
        assert m, "review.js 未定义 FINDINGS_PAGE_LIMIT 常量"
        assert int(m.group(1)) == 200, (
            "FINDINGS_PAGE_LIMIT 必须等于 api/review.py 的 min(limit, 200) 上限"
        )


# ─────────────────────────────────────────────────────────────────────
# 0. 调用点必须把 total 传下去（否则提示静默降级成"不报数字"）
# ─────────────────────────────────────────────────────────────────────
class TestRenderCallSitesPassTheTotal:
    """`total` 是可选第三参 ⇒ 漏传**不会报错**，只会让截断提示退化成
    "仍有多条未显示"（无数字）。那种退化是静默的：页面上一切正常，
    只是复核者永远不知道还差几条。故必须机检调用点。
    """

    def test_both_render_call_sites_pass_findings_total(self):
        src = REVIEW_JS.read_text(encoding="utf-8")
        calls = re.findall(r"R\.findings\.renderFindings\((.*?)\);", src, re.S)
        # 防空转：提取器必须真的命中 2 处调用点
        assert len(calls) == 2, (
            f"期望 2 处 renderFindings 调用点，实得 {len(calls)} —— 提取器或代码变了"
        )
        for i, c in enumerate(calls, 1):
            assert "findingsData.total" in c, (
                f"第 {i} 处 renderFindings 未传 findingsData.total —— "
                "截断提示会退化成不报数字（静默）"
            )


# ─────────────────────────────────────────────────────────────────────
# 1. 请求上限：不留"没带 limit 的第三处 fetch"
# ─────────────────────────────────────────────────────────────────────
class TestFetchCarriesTheRaisedLimit:
    def test_both_call_sites_pass_the_shared_limit(self):
        src = REVIEW_JS.read_text(encoding="utf-8")
        full = re.findall(
            r"findings\?page=\$\{[A-Za-z]+\}&order=confidence&limit=\$\{FINDINGS_PAGE_LIMIT\}",
            src,
        )
        assert len(full) == 2, (
            "两处 findings fetch 都必须带 limit=${FINDINGS_PAGE_LIMIT}；"
            f"实得 {len(full)} 处 —— 不传 limit 时后端默认 50，单页 >50 条不可达"
        )

    def test_no_findings_fetch_forgets_the_limit(self):
        """findings 翻页 fetch 的 `order=confidence` 之后**必须紧跟** `&limit=`。

        ⚠️ 锚点必须含 `findings?page=`，不能只查裸串 `order=confidence` ——
        函数内注释也会出现这个标识符，裸串匹配会把注释当违规（假失败），
        反过来也可能把真违规锚错位置。
        """
        src = REVIEW_JS.read_text(encoding="utf-8")
        pattern = r"findings\?page=\$\{[A-Za-z]+\}&order=confidence(?!&limit=)"
        offenders = re.findall(pattern, src)
        assert offenders == [], (
            f"有 {len(offenders)} 处 findings 翻页 fetch 未紧跟 &limit= —— "
            "该处会退回后端默认 50，单页第 51 条起在 UI 里不可达"
        )


# ─────────────────────────────────────────────────────────────────────
# 2/3. 提示文案：报真实数字，且不指错路
# ─────────────────────────────────────────────────────────────────────
class TestTruncationHintIsHonest:
    def test_hint_reports_the_real_total_and_the_real_gap(self):
        out = _probe(
            """
const html = render(50, true, 57);
console.log(JSON.stringify({ html }));
"""
        )
        html = out["html"]
        assert "57" in html, "截断提示未报出本页总数 57"
        assert "50" in html, "截断提示未报出已显示条数 50"
        assert "7" in html, "截断提示未报出缺口 7（57 - 50）"
        assert "未显示" in html

    def test_hint_does_not_advise_actions_that_cannot_work(self):
        """旧文案「请逐页翻页或处理后刷新」两句都做不到，必须消失。"""
        out = _probe(
            """
const html = render(50, true, 57);
console.log(JSON.stringify({ html }));
"""
        )
        html = out["html"]
        assert "逐页翻页" not in html, (
            "提示仍在建议「逐页翻页」—— findings 是页内集合，翻页回来仍是同一批前 50 条"
        )
        assert "处理后刷新" not in html, (
            "提示仍在建议「处理后刷新」—— 已裁决条目只是变淡、不会移出列表，换不出被截断的条目"
        )

    def test_hint_points_at_the_report_export_which_really_has_everything(self):
        """指路必须指向真能到的地方：报告导出的查询无 LIMIT（api/report.py）。"""
        out = _probe(
            """
const html = render(50, true, 57);
console.log(JSON.stringify({ html }));
"""
        )
        assert "报告导出" in out["html"], (
            "截断提示未指向报告导出 —— 那是唯一含**全部** findings 的出口"
        )

    def test_no_hint_when_nothing_was_truncated(self):
        out = _probe(
            """
const html = render(3, false, 3);
console.log(JSON.stringify({ html }));
"""
        )
        assert "未显示" not in out["html"], "未截断时不该出现截断提示"

    def test_hint_degrades_without_a_total_instead_of_lying(self):
        """旧调用点只传 2 个参数 → 不报数字，但**绝不**退回无效指引。"""
        out = _probe(
            """
const html = render(50, true);
console.log(JSON.stringify({ html }));
"""
        )
        html = out["html"]
        assert "未显示" in html, "缺 total 时仍应提示「仍有多条未显示」"
        assert "逐页翻页" not in html
        assert "处理后刷新" not in html

    def test_no_bogus_number_when_total_equals_shown(self):
        """hasMore=true 但 total 未超过已显示条数（边界）→ 不得报出 0 或负数缺口。"""
        out = _probe(
            """
const html = render(50, true, 50);
console.log(JSON.stringify({ html }));
"""
        )
        html = out["html"]
        assert "尚有 0 条" not in html, "报出了 0 条缺口 —— 边界判据写错"
        assert "尚有 -" not in html, "报出了负数缺口"
