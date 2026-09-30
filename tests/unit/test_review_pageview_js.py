"""review-pageview.js 的行为护栏（此前只有**源码字符串**断言，零行为覆盖）。

**为什么必须测**：本模块承担翻页时"左侧 PDF 面板"的全部一致性 —— 图片、
页码、计数器、导航箭头、页码圆点、区域证据框。它此前由
`test_region_anchor_wiring.py` 以**源码扫描**方式覆盖（`assert "REGION_ASPECT_TOL"
in js` 之类），能证明"代码里写了"，不能证明"真的会这么走"。

已发生过一次真实事故，正是源码扫描抓不到的类型：
`updatePdfDisplay` 里的 `syncNavButtons()` 曾被放在函数**末尾**，而
"图片已缓存同一页"分支会提前 `return` —— 于是从末页跳回任意页后，
`next` 箭头**永久禁用**。源码里 `syncNavButtons()` 明明存在。

本文件用假 DOM 真实调用，断言落在**可观测决策**上（哪个 class 被加、
style 像素值、toast、dataset 签名）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.js_harness import run_js_async

REPO = Path(__file__).resolve().parents[2]
REVIEW_HTML = REPO / "templates" / "review.html"

FILES = ["review-state.js", "review-pageview.js"]

_STUB = r"""
globalThis.__PBC__ = { job_id: "J1", page: 1, total_pages: 5 };
"""

_PRE = r"""
const R = window.PbcReview;
const PV = R.pageview;
R.state.jobId = "J1";
R.state.totalPages = 5;
R.state.currentPage = 1;

// review.js 未加载 → 桩掉跨模块协作者，并记录调用（顺序/次数即断言对象）。
globalThis.__goPage = [];
R.goPage = (p) => { __goPage.push(p); };
globalThis.__sync = [];
R.syncNavButtons = () => { __sync.push(R.state.currentPage); };

const mk = (id, tag) => __el(id, tag);
const imgEl = () => document.getElementById("pdf-page-img");
const ovEl = () => document.getElementById("region-overlay");
const txt = (id) => {
  const e = document.getElementById(id);
  return e ? e.textContent : null;
};

// 页码导航必须挂在 body 下 —— 模块用 document.querySelectorAll 找 .page-nav-item
const nav = () => {
  const n = mk("page-nav");
  document.body.appendChild(n);
  return n;
};
const addNavItems = (n, pages) => {
  pages.forEach((p) => {
    const a = document.createElement("a");
    a.className = "page-nav-item";
    a.dataset.page = String(p);
    n.appendChild(a);
  });
};
const item = (p) => document.querySelectorAll(".page-nav-item")[p - 1] || null;
const dots = (p) => {
  const el = item(p);
  return el ? el.querySelector("[data-dots]") : null;
};
// 服务端渲染形态的页码项（镜像 templates/review.html 的 SSR 标记：
// 容器带 data-dots="1"、子圆点带 data-dot="critical|warning"）。
// 模板侧由 TestSsrAjaxDotMarkerParity 的源码断言锁定；本 helper 与之配对，
// 共同构成"生产者 ↔ 消费者标记契约"。
const ssrItem = (page, sev) => {
  const a = document.createElement("a");
  a.className = "page-nav-item";
  a.dataset.page = String(page);
  const label = document.createElement("span");
  label.className = "tabular-nums";
  label.textContent = "第" + page + "页";
  a.appendChild(label);
  if (sev) {
    const holder = document.createElement("span");
    holder.setAttribute("data-dots", "1");
    holder.className = "flex items-center gap-1";
    if (sev.critical > 0) {
      const d = document.createElement("span");
      d.setAttribute("data-dot", "critical");
      d.className = "w-1 h-1 rounded-full bg-destructive";
      d.title = sev.critical + " 严重";
      holder.appendChild(d);
    }
    if (sev.warning > 0) {
      const d = document.createElement("span");
      d.setAttribute("data-dot", "warning");
      d.className = "w-1 h-1 rounded-full bg-warning";
      d.title = sev.warning + " 警告";
      holder.appendChild(d);
    }
    a.appendChild(holder);
  }
  document.body.appendChild(a);
  return a;
};
// 页码项下"圆点容器"的个数（按容器 class 计，与标记名无关）
const dotContainers = (p) => {
  const el = item(p);
  return el ? el.children.filter((c) => c.classList.contains("flex")).length : -1;
};
const dotsInfo = (p) => {
  const d = dots(p);
  if (!d) return null;
  return {
    n: d.children.length,
    titles: d.children.map((c) => c.title || ""),
    texts: d.children.map((c) => c.textContent || ""),
    classes: d.children.map((c) => c.className || ""),
  };
};

// 一张 600×800 的页图（宽高比 0.75，与 regionRef 的 page_aspect 一致）
const pageImg = (w, h) => {
  mk("pdf-page-img");
  const img = imgEl();
  img.src = "";
  img.naturalWidth = w;
  img.naturalHeight = h;
  img.offsetLeft = 0;
  img.offsetTop = 0;
  img.offsetWidth = w;
  img.offsetHeight = h;
  return img;
};
const regionRef = (over) => Object.assign({
  page_bbox: [0.25, 0.5, 0.75, 1.0],
  bbox: [0.25, 0.5, 0.75, 1.0],
  page_aspect: 0.75,
}, over || {});
const click = (el) => {
  let prevented = false;
  el.dispatchEvent({ type: "click", preventDefault() { prevented = true; } });
  return prevented;
};
"""


def _probe(body: str):
    return run_js_async(FILES, _PRE + "\n" + body, stub=_STUB)


# ─────────────────────────────────────────────────────────────────────
# updatePdfDisplay —— "末页回跳后 next 永久卡死"回归门
# ─────────────────────────────────────────────────────────────────────
class TestUpdatePdfDisplay:
    def test_cache_hit_still_syncs_nav_buttons(self):
        """**回归门**：图片已缓存同一页（提前 return）时，`syncNavButtons()`
        仍必须被调用。

        箭头禁用位只依赖 currentPage/totalPages，与"图片是否要重新请求"
        毫无关系。历史缺陷：`syncNavButtons()` 写在函数末尾 → 缓存命中分支
        提前 return 把它跳过 → 从末页跳回任意页后 `next` 永久禁用。
        """
        body = """
mk("pdf-page-img");
const img = imgEl();
img.src = "/api/jobs/J1/page/3";
mk("page-num"); mk("page-total"); mk("page-counter"); mk("pdf-loading");
const loading = document.getElementById("pdf-loading");
loading.classList.add("is-loaded");
const p = document.createElement("p");
p.textContent = "旧文案";
loading.appendChild(p);
R.state.currentPage = 3;
PV.updatePdfDisplay(3);
console.log(JSON.stringify({
  sync: __sync.length,
  src: img.src,
  counter: txt("page-counter"),
  stillLoaded: loading.classList.contains("is-loaded"),
  loadingText: p.textContent,
}));
"""
        assert _probe(body) == {
            "sync": 1,
            "src": "/api/jobs/J1/page/3",
            "counter": "3 / 5",
            "stillLoaded": True,
            "loadingText": "旧文案",
        }

    def test_page_change_sets_src_clears_anchor_and_resets_loading(self):
        """换页：图片 URL 更新、旧区域框清掉、loading 遮罩重新盖住。"""
        body = """
mk("pdf-page-img"); mk("page-num"); mk("page-total"); mk("page-counter");
mk("pdf-loading"); mk("region-overlay");
const img = imgEl();
img.src = "/api/jobs/J1/page/1";
const loading = document.getElementById("pdf-loading");
loading.classList.add("is-loaded");
const p = document.createElement("p");
p.textContent = "旧文案";
loading.appendChild(p);
R.state.currentPage = 1;
PV.updatePdfDisplay(4);
console.log(JSON.stringify({
  sync: __sync.length,
  src: img.src,
  pageNum: txt("page-num"),
  pageTotal: txt("page-total"),
  counter: txt("page-counter"),
  anchorHidden: ovEl().classList.contains("hidden"),
  stillLoaded: loading.classList.contains("is-loaded"),
  loadingText: p.textContent,
}));
"""
        assert _probe(body) == {
            "sync": 1,
            "src": "/api/jobs/J1/page/4",
            "pageNum": "4",
            "pageTotal": "5",
            "counter": "4 / 5",
            "anchorHidden": True,
            "stillLoaded": False,
            "loadingText": "正在渲染第 4 页 …",
        }

    def test_total_pages_zero_shows_question_mark(self):
        """OCR 未完成时 totalPages=0（真实页数未知）→ 显示 `?` 而非 0/1。"""
        body = """
mk("pdf-page-img"); mk("page-num"); mk("page-total"); mk("page-counter");
imgEl().src = "";
R.state.totalPages = 0;
PV.updatePdfDisplay(2);
console.log(JSON.stringify({ total: txt("page-total"), counter: txt("page-counter") }));
"""
        assert _probe(body) == {"total": "?", "counter": "2 / ?"}

    def test_missing_image_is_a_noop_but_still_syncs(self):
        """页图元素缺失（模板精简）时不抛错，且箭头状态照常同步。"""
        body = """
R.state.currentPage = 2;
const rv = PV.updatePdfDisplay(2);
console.log(JSON.stringify({ rv: rv === undefined, sync: __sync.length }));
"""
        assert _probe(body) == {"rv": True, "sync": 1}

    def test_missing_loading_overlay_is_tolerated(self):
        """只有图片没有 loading 遮罩：不得在 `loading.classList` 上抛错。"""
        body = """
mk("pdf-page-img");
const img = imgEl();
img.src = "";
PV.updatePdfDisplay(2);
console.log(JSON.stringify({ src: img.src }));
"""
        assert _probe(body) == {"src": "/api/jobs/J1/page/2"}


# ─────────────────────────────────────────────────────────────────────
# updatePageNavDots —— 页码圆点：签名跳过 + 内容正确
# ─────────────────────────────────────────────────────────────────────
class TestUpdatePageNavDots:
    def test_identical_counts_do_not_rebuild_dom(self):
        """SSE 每 3s 全量 tick；计数未变时必须**跳过重建**（元素同一性可证）。

        51 页 × 每 tick 删建 DOM 纯属浪费 —— 而"跳过"若被误删，功能上看
        不出任何异常，只会白烧 CPU。故用 `===` 元素同一性锁定。
        """
        body = """
const n = nav(); addNavItems(n, [1, 2, 3]);
const counts = { 1: { critical: 1, warning: 0, info: 0, total: 1 } };
PV.updatePageNavDots(counts);
const first = dots(1);
PV.updatePageNavDots(counts);
const second = dots(1);
console.log(JSON.stringify({
  same: first === second,
  hasSig: !!item(1).dataset.dotsSig,
}));
"""
        assert _probe(body) == {"same": True, "hasSig": True}

    def test_changed_counts_do_rebuild(self):
        """反向：计数变化必须重建（否则圆点永远停在首帧）。"""
        body = """
const n = nav(); addNavItems(n, [1, 2, 3]);
PV.updatePageNavDots({ 1: { critical: 1, warning: 0, info: 0, total: 1 } });
const first = dots(1);
PV.updatePageNavDots({ 1: { critical: 0, warning: 2, info: 0, total: 2 } });
const second = dots(1);
console.log(JSON.stringify({
  changed: first !== second,
  titles: dotsInfo(1).titles,
}));
"""
        assert _probe(body) == {
            "changed": True,
            "titles": ["2 警告"],
        }

    def test_total_is_part_of_the_signature(self):
        """**渲染输出依赖的每一个输入都必须进签名。**

        圆点上的数字渲染的是 `c.total`（info 分支 `n.textContent = c.total`），
        但签名只含 critical/warning/info。两者**不等价**：后端
        `_page_finding_counts` 对未知 severity 仍 `total += cnt` 而不增任何
        分档计数（severity 列无 CHECK 约束，`findings-map.js` 的
        `zhOrUnknown` 也证明"未知 severity"是既定现实）。此时 total 变了、
        签名不变 → 圆点数字**永远停在旧值**。
        """
        body = """
const n = nav(); addNavItems(n, [1, 2, 3]);
PV.updatePageNavDots({ 1: { critical: 0, warning: 0, info: 1, total: 1 } });
const before = dotsInfo(1).texts[0];
PV.updatePageNavDots({ 1: { critical: 0, warning: 0, info: 1, total: 2 } });
console.log(JSON.stringify({ before, after: dotsInfo(1).texts[0] }));
"""
        assert _probe(body) == {"before": "1", "after": "2"}

    def test_ssr_rendered_dots_are_replaced_not_duplicated(self):
        """**回归门**：SSR 已渲染圆点的页码项，首次实时刷新必须**替换**它。

        `templates/review.html` 的圆点容器原本不带标记（只有子圆点带
        `data-dot`），而本模块按 `[data-dots]` 找"旧容器"删 —— 选择器对不上
        → 服务端渲染的容器永远删不掉，每次重建都在旁边**再挂一个**。

        SSR 形态由本文件的 `ssrItem` 镜像，模板侧由
        `TestSsrAjaxDotMarkerParity` 锁定；两者配对才是完整的契约护栏。
        """
        body = """
ssrItem(1, { critical: 1, warning: 0 });
PV.updatePageNavDots({ 1: { critical: 1, warning: 0, info: 0, total: 1 } });
console.log(JSON.stringify({
  containers: dotContainers(1),
  children: item(1).children.length,
}));
"""
        assert _probe(body) == {"containers": 1, "children": 2}

    def test_stale_ssr_severity_dot_disappears(self):
        """服务端时刻的严重度圆点必须在刷新后消失。

        后果不是"多两个小点"而是**指向错误的严重度**：SSR 时该页有 critical
        （红点），随后降级为 info；旧容器留着红点，新容器显示数字 —— 复核者
        看到红点会以为该页仍有严重问题。
        """
        body = """
ssrItem(1, { critical: 1, warning: 0 });
PV.updatePageNavDots({ 1: { critical: 0, warning: 0, info: 2, total: 2 } });
const el = item(1);
console.log(JSON.stringify({
  staleRed: el.querySelectorAll('[data-dot="critical"]').length,
  texts: dotsInfo(1).texts,
}));
"""
        assert _probe(body) == {"staleRed": 0, "texts": ["2"]}

    def test_js_dots_carry_the_same_markers_as_ssr(self):
        """JS 重建的圆点必须与 SSR 用**同一套标记** —— 否则下一个人再加一个
        消费者（CSS / e2e / 查询）时，两条渲染路径的行为会再次分叉。"""
        body = """
const n = nav(); addNavItems(n, [1, 2, 3]);
PV.updatePageNavDots({ 1: { critical: 1, warning: 1, info: 0, total: 2 } });
const el = item(1);
console.log(JSON.stringify({
  container: el.querySelectorAll("[data-dots]").length,
  critical: el.querySelectorAll('[data-dot="critical"]').length,
  warning: el.querySelectorAll('[data-dot="warning"]').length,
}));
"""
        assert _probe(body) == {"container": 1, "critical": 1, "warning": 1}

    def test_severity_dots_and_info_number(self):
        """三种形态：严重+警告圆点、仅 info 时显示数字、全零时只有空容器。"""
        body = """
const n = nav(); addNavItems(n, [1, 2, 3]);
PV.updatePageNavDots({
  1: { critical: 2, warning: 1, info: 3, total: 6 },
  2: { critical: 0, warning: 0, info: 3, total: 3 },
  3: { critical: 0, warning: 0, info: 0, total: 0 },
});
console.log(JSON.stringify({
  mixed: dotsInfo(1),
  infoOnly: dotsInfo(2),
  empty: dotsInfo(3),
}));
"""
        assert _probe(body) == {
            "mixed": {
                "n": 2,
                "titles": ["2 严重", "1 警告"],
                "texts": ["", ""],
                "classes": [
                    "w-1 h-1 rounded-full bg-destructive",
                    "w-1 h-1 rounded-full bg-warning",
                ],
            },
            "infoOnly": {
                "n": 1,
                "titles": [""],
                "texts": ["3"],
                "classes": ["text-[11px] tabular-nums text-muted-foreground"],
            },
            "empty": {"n": 0, "titles": [], "texts": [], "classes": []},
        }

    def test_null_counts_is_a_noop(self):
        """SSE 帧里 `page_finding_counts` 可能缺省 → 不得清空已有圆点。"""
        body = """
const n = nav(); addNavItems(n, [1, 2, 3]);
PV.updatePageNavDots({ 1: { critical: 1, warning: 0, info: 0, total: 1 } });
const before = dotsInfo(1);
PV.updatePageNavDots(null);
PV.updatePageNavDots(undefined);
console.log(JSON.stringify({ same: JSON.stringify(before) === JSON.stringify(dotsInfo(1)) }));
"""
        assert _probe(body) == {"same": True}

    def test_missing_page_entry_renders_empty_container(self):
        """某页在 counts 里缺席 → 按全零处理（不得抛错、不得残留旧圆点）。"""
        body = """
const n = nav(); addNavItems(n, [1, 2, 3]);
PV.updatePageNavDots({ 1: { critical: 1, warning: 0, info: 0, total: 1 } });
PV.updatePageNavDots({});
console.log(JSON.stringify({ n: dotsInfo(1).n }));
"""
        assert _probe(body) == {"n": 0}

    def test_no_nav_items_is_a_noop(self):
        body = """
const rv = PV.updatePageNavDots({ 1: { critical: 1, warning: 0, info: 0, total: 1 } });
console.log(JSON.stringify({ rv: rv === undefined }));
"""
        assert _probe(body) == {"rv": True}


# ─────────────────────────────────────────────────────────────────────
# buildPageNav / updatePageNavActive —— 占位态重建 + 选中态
# ─────────────────────────────────────────────────────────────────────
class TestPageNav:
    def test_total_pages_zero_does_not_build(self):
        """OCR 未完成（totalPages=0）时不建导航 —— 页数未知，建出来就是错的。"""
        body = """
const n = nav();
R.state.totalPages = 0;
PV.buildPageNav();
console.log(JSON.stringify({ children: n.children.length }));
"""
        assert _probe(body) == {"children": 0}

    def test_builds_items_with_href_and_active_state(self):
        body = """
const n = nav();
R.state.totalPages = 3;
R.state.currentPage = 2;
PV.buildPageNav();
console.log(JSON.stringify({
  children: n.children.length,
  hrefs: n.children.map((a) => a.href),
  pages: n.children.map((a) => a.dataset.page),
  labels: n.children.map((a) => a.textContent),
  isNavItem: n.children.every((a) => a.classList.contains("page-nav-item")),
  active: item(2).classList.contains("bg-foreground"),
  activeText: item(2).classList.contains("text-background"),
  inactiveMuted: item(1).classList.contains("text-muted-foreground"),
}));
"""
        assert _probe(body) == {
            "children": 3,
            "hrefs": [
                "/jobs/J1/review?page=1",
                "/jobs/J1/review?page=2",
                "/jobs/J1/review?page=3",
            ],
            "pages": ["1", "2", "3"],
            "labels": ["第1页", "第2页", "第3页"],
            "isNavItem": True,
            "active": True,
            "activeText": True,
            "inactiveMuted": True,
        }

    def test_click_navigates_without_full_reload(self):
        """点击页码必须 `preventDefault` 后走 SPA 翻页 —— 否则整页刷新，
        未保存的裁决输入全丢。"""
        body = """
const n = nav();
R.state.totalPages = 3;
R.state.currentPage = 1;
PV.buildPageNav();
const prevented = click(n.children[2]);
console.log(JSON.stringify({ prevented, goPage: __goPage }));
"""
        assert _probe(body) == {"prevented": True, "goPage": [3]}

    def test_active_toggles_both_ways(self):
        """选中态必须**双向**切换：只加不移 → 多个页码同时高亮。"""
        body = """
const n = nav(); addNavItems(n, [1, 2, 3]);
PV.updatePageNavActive(1);
const a1 = item(1).classList.contains("bg-foreground");
PV.updatePageNavActive(3);
console.log(JSON.stringify({
  a1After: item(1).classList.contains("bg-foreground"),
  a1Muted: item(1).classList.contains("text-muted-foreground"),
  a3: item(3).classList.contains("bg-foreground"),
  activeCount: [1, 2, 3].filter((p) => item(p).classList.contains("bg-foreground")).length,
}));
"""
        assert _probe(body) == {
            "a1After": False,
            "a1Muted": True,
            "a3": True,
            "activeCount": 1,
        }


# ─────────────────────────────────────────────────────────────────────
# 缩放
# ─────────────────────────────────────────────────────────────────────
class TestZoom:
    def test_zoom_formats_percent_and_clamps(self):
        """步进结果必须夹在 50%~200%，并以整数百分比写入 style/label。

        ⚠️ 诚实边界：`zoomPdf` 里的 `Math.round(...*100)/100` **本身不可观测**
        —— `applyZoom` 用 `Math.round(pdfZoom * 100)` 再取整一次，所以内部
        浮点毛刺（1.3000000000000003）永远到不了 DOM。该处取整是卫生措施，
        不是可测行为；本用例锁定的是**夹取 + 百分比格式化**。
        """
        body = """
mk("pdf-page-img"); mk("pdf-zoom-label");
imgEl().src = "";
PV.zoomPdf(0.1); PV.zoomPdf(0.1); PV.zoomPdf(0.1);
const at130 = { label: txt("pdf-zoom-label"), width: imgEl().style.width };
PV.zoomPdf(5);
const atMax = txt("pdf-zoom-label");
PV.zoomPdf(-9);
const atMin = txt("pdf-zoom-label");
PV.resetZoom();
console.log(JSON.stringify({ at130, atMax, atMin, reset: txt("pdf-zoom-label") }));
"""
        assert _probe(body) == {
            "at130": {"label": "130%", "width": "130%"},
            "atMax": "200%",
            "atMin": "50%",
            "reset": "100%",
        }

    def test_apply_zoom_repositions_region_overlay(self):
        """缩放后区域框必须按**新的图片像素尺寸**重算。

        框用绝对像素定位（`img.offsetWidth` 基准），图片放大而框不重算
        就会跑偏 —— 框指向的位置与实际版面错位，比不显示更危险。
        """
        body = """
const img = pageImg(600, 800);
mk("region-overlay"); mk("pdf-zoom-label");
ovEl().dataset.bbox = "0.25,0.5,0.75,1";
ovEl().classList.remove("hidden");
PV.applyZoom();
const before = ovEl().style.width;
img.offsetWidth = 1200;
img.offsetHeight = 1600;
PV.zoomPdf(0.5);
console.log(JSON.stringify({
  before,
  after: ovEl().style.width,
  left: ovEl().style.left,
  top: ovEl().style.top,
  height: ovEl().style.height,
  label: txt("pdf-zoom-label"),
}));
"""
        assert _probe(body) == {
            "before": "300px",
            "after": "600px",
            "left": "300px",
            "top": "800px",
            "height": "800px",
            "label": "150%",
        }

    def test_missing_image_is_tolerated(self):
        body = """
mk("pdf-zoom-label");
const rv = PV.zoomPdf(0.1);
console.log(JSON.stringify({ rv: rv === undefined, label: txt("pdf-zoom-label") }));
"""
        assert _probe(body) == {"rv": True, "label": "110%"}


# ─────────────────────────────────────────────────────────────────────
# locateRegion —— 区域级证据锚
# ─────────────────────────────────────────────────────────────────────
class TestLocateRegion:
    def test_draws_box_in_image_pixel_space(self):
        """框按图片像素定位：left/top/width/height 全部由归一化 bbox × 图片尺寸推出。"""
        body = """
const img = pageImg(600, 800);
mk("region-overlay");
ovEl().classList.add("hidden");
R.state.regionRefs = { 7: regionRef() };
let stopped = false;
PV.locateRegion({ stopPropagation() { stopped = true; } }, 7);
console.log(JSON.stringify({
  stopped,
  hidden: ovEl().classList.contains("hidden"),
  bbox: ovEl().dataset.bbox,
  left: ovEl().style.left,
  top: ovEl().style.top,
  width: ovEl().style.width,
  height: ovEl().style.height,
}));
"""
        assert _probe(body) == {
            "stopped": True,
            "hidden": False,
            "bbox": "0.25,0.5,0.75,1",
            "left": "150px",
            "top": "400px",
            "width": "300px",
            "height": "400px",
        }

    def test_second_click_collapses(self):
        """再点一次同一 finding 必须收起 —— 否则框无法关闭。"""
        body = """
const img = pageImg(600, 800);
mk("region-overlay");
R.state.regionRefs = { 7: regionRef() };
PV.locateRegion(null, 7);
const opened = !ovEl().classList.contains("hidden");
PV.locateRegion(null, 7);
console.log(JSON.stringify({ opened, closed: ovEl().classList.contains("hidden") }));
"""
        assert _probe(body) == {"opened": True, "closed": True}

    def test_switching_finding_keeps_box_open(self):
        """点另一个 finding 直接换框（不是先收起再展开）。"""
        body = """
const img = pageImg(600, 800);
mk("region-overlay");
R.state.regionRefs = {
  7: regionRef(),
  8: regionRef({ page_bbox: [0.5, 0, 1.0, 0.25] }),
};
PV.locateRegion(null, 7);
PV.locateRegion(null, 8);
console.log(JSON.stringify({
  hidden: ovEl().classList.contains("hidden"),
  bbox: ovEl().dataset.bbox,
  width: ovEl().style.width,
}));
"""
        assert _probe(body) == {
            "hidden": False,
            "bbox": "0.5,0,1,0.25",
            "width": "300px",
        }

    def test_aspect_gate_refuses_to_draw_a_misplaced_box(self):
        """宽高比闸门：坐标系与页面不同源时**明示无法定位**，不画错框。

        画一个错位的框会把复核员的注意力引到无关区域 —— 比不显示更危险。
        """
        body = """
const img = pageImg(600, 800);
mk("region-overlay");
ovEl().classList.add("hidden");
R.state.regionRefs = { 7: regionRef({ page_aspect: 0.7 }) };
PV.locateRegion(null, 7);
console.log(JSON.stringify({
  hidden: ovEl().classList.contains("hidden"),
  toasts: __toasts,
  bbox: ovEl().dataset.bbox || "",
}));
"""
        assert _probe(body) == {
            "hidden": True,
            "toasts": [
                {
                    "msg": "该页坐标系与页面方向不一致（上游未上报旋转角），无法自动定位，请人工核对原图",
                    "type": "err",
                }
            ],
            "bbox": "",
        }

    def test_aspect_gate_passes_within_tolerance(self):
        """容差 2% 内的差异视为同源（渲染图取整、DPI 折算会有微小偏差）。"""
        body = """
const img = pageImg(600, 800);
mk("region-overlay");
ovEl().classList.add("hidden");
R.state.regionRefs = { 7: regionRef({ page_aspect: 0.755 }) };
PV.locateRegion(null, 7);
console.log(JSON.stringify({
  hidden: ovEl().classList.contains("hidden"),
  toasts: __toasts.length,
}));
"""
        assert _probe(body) == {"hidden": False, "toasts": 0}

    def test_aspect_gate_rejects_rotated_page(self):
        """旋转页最典型的失配：图片 800×600（1.333）而页面应为 1.0。"""
        body = """
const img = pageImg(800, 600);
mk("region-overlay");
ovEl().classList.add("hidden");
R.state.regionRefs = { 7: regionRef({ page_aspect: 1.0 }) };
PV.locateRegion(null, 7);
console.log(JSON.stringify({ hidden: ovEl().classList.contains("hidden"), toasts: __toasts.length }));
"""
        assert _probe(body) == {"hidden": True, "toasts": 1}

    def test_missing_aspect_skips_gate(self):
        """`page_aspect` 缺失（后端未上报 space_aspect）时不拦 —— 此时
        page_bbox 与渲染图同源（未旋转），画出来是对的。"""
        body = """
const img = pageImg(600, 800);
mk("region-overlay");
ovEl().classList.add("hidden");
R.state.regionRefs = { 7: regionRef({ page_aspect: null }) };
PV.locateRegion(null, 7);
console.log(JSON.stringify({
  hidden: ovEl().classList.contains("hidden"),
  toasts: __toasts.length,
}));
"""
        assert _probe(body) == {"hidden": False, "toasts": 0}

    def test_unloaded_image_skips_gate(self):
        """图片尚未加载（naturalWidth=0）时不能拿 0 去比 —— 否则所有锚点
        在首帧都会被误判为"坐标系不一致"。"""
        body = """
const img = pageImg(0, 0);
mk("region-overlay");
ovEl().classList.add("hidden");
R.state.regionRefs = { 7: regionRef({ page_aspect: 0.7 }) };
PV.locateRegion(null, 7);
console.log(JSON.stringify({ hidden: ovEl().classList.contains("hidden"), toasts: __toasts.length }));
"""
        assert _probe(body) == {"hidden": False, "toasts": 0}

    def test_missing_ref_is_a_noop(self):
        body = """
const img = pageImg(600, 800);
mk("region-overlay");
ovEl().classList.add("hidden");
R.state.regionRefs = {};
PV.locateRegion(null, 99);
console.log(JSON.stringify({
  hidden: ovEl().classList.contains("hidden"),
  toasts: __toasts.length,
}));
"""
        assert _probe(body) == {"hidden": True, "toasts": 0}

    def test_missing_image_or_overlay_is_a_noop(self):
        body = """
R.state.regionRefs = { 7: regionRef() };
PV.locateRegion(null, 7);       // 无 img / 无 overlay
mk("region-overlay");
PV.locateRegion(null, 7);       // 只有 overlay
console.log(JSON.stringify({ toasts: __toasts.length }));
"""
        assert _probe(body) == {"toasts": 0}

    def test_id_type_does_not_matter_for_the_toggle(self):
        """id 的**类型**不得影响"再点一次收起"的判定。

        模板内联 onclick 渲染的是数字字面量（`locateRegion(event, 7)`），但
        事件透传/其他调用点可能给出字符串。`activeRegionFid` 用 `===` 比较，
        不做 `Number()` 归一时 `7 !== "7"` → 第二次点击**收不起来**，
        复核者无法关闭区域框。

        ⚠️ 诚实边界：单用字符串 id 是测不出来的 —— `regionRefs` 的键本就是
        字符串（对象属性名），`regionRefs["7"]` 与 `regionRefs[7]` 等价。
        必须**混用**两种类型才能暴露 `===` 的类型敏感。
        """
        body = """
const img = pageImg(600, 800);
mk("region-overlay");
R.state.regionRefs = { 7: regionRef() };
PV.locateRegion(null, 7);          // 数字（模板内联 onclick 的形态）
const opened = !ovEl().classList.contains("hidden");
PV.locateRegion(null, "7");        // 字符串（事件透传）
console.log(JSON.stringify({
  opened,
  closed: ovEl().classList.contains("hidden"),
}));
"""
        assert _probe(body) == {"opened": True, "closed": True}

    def test_scrolls_region_into_view_when_below_the_fold(self):
        """区域在容器可视区之外时必须滚到它附近 —— 否则复核者只看到一片空白，
        以为"没有框"。"""
        body = """
const img = pageImg(600, 800);
mk("region-overlay"); mk("pdf-scroll");
const scroll = document.getElementById("pdf-scroll");
scroll.scrollTop = 0;
scroll.clientHeight = 400;
ovEl().offsetTop = 500;          // 模拟：框在可视区之下
R.state.regionRefs = { 7: regionRef() };
PV.locateRegion(null, 7);
console.log(JSON.stringify({ scrollTop: scroll.scrollTop }));
"""
        # 500 - 400/3 = 366.67
        got = _probe(body)
        assert abs(got["scrollTop"] - 366.66666666666663) < 1e-6

    def test_no_scroll_when_region_already_visible(self):
        body = """
const img = pageImg(600, 800);
mk("region-overlay"); mk("pdf-scroll");
const scroll = document.getElementById("pdf-scroll");
scroll.scrollTop = 0;
scroll.clientHeight = 400;
ovEl().offsetTop = 100;
R.state.regionRefs = { 7: regionRef() };
PV.locateRegion(null, 7);
console.log(JSON.stringify({ scrollTop: scroll.scrollTop }));
"""
        assert _probe(body) == {"scrollTop": 0}

    def test_invalid_bbox_does_not_throw(self):
        """bbox 残缺时定位函数直接放弃（不得算出 NaN 像素写进 style）。"""
        body = """
const img = pageImg(600, 800);
mk("region-overlay");
ovEl().dataset.bbox = "0.1,0.2";
PV.positionRegionOverlay();
console.log(JSON.stringify({ left: ovEl().style.left || "" }));
"""
        assert _probe(body) == {"left": ""}

    def test_clear_region_anchor_hides_overlay(self):
        body = """
mk("region-overlay");
PV.clearRegionAnchor();
console.log(JSON.stringify({ hidden: ovEl().classList.contains("hidden") }));
"""
        assert _probe(body) == {"hidden": True}

    def test_exports_for_inline_onclick(self):
        """模板内联 onclick 需要 zoomPdf / resetZoom / locateRegion 挂在 window。"""
        body = """
console.log(JSON.stringify({
  zoom: typeof window.zoomPdf,
  reset: typeof window.resetZoom,
  locate: typeof window.locateRegion,
  api: Object.keys(PV).sort().join(","),
}));
"""
        assert _probe(body) == {
            "zoom": "function",
            "reset": "function",
            "locate": "function",
            "api": "applyZoom,buildPageNav,clearRegionAnchor,locateRegion,"
                   "positionRegionOverlay,resetZoom,updatePageNavActive,"
                   "updatePageNavDots,updatePdfDisplay,zoomPdf",
        }


# ─────────────────────────────────────────────────────────────────────
# SSR ↔ AJAX 标记一致（"两条渲染路径各写各的"是本仓库的反复缺陷类型）
# ─────────────────────────────────────────────────────────────────────
class TestSsrAjaxDotMarkerParity:
    """页码圆点由 SSR（首屏）与 AJAX（实时帧）两条路径渲染。

    两套标记必须**一致** —— 否则 AJAX 的"删旧建新"删不掉 SSR 的产物，
    首屏圆点会永久残留（见 `test_stale_ssr_severity_dot_disappears`）。
    这两个断言是**生产者侧**的锁：模板改了标记名，这里立刻红。
    """

    def test_template_marks_the_dots_container(self):
        html = REVIEW_HTML.read_text(encoding="utf-8")
        assert 'data-dots="1"' in html, (
            'SSR 圆点容器必须带 data-dots="1" —— review-pageview.js 用 '
            '`querySelectorAll("[data-dots]")` 找并删除旧容器；容器不带该标记 '
            "则服务端圆点永远删不掉，实时刷新会叠加出陈旧的严重度圆点"
        )

    def test_js_and_template_agree_on_child_markers(self):
        js = (REPO / "static" / "review-pageview.js").read_text(encoding="utf-8")
        html = REVIEW_HTML.read_text(encoding="utf-8")
        for sev in ("critical", "warning"):
            assert f'data-dot="{sev}"' in html, f"SSR 圆点须带 data-dot={sev}"
            assert f'setAttribute("data-dot", "{sev}")' in js, (
                f'JS 生成的圆点须带 data-dot="{sev}" —— 与 SSR 同标记'
            )

    def test_both_paths_render_the_same_three_branches(self):
        """两条路径都必须：critical→红点、warning→黄点、
        仅 info 时显示 total 数字（且 total 是两者共用的口径）。"""
        js = (REPO / "static" / "review-pageview.js").read_text(encoding="utf-8")
        html = REVIEW_HTML.read_text(encoding="utf-8")
        for src, name in ((js, "review-pageview.js"), (html, "review.html")):
            assert "bg-destructive" in src, name
            assert "bg-warning" in src, name
            assert "total" in src, name
        assert "c.critical === 0 && c.warning === 0" in js, (
            "info 数字只在无严重/警告时显示 —— SSR 侧对应 "
            "`pfc.critical == 0 and pfc.warning == 0`"
        )
        assert "pfc.critical == 0 and pfc.warning == 0" in html
