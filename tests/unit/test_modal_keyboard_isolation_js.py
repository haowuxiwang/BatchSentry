"""弹窗的键盘隔离：弹窗打开时，页面级快捷键必须停用（P2，本轮新发现）。

**缺陷（定位）**：`confirm-dialog.js` 的 `onKey` 只处理 `Esc` / `Enter` / `Tab`，
其余按键**继续冒泡**到页面级的 `document` keydown。复核页在那里用 `←` `→` 翻页，
于是在"取消任务"确认框开着时按方向键会**翻动背后的页面**：

* 弹窗还在，上下文却已经换了 —— 正要确认的那条 finding 已不在屏幕上；
* 翻页还会触发 `loadPageData()` 重渲染问题清单与 PDF，用户看到的与确认的内容脱节。

**为什么不能在弹窗侧拦**：两个监听器**都挂在 `document` 上**，而页面级监听器
在模块加载期就注册了（早于弹窗创建）。同一 target 上的监听器按**注册序**触发，
且不受 `stopPropagation()` 影响 ⇒ 弹窗侧加 `stopPropagation` 也来不及。

**修法**：`confirm-dialog.js` 暴露 `window.PBC.isDialogOpen()`（以 DOM 里的
`role="dialog"` 为单一真值，不新增计数器），消费方在快捷键处理里主动查询。
`pbc-fallback.js` 补一个恒 `false` 的降级版（模块缺席 ⇒ 不可能有弹窗）。
"""
from __future__ import annotations

from tests.js_harness import run_js_async

# review.js 的**模块作用域**会执行 `R.safeAutoReload = R.progress.safeAutoReload`
# 该赋值在 review.js 的模块作用域，故必须加载 review-progress.js。
FILES = [
    "pbc-fallback.js",
    "confirm-dialog.js",
    "review-state.js",
    "review-progress.js",
    "review.js",
]
FILES_NO_DIALOG = [
    "pbc-fallback.js",
    "review-state.js",
    "review-progress.js",
    "review.js",
]

_STUB = r"""
globalThis.__calls = { fetch: [], warn: [] };
globalThis.__PBC__ = { job_id: "J1", page: 1, total_pages: 3 };
globalThis.history = { pushState: () => {} };
globalThis.location = { href: "" };

globalThis.fetch = async (url) => {
  __calls.fetch.push(String(url));
  const body = String(url).indexOf("findings") >= 0
    ? { findings: [], count: 0, has_more: false }
    : String(url).indexOf("measurements") >= 0
      ? { measurements: [] }
      : { raw_html: "", total_pages: 3 };
  return { ok: true, status: 200, json: async () => body };
};

/* review.js 的翻页链路会调用若干兄弟模块（pageview/pageinfo/findings/
 * suppressions/locate）。**这些模块本用例刻意不加载** —— 只提供最小接口让
 * 翻页流程跑完，从而把断言聚焦在"是否发起翻页请求"上。
 * ⚠️ 不能反向操作（加载真模块再覆盖）：那会变成"用桩覆盖真实共享件"，
 * 断言随之失真。此处这些模块根本没加载，不存在覆盖问题。 */
globalThis.__installCollaborators = () => {
  Object.assign(globalThis.PbcReview, {
    pageview: {
      updatePageNavActive() {},
      updatePdfDisplay() {},
      positionRegionOverlay() {},
    },
    pageinfo: { updatePageLevelUI() {}, applyInitialPageLevelUI() {} },
    findings: { renderFindings() {} },
    suppressions: { loadSuppressions: async () => {} },
    locate: { htmlToText: (s) => s || "", locateInOcrPanel() {} },
  });
};
"""

_PRE = r"""
__installCollaborators();

const settle = () => new Promise((r) => setImmediate(r));
const drain = async (n) => { for (let i = 0; i < (n || 10); i++) await settle(); };

/* 只数"翻页数据请求"，避免与其它 fetch（统计等）混淆 */
const pageFetches = () => __calls.fetch.filter((u) => /\/pages\/\d+$/.test(u));

/* ⚠️ harness 的 __keydown 不注入 target，而 review.js 的处理器会读
 * `e.target.tagName` —— 不传就抛 TypeError（会把"未翻页"误判成"被拦住了"）。 */
const arrow = (key) => __keydown(key, { target: { tagName: "BODY" } });

const btns = () => __queryAll(document.body, "button");
const openDialog = (title) => window.PBC.confirmDialog({ title: title || "T" });
const closeTopDialog = () => { btns()[0].dispatchEvent({ type: "click" }); __flushTimers(); };
"""


def _run(body: str, *, files=None, stub_extra: str = ""):
    return run_js_async(files or FILES, _PRE + "\n" + body, stub=_STUB + stub_extra)


# ══════════════════════════════════════════════════════════════════════
class TestHarnessIsNotVacuous:
    """先证明"能观测到翻页"这件事本身成立 —— 否则"没翻页"可能是探针坏了。"""

    def test_arrow_right_navigates_when_no_dialog_is_open(self):
        """防空转：没有弹窗时，→ 必须真的发起翻页请求。"""
        assert _run("""
arrow("ArrowRight");
await drain();
console.log(JSON.stringify({
  fetches: pageFetches(),
  isOpen: window.PBC.isDialogOpen(),
}));
""") == {"fetches": ["/api/jobs/J1/pages/2"], "isOpen": False}

    def test_arrow_left_navigates_from_page_two(self):
        """防空转（反向）：← 也要真的能翻。"""
        assert _run("""
window.PbcReview.state.currentPage = 2;
arrow("ArrowLeft");
await drain();
console.log(JSON.stringify({ fetches: pageFetches() }));
""") == {"fetches": ["/api/jobs/J1/pages/1"]}

    def test_dialog_module_exposes_the_flag(self):
        assert _run("""
console.log(JSON.stringify({
  type: typeof window.PBC.isDialogOpen,
  closed: window.PBC.isDialogOpen(),
}));
""") == {"type": "function", "closed": False}


# ══════════════════════════════════════════════════════════════════════
class TestDialogIsOpenTracksTheDom:
    """`isDialogOpen()` 必须反映 DOM 里真实的弹窗，而不是一个独立计数器。"""

    def test_open_then_settled(self):
        assert _run("""
const before = window.PBC.isDialogOpen();
openDialog("A");
const during = window.PBC.isDialogOpen();
closeTopDialog();
await drain();
console.log(JSON.stringify({
  before: before, during: during, after: window.PBC.isDialogOpen(),
}));
""") == {"before": False, "during": True, "after": False}

    def test_stacked_dialogs_stay_open_until_the_last_closes(self):
        assert _run("""
openDialog("A");
openDialog("B");
const two = window.PBC.isDialogOpen();
closeTopDialog();                       // 关 A
const one = window.PBC.isDialogOpen();
closeTopDialog();                       // 关 B
await drain();
console.log(JSON.stringify({ two: two, one: one, none: window.PBC.isDialogOpen() }));
""") == {"two": True, "one": True, "none": False}

    def test_prompt_dialog_also_counts(self):
        """两种弹窗都带 role="dialog" —— 输入框开着时同样必须隔离。"""
        assert _run("""
window.PBC.promptDialog({ title: "原因" });
console.log(JSON.stringify({ open: window.PBC.isDialogOpen() }));
""") == {"open": True}


# ══════════════════════════════════════════════════════════════════════
class TestArrowKeysAreSuppressedWhileAModalIsOpen:
    """核心回归门：弹窗打开时，← → 不得翻页。"""

    def test_arrow_right_is_ignored_while_dialog_is_open(self):
        assert _run("""
openDialog("取消任务？");
arrow("ArrowRight");
await drain();
console.log(JSON.stringify({ fetches: pageFetches(), page: window.PbcReview.state.currentPage }));
""") == {"fetches": [], "page": 1}

    def test_arrow_left_is_ignored_while_dialog_is_open(self):
        assert _run("""
window.PbcReview.state.currentPage = 2;
openDialog("取消任务？");
arrow("ArrowLeft");
await drain();
console.log(JSON.stringify({ fetches: [] , page: window.PbcReview.state.currentPage }));
""") == {"fetches": [], "page": 2}

    def test_navigation_resumes_after_the_dialog_closes(self):
        """隔离必须是"暂时的"——弹窗关掉后键盘导航要恢复正常。"""
        assert _run("""
openDialog("A");
arrow("ArrowRight");
await drain();
const blocked = pageFetches();
closeTopDialog();
await drain();
arrow("ArrowRight");
await drain();
console.log(JSON.stringify({ blocked: blocked, after: pageFetches() }));
""") == {"blocked": [], "after": ["/api/jobs/J1/pages/2"]}

    def test_stacked_dialogs_keep_suppressing_until_all_close(self):
        assert _run("""
openDialog("A");
openDialog("B");
arrow("ArrowRight");
await drain();
closeTopDialog();            // 仍剩一个
arrow("ArrowRight");
await drain();
const stillBlocked = pageFetches();
closeTopDialog();
await drain();
arrow("ArrowRight");
await drain();
console.log(JSON.stringify({ stillBlocked: stillBlocked, after: pageFetches() }));
""") == {"stillBlocked": [], "after": ["/api/jobs/J1/pages/2"]}

    def test_typing_in_an_input_is_still_ignored(self):
        """回归：原有的 INPUT/TEXTAREA 守卫不能被新守卫挤掉。"""
        assert _run("""
arrow2 = (k) => __keydown(k, { target: { tagName: "INPUT" } });
arrow2("ArrowRight");
await drain();
console.log(JSON.stringify({ fetches: pageFetches() }));
""") == {"fetches": []}


# ══════════════════════════════════════════════════════════════════════
class TestFallbackKeepsKeyboardNavigationAlive:
    """`confirm-dialog.js` 缺席时（无弹窗），键盘导航不得被新守卫废掉。"""

    def test_shim_provides_a_false_flag(self):
        assert _run("""
console.log(JSON.stringify({
  type: typeof window.PBC.isDialogOpen,
  open: window.PBC.isDialogOpen(),
}));
""", files=FILES_NO_DIALOG) == {"type": "function", "open": False}

    def test_arrow_still_navigates_without_the_dialog_module(self):
        assert _run("""
arrow("ArrowRight");
await drain();
console.log(JSON.stringify({ fetches: pageFetches() }));
""", files=FILES_NO_DIALOG) == {"fetches": ["/api/jobs/J1/pages/2"]}

    def test_guard_fails_open_if_the_flag_is_missing_entirely(self):
        """兜底：连降级方法都没有时，宁可放行也不能抛错。

        本处理器在**每次按键**上运行 —— 抛错会连带废掉全部键盘导航，
        比"方向键穿透"更糟。故 review.js 里刻意做了存在性判断。
        """
        assert _run("""
delete window.PBC.isDialogOpen;
arrow("ArrowRight");
await drain();
console.log(JSON.stringify({ fetches: pageFetches() }));
""") == {"fetches": ["/api/jobs/J1/pages/2"]}
