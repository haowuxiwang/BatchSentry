"""跨页问题总览 —— 后端已就绪、前端此前无入口（对抗审查 P1）。

**要防的失效模式**

`GET /api/jobs/{id}/findings` 支持**不带 `page`** 的全量聚合 + `order=confidence`
置信度排序 + `limit`/`offset` 分页；而复核页两处 fetch **恒带 `page=`**，且
**从不传 `offset`** ⇒ 复核者只能逐页翻（300 页批次里找 1 条 critical 要翻 300 次），
单页超过 `limit` 的条目则**完全不可达**。

本文件钉四件事：

1. **请求形状**：总览必须走**不带 `page`** 的 URL，且**带 `offset`**
   —— 只把 `limit` 抬高只是挪天花板，`offset` 续读才是彻底解法。
2. **续读是追加而非替换**：`加载更多` 若 `innerHTML = ...` 覆盖，会把已读条目冲掉。
3. **诚实性**：数字取自后端 `total`（不是"已读条数"）；`has_more` 为假时
   `加载更多` 必须**隐藏**（否则是点了没反应的假承诺）；**加载失败必须可见**
   （静默失败会让复核者以为"全批就这些问题" —— GMP 假阴性）；
   空结果要有明确文案而不是空白。
4. **隔离**：展开总览时隐藏页内清单与 `#tier-bar` —— 图例文案与 `title` 都写着
   "本页"，与全批数字并排显示会自相矛盾。

⚠️ 每条断言都配防空转用例：`overviewRow` 必须真的能产出含描述文本的行
（若映射/接线断了，所有"不含某字符串"的断言都会**假绿**）。
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.js_harness import run_js_async

REPO = Path(__file__).resolve().parents[2]
FINDINGS_JS = REPO / "static" / "review-findings.js"
REVIEW_JS = REPO / "static" / "review.js"
REVIEW_HTML = REPO / "templates" / "review.html"

FILES = ["findings-map.js", "review-state.js", "review-findings.js"]

# 服务端注入上下文 + 可编程 fetch 队列
_STUB = r"""
globalThis.__PBC__ = { job_id: "J1", page: 1, total_pages: 3 };
globalThis.__fetchQueue = [];
globalThis.fetch = async (url, opts) => {
  __calls.fetch.push({ url: String(url), opts: opts });
  const spec = __fetchQueue.length
    ? __fetchQueue.shift()
    : { ok: true, status: 200, json: {} };
  return { ok: spec.ok, status: spec.status, json: async () => (spec.json || {}) };
};
"""

_PRE = r"""
const R = window.PbcReview;
const OV = R.findings;
globalThis.__calls = { fetch: [] };

// 防空转前提：所有元素必须真的注册得到，否则 setOverviewOpen / syncOverviewChrome
// 会静默什么都不做，断言随之变成空断言。
const IDS = ["all-findings-panel", "all-findings-list", "all-findings-summary",
             "all-findings-more", "all-findings-toggle", "findings-list", "tier-bar"];
const ELS = {};
for (const id of IDS) ELS[id] = __el(id);
const el = (id) => ELS[id];

const settle = () => new Promise((r) => setImmediate(r));
const shown = (id) => !el(id).classList.contains("hidden");
const listHtml = () => el("all-findings-list").innerHTML;
const urls = () => __calls.fetch.map((c) => c.url);

const mkFinding = (i, page) => ({
  id: i,
  page: page,
  type: "参数越界",
  severity: "critical",
  source: "rule",
  description: "批量投料记录 " + i,
  status: "pending",
  tier: "rule",
});

// 排队一个总览响应
const queue = (findings, total) => {
  __fetchQueue.push({
    ok: true,
    status: 200,
    json: { findings: findings, total: total, count: findings.length },
  });
};

// 打开总览并等一次微任务排空（setOverviewOpen 不 await loadOverviewPage）
const openOverview = async () => {
  OV.setOverviewOpen(true);
  await settle();
};
"""


def _probe(body: str):
    return run_js_async(FILES, _PRE + "\n" + body, stub=_STUB)


# ─────────────────────────────────────────────────────────────────────
# 防空转
# ─────────────────────────────────────────────────────────────────────
class TestHarnessIsNotVacuous:
    def test_overview_row_renders_the_description(self):
        """`overviewRow` 必须真的产出含描述的 HTML（否则"不含"类断言全假绿）。"""
        out = _probe(
            """
const html = OV.overviewRow(mkFinding(3, 7));
console.log(JSON.stringify({
  hasDesc: html.includes("批量投料记录 3"),
  hasPageBtn: html.includes("goPage(7)"),
  length: html.length,
}));
"""
        )
        assert out["hasDesc"] is True, "overviewRow 未产出描述 —— 映射或拼接断了"
        assert out["hasPageBtn"] is True, "overviewRow 未产出跳页入口"
        assert out["length"] > 0

    def test_all_dom_elements_are_reachable(self):
        out = _probe(
            """
console.log(JSON.stringify({ missing: IDS.filter((id) => !el(id)) }));
"""
        )
        assert out["missing"] == [], f"元素查不到：{out['missing']} —— 断言会变空断言"

    def test_the_overview_fetch_source_is_unique(self):
        """源码提取器必须命中**恰好一处**总览 fetch（零匹配会让形状断言空转）。"""
        src = FINDINGS_JS.read_text(encoding="utf-8")
        sites = re.findall(r"/api/jobs/\$\{jobId\}/findings\?order=confidence", src)
        assert len(sites) == 1, f"期望 1 处总览 fetch，实得 {len(sites)}"


# ─────────────────────────────────────────────────────────────────────
# 1. 请求形状：不带 page、必须带 offset
# ─────────────────────────────────────────────────────────────────────
class TestOverviewRequestShape:
    def test_fetch_is_job_wide_and_offset_paged(self):
        out = _probe(
            """
queue([mkFinding(1, 1)], 1);
await openOverview();
const u = urls()[0];
console.log(JSON.stringify({
  url: u,
  hasPage: /[?&]page=/.test(u),
  hasOrder: u.includes("order=confidence"),
  hasLimit: /[?&]limit=\\d+/.test(u),
  hasOffset: /[?&]offset=\\d+/.test(u),
}));
"""
        )
        assert out["hasPage"] is False, "总览 fetch 带了 page= —— 那就退回页内语义了"
        assert out["hasOrder"] is True
        assert out["hasLimit"] is True, "缺 limit —— 后端默认 50，天花板过低"
        assert out["hasOffset"] is True, (
            "缺 offset —— 只抬高 limit 是挪天花板；offset 续读才能取到全部条目"
        )

    def test_first_request_starts_at_offset_zero(self):
        out = _probe(
            """
queue([mkFinding(1, 1)], 1);
await openOverview();
console.log(JSON.stringify({ url: urls()[0] }));
"""
        )
        assert "offset=0" in out["url"]

    def test_load_more_advances_the_offset_by_what_was_read(self):
        """续读必须用**已读条数**推进 offset，不能恒为 0（否则无限拉同一页）。"""
        out = _probe(
            """
queue([mkFinding(1, 1), mkFinding(2, 2)], 4);
await openOverview();
queue([mkFinding(3, 3), mkFinding(4, 4)], 4);
await OV.loadOverviewPage();
console.log(JSON.stringify({ urls: urls() }));
"""
        )
        assert len(out["urls"]) == 2
        assert "offset=0" in out["urls"][0]
        assert "offset=2" in out["urls"][1], (
            f"续读 offset 未按已读条数推进：{out['urls'][1]}"
        )

    def test_source_never_passes_a_page_param(self):
        """源码级守卫：总览 URL 模板里不得出现 page=（新增调用点漏改即变红）。"""
        src = FINDINGS_JS.read_text(encoding="utf-8")
        block = re.search(
            r"const url =\s*(.*?);", src, re.S
        )
        assert block, "未找到总览 URL 构造 —— 提取器或代码变了"
        assert "page=" not in block.group(1), (
            "总览 URL 模板含 page= —— 会退回页内语义"
        )


# ─────────────────────────────────────────────────────────────────────
# 2. 续读是追加，不是替换
# ─────────────────────────────────────────────────────────────────────
class TestLoadMoreAppends:
    def test_second_page_is_appended_not_replaced(self):
        out = _probe(
            """
queue([mkFinding(1, 1), mkFinding(2, 2)], 4);
await openOverview();
const afterFirst = listHtml();
queue([mkFinding(3, 3), mkFinding(4, 4)], 4);
await OV.loadOverviewPage();
console.log(JSON.stringify({
  afterFirst,
  afterSecond: listHtml(),
  hasFirst: listHtml().includes("批量投料记录 1"),
  hasSecond: listHtml().includes("批量投料记录 3"),
}));
"""
        )
        assert out["hasFirst"] is True, (
            "续读把已读条目冲掉了（innerHTML 覆盖而非追加）—— 复核者会丢失前面的问题"
        )
        assert out["hasSecond"] is True
        assert len(out["afterSecond"]) > len(out["afterFirst"])


# ─────────────────────────────────────────────────────────────────────
# 3. 诚实性：数字 / 加载更多 / 失败可见 / 空态
# ─────────────────────────────────────────────────────────────────────
class TestOverviewIsHonest:
    def test_summary_uses_the_backend_total_not_the_read_count(self):
        """总数必须取自后端 `total`：只报"已读条数"会让复核者以为全批就这么多。"""
        out = _probe(
            """
queue([mkFinding(1, 1), mkFinding(2, 2)], 57);
await openOverview();
console.log(JSON.stringify({ summary: el("all-findings-summary").textContent }));
"""
        )
        s = out["summary"]
        assert "57" in s, f"摘要未报后端总数 57：{s}"
        assert "2" in s, f"摘要未报已显示条数：{s}"

    def test_load_more_is_hidden_when_nothing_is_left(self):
        out = _probe(
            """
queue([mkFinding(1, 1), mkFinding(2, 2)], 2);
await openOverview();
console.log(JSON.stringify({ moreShown: shown("all-findings-more") }));
"""
        )
        assert out["moreShown"] is False, (
            "已无更多条目却仍显示「加载更多」—— 点了没反应，是假承诺"
        )

    def test_load_more_is_shown_when_more_remain(self):
        out = _probe(
            """
queue([mkFinding(1, 1)], 40);
await openOverview();
console.log(JSON.stringify({ moreShown: shown("all-findings-more") }));
"""
        )
        assert out["moreShown"] is True, "还有 39 条未读却不给续读入口"

    def test_failure_is_visible_never_silent(self):
        """加载失败必须报出来。静默失败 = 复核者以为"全批就这些问题"（GMP 假阴性）。"""
        out = _probe(
            """
__fetchQueue.push({ ok: false, status: 500, json: {} });
await openOverview();
console.log(JSON.stringify({ html: listHtml(), blank: listHtml() === "" }));
"""
        )
        assert out["blank"] is False, "加载失败后清单为空 —— 静默失败（最危险形态）"
        assert "失败" in out["html"], f"失败未在界面上报出：{out['html']}"

    def test_empty_job_says_so_explicitly(self):
        out = _probe(
            """
queue([], 0);
await openOverview();
console.log(JSON.stringify({ html: listHtml() }));
"""
        )
        assert out["html"].strip() != "", "空结果显示空白 —— 与「加载失败」无法区分"
        assert "没有问题" in out["html"], f"空结果缺明确文案：{out['html']}"


# ─────────────────────────────────────────────────────────────────────
# 4. 行内容：跳页入口 + 转义
# ─────────────────────────────────────────────────────────────────────
class TestOverviewRowContent:
    def test_page_badge_targets_that_page(self):
        out = _probe(
            """
const html = OV.overviewRow(mkFinding(1, 7));
console.log(JSON.stringify({ html }));
"""
        )
        assert "goPage(7)" in out["html"], "跳页入口未指向该条所在页"
        assert "第 7 页" in out["html"]

    def test_description_is_escaped(self):
        """description 来自 LLM/规则产出，必须转义 —— 总览是新增注入面。"""
        out = _probe(
            """
const f = mkFinding(1, 1);
f.description = '<img src=x onerror=alert(1)>.pdf';
console.log(JSON.stringify({ html: OV.overviewRow(f) }));
"""
        )
        html = out["html"]
        assert "<img" not in html, "description 未转义 —— 总览引入了 XSS 面"
        # ⚠️ 不要断言 `onerror=alert` 不存在：转义只处理尖括号，该**字面量**当然
        # 还在，一旦尖括号被转义它就不再是属性 —— 那样断言是无效信号（假失败）。
        # 正确的信号是"载荷的尖括号变成了实体"。
        assert "&lt;img src=x onerror=alert(1)&gt;" in html, (
            "载荷的尖括号未转义成实体 —— 转义可能只是把整段删掉了，或漏了 >"
        )

    def test_type_and_status_also_escaped(self):
        out = _probe(
            """
const f = mkFinding(1, 1);
f.type = '<script>bad</script>';
f.source = '<b>x</b>';
console.log(JSON.stringify({ html: OV.overviewRow(f) }));
"""
        )
        html = out["html"]
        assert "<script>" not in html, "type 未转义"
        assert "<b>" not in html, "source 未转义"

    def test_no_page_button_when_page_is_unknown(self):
        """页码缺失/为 0 时不渲染跳页入口 —— 宁可没有，也不要跳到第 0 页。"""
        out = _probe(
            """
const f = mkFinding(1, 0);
console.log(JSON.stringify({
  zero: OV.overviewRow(f).includes("goPage("),
  missing: OV.overviewRow({ id: 2, description: "x" }).includes("goPage("),
}));
"""
        )
        assert out["zero"] is False, "page=0 仍渲染了跳页入口"
        assert out["missing"] is False, "page 缺失仍渲染了跳页入口"


# ─────────────────────────────────────────────────────────────────────
# 5. 隔离：总览与页内清单/tier 图例互斥
# ─────────────────────────────────────────────────────────────────────
class TestOverviewIsolatesPageScopedChrome:
    def test_opening_hides_the_page_scoped_chrome(self):
        """`#tier-bar` 的文案与 title 都写着"本页" ⇒ 与全批数字并排会自相矛盾。"""
        out = _probe(
            """
queue([mkFinding(1, 1)], 1);
await openOverview();
console.log(JSON.stringify({
  panel: shown("all-findings-panel"),
  pageList: shown("findings-list"),
  tierBar: shown("tier-bar"),
  expanded: el("all-findings-toggle").getAttribute("aria-expanded"),
  label: el("all-findings-toggle").textContent,
}));
"""
        )
        assert out["panel"] is True, "展开后总览面板仍隐藏"
        assert out["pageList"] is False, "展开后页内清单未隐藏 —— 两套清单同时可见"
        assert out["tierBar"] is False, "展开后 tier 图例未隐藏（其文案是本页口径）"
        assert out["expanded"] == "true", "aria-expanded 未同步（无障碍）"
        assert "返回" in out["label"], f"切换按钮文案未反映当前状态：{out['label']}"

    def test_closing_restores_the_page_scoped_chrome(self):
        out = _probe(
            """
queue([mkFinding(1, 1)], 1);
await openOverview();
OV.setOverviewOpen(false);
console.log(JSON.stringify({
  panel: shown("all-findings-panel"),
  pageList: shown("findings-list"),
  tierBar: shown("tier-bar"),
  expanded: el("all-findings-toggle").getAttribute("aria-expanded"),
}));
"""
        )
        assert out["panel"] is False
        assert out["pageList"] is True, "收起后页内清单未恢复"
        assert out["tierBar"] is True, "收起后 tier 图例未恢复"
        assert out["expanded"] == "false"

    def test_reopening_does_not_refetch(self):
        """已加载过就不该重拉（否则每次开关都打一次全量查询）。"""
        out = _probe(
            """
queue([mkFinding(1, 1), mkFinding(2, 2)], 2);
await openOverview();
OV.setOverviewOpen(false);
await openOverview();
console.log(JSON.stringify({ n: urls().length }));
"""
        )
        assert out["n"] == 1, f"重新展开又拉了一次（共 {out['n']} 次）"


# ─────────────────────────────────────────────────────────────────────
# 6. 接线：模板容器 + 初始化挂载
# ─────────────────────────────────────────────────────────────────────
class TestOverviewIsWired:
    def test_template_has_the_containers(self):
        html = REVIEW_HTML.read_text(encoding="utf-8")
        for anchor in (
            'id="all-findings-panel"',
            'id="all-findings-list"',
            'id="all-findings-summary"',
            'id="all-findings-more"',
            'id="all-findings-toggle"',
        ):
            assert anchor in html, f"模板缺 {anchor}"

    def test_init_is_called_from_the_page_bootstrap(self):
        """总览按钮的监听必须在 DOMContentLoaded 里挂上，否则点了没反应。"""
        src = REVIEW_JS.read_text(encoding="utf-8")
        assert "R.findings.initOverview()" in src, (
            "review.js 未调用 initOverview —— 总览按钮不会被绑定"
        )
        # 防空转：确认那确实在 DOMContentLoaded 块内（锚调用点，不锚裸标识符）
        dom = src.index('addEventListener("DOMContentLoaded"')
        call = src.index("R.findings.initOverview()")
        assert call > dom, "initOverview 调用点不在 DOMContentLoaded 之后"
