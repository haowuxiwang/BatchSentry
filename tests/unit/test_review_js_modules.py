"""R63 review.js 拆分的两道结构护栏。

**为什么需要**：拆分把 2037 行的 review.js 按职责切成 8 个模块
（review-state / locate / pageview / pageinfo / findings / suppressions /
progress + 入口 review.js），模板用 13 个 defer script 按依赖序加载。
拆分当轮就暴露过一个**真实缺陷**：原文件里两个同名 `locateFinding`
（card 版做 OCR 文本定位 / 事件版做区域锚定）因函数声明提升互相遮蔽，
"点击卡片高亮 OCR 原文"自引入以来静默失效 —— 拆分时才借改名修复。
静态结构（加载顺序 / 命名唯一 / 导出齐全）错了**不会抛异常**，
只会静默 undefined，必须靠机检钉住：

1. **TestNoShadowing**：`locateFinding` 同名遮蔽缺陷的回归门 —— 任何
   review*.js 里重新出现该函数声明、或事件委托不指向
   locateInOcrPanel，立刻红。
2. **TestScriptOrder**：review.html 必须按依赖序引入全部模块 ——
   review-state 先行、共享件先于消费者、入口最后；漏引一个
   新模块 = 该模块全部职责静默消失（页面上无任何报错）。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATIC = REPO / "static"
REVIEW_HTML = REPO / "templates" / "review.html"

# 拆分后的模块清单（不含入口 review.js —— 它单独断言"必须最后"）
REVIEW_MODULES = [
    "review-state.js",       # PbcReview 命名空间 + 共享状态（一切模块的依赖）
    "review-locate.js",      # OCR 面板文本定位
    "review-pageview.js",    # PDF 页图/缩放/页码导航/区域锚
    "review-pageinfo.js",    # 页面级 UI（置信度/横幅/参数矩阵/豁免区）
    "review-findings.js",    # findings 渲染与裁决
    "review-suppressions.js",  # 抑制留痕面板
    "review-progress.js",    # SSE 实时进度（依赖 sse.js/status.js/eta.js）
]

# review.html 里实际需要的共享件（拆分引入的依赖，漏一个即静默失效）
SHARED_DEPS = [
    "eta.js",            # PbcEta（review-progress.js ETA 采样池）
    "status.js",         # PbcStatus（状态中文/状态点颜色单一真值）
    "sse.js",            # PbcSse（manual/auto 双模式重连骨架）
    "findings-map.js",   # PbcFindingsMap（findings 中文映射单一真值）
    "confirm-dialog.js",  # PBC.confirmDialog（取消/重试确认弹窗）
]

# 显式依赖序（前项必须先于后项加载）：共享件 → state → 各模块 → 入口
_ORDER_PAIRS = [
    ("status.js", "sse.js"),            # sse.js 缺省判定用 PbcStatus（运行期）
    ("sse.js", "review-progress.js"),
    ("findings-map.js", "review-findings.js"),
    ("review-state.js", "review-locate.js"),
    ("review-state.js", "review-pageview.js"),
    ("review-state.js", "review-pageinfo.js"),
    ("review-state.js", "review-findings.js"),
    ("review-state.js", "review-suppressions.js"),
    ("review-state.js", "review-progress.js"),
    ("review-pageview.js", "review.js"),   # 入口编排消费 pageview
    ("review-progress.js", "review.js"),  # 入口订阅 SSE 进度
    ("review-findings.js", "review.js"),  # 入口数据层调用 findings 渲染
]


def _all_review_js() -> list[Path]:
    return sorted(STATIC.glob("review*.js"))


def _script_srcs(html: str) -> list[str]:
    """按出现顺序取出 review.html 里 defer script 的静态文件名。

    模板带 `?v={{ asset_ver(...) }}` 版本戳，匹配到 `.js` 为止。
    """
    return re.findall(r'<script[^>]*src="/static/([\w.-]+\.js)[?"]', html)


class TestNoShadowing:
    """locateFinding 同名遮蔽缺陷（拆分时修复）的回归门。

    缺陷形态：两个 `function locateFinding` 声明，后者（区域锚版）因
    函数声明提升遮蔽前者（OCR 文本定位版）⇒ 事件委托把 card 传给
    区域版 → `regionRefs[NaN]` undefined → 静默 return。修复方式是
    改名（locateInOcrPanel / locateRegion），本护栏锁"改名成果不被
    冲掉 + 各归其位"。
    """

    def test_no_locate_finding_declaration_anywhere(self):
        """任何 review*.js 里都不得再出现 `function locateFinding` 声明。

        这是缺陷的根因形态：只要同名声明回归，提升遮蔽就回来了。
        只匹配**行首函数声明**（真实声明都在行首）—— review-locate.js
        的头注里引用了旧名作为命名史说明，那是合法的文字性提及。
        """
        for p in _all_review_js():
            src = p.read_text(encoding="utf-8")
            assert not re.search(r"^[ \t]*function\s+locateFinding\s*\(", src, re.M), (
                f"{p.name} 重新声明了 locateFinding —— 同名遮蔽缺陷"
                f"（OCR 文本定位静默失效）的根因形态，改回独立命名"
            )

    def test_locate_in_ocr_panel_exists_and_exported(self):
        """card 版（OCR 文本定位）必须存在且挂到 R.locate 命名空间。"""
        src = (STATIC / "review-locate.js").read_text(encoding="utf-8")
        assert "function locateInOcrPanel" in src, (
            "review-locate.js 缺少 locateInOcrPanel（card 版 OCR 文本定位）"
        )
        assert "locateInOcrPanel: locateInOcrPanel" in src, (
            "locateInOcrPanel 未挂到 R.locate —— 事件委托无处调用"
        )

    def test_locate_region_exists_and_on_window(self):
        """区域锚版必须存在且挂到 window（模板内联 onclick 依赖）。"""
        src = (STATIC / "review-pageview.js").read_text(encoding="utf-8")
        assert "function locateRegion" in src, (
            "review-pageview.js 缺少 locateRegion（区域锚版）"
        )
        assert "global.locateRegion = locateRegion;" in src, (
            "locateRegion 未挂到 window —— 模板内联 onclick 会 ReferenceError"
        )

    def test_event_delegation_targets_locate_in_ocr_panel(self):
        """入口事件委托必须指向 card 版（R.locate.locateInOcrPanel）。

        缺陷发生时这里指向被遮蔽的同名函数 —— 传 card 对象给区域版，
        NaN 索引 + 静默 return。委托指向是修复的**消费点**，必须钉住。
        """
        src = (REPO / "static" / "review.js").read_text(encoding="utf-8")
        assert "R.locate.locateInOcrPanel(card)" in src, (
            "findings 卡片点击委托未指向 R.locate.locateInOcrPanel —— "
            "同名遮蔽缺陷的回归（点击卡片高亮 OCR 原文会静默失效）"
        )

    def test_inline_onclicks_use_locate_region(self):
        """内联 onclick（模板 SSR + findings 渲染）统一用 locateRegion。"""
        html = REVIEW_HTML.read_text(encoding="utf-8")
        assert "locateRegion(event," in html, "模板 SSR 定位入口缺失"
        assert "locateFinding(event," not in html, (
            "模板仍有 locateFinding 残留 —— 对应函数已改名，点击会 ReferenceError"
        )
        fjs = (STATIC / "review-findings.js").read_text(encoding="utf-8")
        assert "locateRegion(event," in fjs, "AJAX findings 渲染缺定位入口"


class TestScriptOrder:
    """review.html 的 defer script 必须完整且按依赖序排列。

    defer 保证按**文档顺序**执行；漏引一个模块或顺序颠倒
    （消费者先于依赖）不会有任何报错 —— 只有 undefined 静默失效。
    """

    def test_all_modules_are_referenced(self):
        """每个拆分模块 + 入口 + 共享件都必须被模板引用。"""
        srcs = _script_srcs(REVIEW_HTML.read_text(encoding="utf-8"))
        needed = REVIEW_MODULES + ["review.js"] + SHARED_DEPS
        missing = [m for m in needed if m not in srcs]
        assert not missing, (
            f"review.html 漏引这些模块 —— 对应职责会静默消失：{missing}"
        )

    def test_no_unreferenced_review_module(self):
        """反向：static 里的 review*.js 若没被模板引用 = 死代码或漏接线。"""
        srcs = _script_srcs(REVIEW_HTML.read_text(encoding="utf-8"))
        on_disk = [p.name for p in _all_review_js()]
        orphan = [n for n in on_disk if n not in srcs]
        assert not orphan, (
            f"static 里这些 review 模块未被模板引用（死代码或漏接线）：{orphan}"
        )

    def test_dependency_order(self):
        """显式依赖序：前项的 script 位置必须先于后项。"""
        srcs = _script_srcs(REVIEW_HTML.read_text(encoding="utf-8"))
        pos = {name: i for i, name in enumerate(srcs)}
        for before, after in _ORDER_PAIRS:
            assert before in pos and after in pos, (
                f"依赖序检查的锚点缺失：{before} / {after}"
            )
            assert pos[before] < pos[after], (
                f"加载顺序颠倒：{before} 必须先于 {after}（defer 按文档序执行，"
                f"颠倒即 undefined 静默失效）"
            )

    def test_entry_point_is_last(self):
        """入口 review.js 必须是最后一个 review 系 script（编排依赖全部模块）。"""
        srcs = _script_srcs(REVIEW_HTML.read_text(encoding="utf-8"))
        review_like = [s for s in srcs if s.startswith("review")]
        assert review_like, "模板未引用任何 review*.js"
        assert review_like[-1] == "review.js", (
            f"入口 review.js 应最后加载（当前最后的是 {review_like[-1]}）—— "
            f"DOMContentLoaded 编排要访问全部子模块的命名空间"
        )
