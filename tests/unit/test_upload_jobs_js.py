"""upload-jobs.js 的行为护栏（此前只有**源码字符串**断言，零行为覆盖）。

**为什么必须测**：本模块负责历史列表的行渲染与实时刷新。这里每一条分支都对应
一个"用户看不到真相"的合规场景：

- **#134（P0）**：`buildMetaLine` 之前"静态渲染"与"实时更新"各写一份，而实时那份
  压根没写 meta ⇒ 任务一进入实时更新，**失败页数就消失**，用户再也看不到哪几页失败。
  本文件用**两条路径都走一遍**的方式钉住它（静态渲染 + SSE 帧），这类"两条路径
  各写各的"漂移在结构上不可能再靠源码扫描发现。
- **#127**：`error` / `partial_review` 必须把失败原因直接显示在行内 —— 否则
  "0 条 finding"到底是记录没问题、还是这次根本没分析成功，界面上无法区分。
- **运行中任务禁用删除**：避免孤儿 pipeline task。
- **文件名 XSS**：`job.id` / `job.filename` 是不可信输入（此前用 innerHTML +
  内联 onclick，Electron 下等同 RCE）。

本模块是**无导出**的 IIFE（仅挂 `window.refreshHistory` / `window.goToPage`），
故全部经由**真实入口**驱动，不新增生产代码：
  - `fetch` 桩 → `loadHistory` → `renderJobRow` / `buildMetaLine`
  - `PbcSse.create` 捕获的 `onFrame` → `updateJobRowLive` / `buildRowFromSnapshot`
  - 事件委托（在 `#history-list` 上派发，`target` 指向按钮 —— 假 DOM **不冒泡**）
"""
from __future__ import annotations

import pytest

from tests.js_harness import run_js_async

FILES = ["status.js", "upload-jobs.js"]

# ⚠️ 加载序：DOM_STUB → 本桩 → status.js → upload-jobs.js。
# 本模块在**模块作用域**就 `getElementById` 并调用 `loadHistory(1)`，
# 因此所需元素与全部共享件必须在它之前就位，否则加载期即抛错。
#
# 刻意**不 stub** `PbcStatus` —— 加载真实的 `status.js`（模块的真实依赖，
# 且状态→中文/颜色的单一真值就在那里）。早先 stub 了一份 `statusZh`，结果被
# 真实的 status.js 覆盖，断言却按 stub 的文案写 ⇒ 假失败。断言请一律用
# status.js 的真实值（"识别中" 而非 "OCR 进行中"）。
# 同样**不加载** `eta.js`：模块对 `typeof PbcEta === "undefined"` 有降级分支，
# 不加载即顺带覆盖该降级路径（eta.js 自身行为由 test_eta_js.py 覆盖）。
_STUB = r"""
globalThis.__pending = [];              // 未响应的 fetch（手动 resolve，用于控制到达顺序）
globalThis.__calls = { fetch: [], confirm: [] };
globalThis.__confirmAnswer = true;

globalThis.__sse = { opts: null, closed: 0 };
globalThis.PbcSse = {
  create: (o) => {
    __sse.opts = o;
    return { isClosed: () => false, close: () => { __sse.closed++; } };
  },
};
globalThis.fetch = (url, opts) => {
  __calls.fetch.push({ url, opts });
  return new Promise((resolve) => __pending.push({ url, resolve }));
};
window.PBC = {
  showToast: (m, t) => __toasts.push({ msg: String(m), type: t || "info" }),
  confirmDialog: async (o) => { __calls.confirm.push(o); return __confirmAnswer; },
  promptDialog: async () => null,
};

/* 并发额度注入（真实由 templates/upload.html 在脚本**之前**写入
 * window.__PBC__，本模块在**加载期**读它 ⇒ 必须在文件之前就位）。
 * 缺失时 limit=0 ⇒ 前端视为"未知" ⇒ 跳过预检（fail-open），
 * 那会让下面的额度口径断言失去意义。 */
window.__PBC__ = { concurrency: { limit: 3 } };

/* 模块加载期就会被查询的元素 */
["history-list", "history-pagination", "history-count",
 "archived-list", "archived-count"].forEach((id) => {
  const e = __el(id, id.indexOf("list") >= 0 ? "ul" : "div");
  document.body.appendChild(e);
});
"""

_PRE = r"""
const settle = () => new Promise((r) => setImmediate(r));
const list = () => document.getElementById("history-list");
const rows = () => __queryAll(list(), "li[data-job-id]");
const rowIds = () => rows().map((r) => r.dataset.jobId);
const rowOf = (jid) => document.querySelector('#history-list li[data-job-id="' + jid + '"]');
const textIn = (jid, cls) => {
  const r = rowOf(jid);
  const e = r && r.querySelector(cls);
  return e ? e.textContent : null;
};
const metaOf = (jid) => textIn(jid, ".job-meta");
const btnOf = (jid, action) =>
  rowOf(jid) && rowOf(jid).querySelector('button[data-action="' + action + '"]');

/* 回应第 i 个未完成的 fetch，并排空微任务让 loadHistory 走完。 */
const respondAt = async (i, body, ok = true, status = 200) => {
  const p = __pending[i];
  if (!p) throw new Error("no pending fetch at index " + i);
  p.resolve({ ok: ok, status: status, json: async () => body });
  await settle(); await settle(); await settle();
};
/* 模拟一帧聚合 SSE 快照 */
const frame = (snap) => __sse.opts.onFrame({ jobs: Array.isArray(snap) ? snap : [snap] });
/* 事件委托：假 DOM 不冒泡 ⇒ 必须在容器上派发、并把 target 指向按钮 */
const clickIn = (container, el) => container.dispatchEvent({ type: "click", target: el });
/* 期望的状态点 class —— 由**真实** PbcStatus 推出，不硬编码调色板
 * （否则 status.js 改配色就会假失败，而那不是本模块的契约）。 */
const expectDotCls = (st) =>
  "w-1.5 h-1.5 rounded-full " + window.PbcStatus.statusDotClass(st) + " status-dot";

const JOB = (over) => Object.assign({
  id: "J1", filename: "a.pdf", created_at: "2026-01-01 10:00",
  status: "review", total_pages: 5, failed_pages: [], pages_analyzed: 0,
}, over || {});
"""


def _probe(body: str):
    return run_js_async(FILES, _PRE + "\n" + body, stub=_STUB)


# 加载期 loadHistory(1) 已经发出第 0 个 fetch —— 每个用例都要先把它了结。
_BOOT = """
await respondAt(0, { jobs: [], total_pages: 1, total_jobs: 0 });
"""


class TestHarnessIsNotVacuous:
    """先证明驱动链真的通了，否则下面全是空断言。"""

    def test_module_loaded_and_sse_captured(self):
        # 注意：`loadHistory` 在 jobs 为空时**提前 return**，不会调 startLiveTracking，
        # 故这里必须回一个 job，否则 __sse.opts 恒为 null（本用例会误报）。
        body = """
await respondAt(0, { jobs: [JOB()], total_pages: 1, total_jobs: 1 });
console.log(JSON.stringify({
  goToPage: typeof window.goToPage,
  refreshHistory: typeof window.refreshHistory,
  sseCaptured: !!(__sse.opts && typeof __sse.opts.onFrame === "function"),
}));
"""
        assert _probe(body) == {
            "goToPage": "function", "refreshHistory": "function", "sseCaptured": True,
        }

    def test_empty_list_does_not_start_live_tracking(self):
        """反向钉住上面那条注释所依赖的行为：空列表不应建立 SSE 订阅
        （否则空历史页会挂一条无意义的实时连接）。"""
        body = _BOOT + """
console.log(JSON.stringify({ sseCaptured: __sse.opts !== null }));
"""
        assert _probe(body) == {"sseCaptured": False}

    def test_rows_render_and_are_queryable(self):
        body = """
await respondAt(0, { jobs: [JOB()], total_pages: 1, total_jobs: 1 });
console.log(JSON.stringify({ ids: rowIds(), meta: metaOf("J1") }));
"""
        got = _probe(body)
        assert got["ids"] == ["J1"], "行渲染了但选择器查不到 —— 护栏会退化成空断言"
        assert got["meta"] == "J1 · 2026-01-01 10:00"


# ─────────────────────────────────────────────────────────────────────
# #134：摘要行 —— 静态渲染路径
# ─────────────────────────────────────────────────────────────────────
class TestMetaLineStaticPath:
    def test_failed_pages_are_listed(self):
        body = """
await respondAt(0, { jobs: [JOB({ status: "partial_review", failed_pages: [1, 2, 3] })],
                     total_pages: 1, total_jobs: 1 });
console.log(JSON.stringify({ meta: metaOf("J1") }));
"""
        assert _probe(body)["meta"] == "J1 · 2026-01-01 10:00 · 失败页 3 页（1,2,3）"

    def test_more_than_twelve_pages_are_elided(self):
        """>12 页时只列前 12 个并加省略号 —— 否则摘要行会被撑爆。"""
        body = """
await respondAt(0, { jobs: [JOB({ failed_pages: [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15] })],
                     total_pages: 1, total_jobs: 1 });
console.log(JSON.stringify({ meta: metaOf("J1") }));
"""
        meta = _probe(body)["meta"]
        assert meta.endswith("失败页 15 页（1,2,3,4,5,6,7,8,9,10,11,12…）"), meta

    def test_absent_failed_pages_adds_no_suffix(self):
        body = """
await respondAt(0, { jobs: [JOB({ failed_pages: [] })], total_pages: 1, total_jobs: 1 });
console.log(JSON.stringify({ meta: metaOf("J1") }));
"""
        assert _probe(body)["meta"] == "J1 · 2026-01-01 10:00"

    def test_non_array_failed_pages_is_not_treated_as_array(self):
        """接口契约是数组。若某天退化成字符串 `"1,2"`，**不得**当成两页渲染
        （`Array.isArray` 守卫）—— 否则长度会算成字符数，显示"失败页 3 页"。"""
        body = """
await respondAt(0, { jobs: [JOB({ failed_pages: "1,2" })], total_pages: 1, total_jobs: 1 });
console.log(JSON.stringify({ meta: metaOf("J1") }));
"""
        meta = _probe(body)["meta"]
        assert "失败页" not in meta, f"字符串被当数组用了：{meta}"


# ─────────────────────────────────────────────────────────────────────
# 列表状态：空 / 失败 / 计数 / 分页
# ─────────────────────────────────────────────────────────────────────
class TestListStates:
    def test_empty_state(self):
        body = _BOOT + """
console.log(JSON.stringify({ html: list().innerHTML.includes("暂无历史记录") }));
"""
        assert _probe(body) == {"html": True}

    def test_load_failure_is_surfaced(self):
        body = """
await respondAt(0, {}, false, 500);
console.log(JSON.stringify({ html: list().innerHTML.includes("加载失败") }));
"""
        assert _probe(body) == {"html": True}

    def test_count_reflects_total_jobs(self):
        body = """
await respondAt(0, { jobs: [JOB()], total_pages: 1, total_jobs: 7 });
console.log(JSON.stringify({ count: document.getElementById("history-count").textContent }));
"""
        assert _probe(body) == {"count": "7 份"}

    def test_pagination_hidden_for_single_page(self):
        body = """
await respondAt(0, { jobs: [JOB()], total_pages: 1, total_jobs: 1 });
console.log(JSON.stringify({
  hidden: document.getElementById("history-pagination").classList.contains("hidden") }));
"""
        assert _probe(body) == {"hidden": True}

    def test_pagination_rendered_for_multi_page(self):
        body = """
await respondAt(0, { jobs: [JOB()], total_pages: 3, total_jobs: 42 });
const p = document.getElementById("history-pagination");
console.log(JSON.stringify({ hidden: p.classList.contains("hidden"),
                             hasText: p.innerHTML.includes("第 1 页 / 共 3 页 · 42 份") }));
"""
        assert _probe(body) == {"hidden": False, "hasText": True}


class TestStaleResponseIsDropped:
    """翻页竞态：先发的 page 2 若后到，**不得**覆盖已渲染的 page 3。"""

    def test_late_response_does_not_overwrite(self):
        body = """
await respondAt(0, { jobs: [JOB({ id: "P1" })], total_pages: 5, total_jobs: 10 });
window.goToPage(2);            // -> __pending[1]
window.goToPage(3);            // -> __pending[2]
await respondAt(2, { jobs: [JOB({ id: "P3" })], total_pages: 5, total_jobs: 10 });
await respondAt(1, { jobs: [JOB({ id: "P2" })], total_pages: 5, total_jobs: 10 });
console.log(JSON.stringify({ ids: rowIds(), fetches: __calls.fetch.map((f) => f.url) }));
"""
        got = _probe(body)
        assert got["ids"] == ["P3"], f"过期响应覆盖了新页：{got['ids']}"
        assert got["fetches"][-2:] == ["/api/jobs?page=2&page_size=20",
                                       "/api/jobs?page=3&page_size=20"]

    def test_goToPage_ignores_out_of_range_and_same_page(self):
        body = """
await respondAt(0, { jobs: [JOB({ id: "P1" })], total_pages: 3, total_jobs: 3 });
const before = __calls.fetch.length;
window.goToPage(0);
window.goToPage(9);
window.goToPage(1);
console.log(JSON.stringify({ extra: __calls.fetch.length - before }));
"""
        assert _probe(body) == {"extra": 0}


# ─────────────────────────────────────────────────────────────────────
# #134（P0）：实时帧也必须刷新摘要行
# ─────────────────────────────────────────────────────────────────────
class TestLiveFrameUpdatesMeta:
    def test_failed_pages_appear_live_and_created_at_survives(self):
        """**本文件最重要的一条**：静态行先渲染（无失败页），随后 SSE 帧带来
        失败页 —— 摘要行必须出现失败页数，且**保留** created_at
        （快照不带 created_at，若整行重写就会丢掉时间）。"""
        body = """
await respondAt(0, { jobs: [JOB({ status: "analyzing" })], total_pages: 1, total_jobs: 1 });
const before = metaOf("J1");
frame({ id: "J1", status: "analyzing", total_pages: 5, pages_analyzed: 2, failed_pages: [4, 5] });
console.log(JSON.stringify({ before: before, after: metaOf("J1") }));
"""
        got = _probe(body)
        assert got["before"] == "J1 · 2026-01-01 10:00"
        assert got["after"] == "J1 · 2026-01-01 10:00 · 失败页 2 页（4,5）", (
            f"实时帧未把失败页数写进摘要行（#134 回归）：{got['after']}"
        )

    def test_snapshot_without_failed_pages_does_not_clobber_created_at(self):
        """快照**没有** failed_pages 时不得改写 meta —— 否则 created_at 被清空。"""
        body = """
await respondAt(0, { jobs: [JOB({ status: "analyzing" })], total_pages: 1, total_jobs: 1 });
frame({ id: "J1", status: "analyzing", total_pages: 5, pages_analyzed: 3 });
console.log(JSON.stringify({ meta: metaOf("J1") }));
"""
        assert _probe(body)["meta"] == "J1 · 2026-01-01 10:00"

    def test_second_live_frame_does_not_accumulate_spaces(self):
        """FIX-8 回归门：meta 已含「· 失败页」段之后，取回 created_at 必须 trim。

        缺陷形态：`split("· ")` 取回的那一段带**尾随空格**，重建行时拼成
        `10:00  · 失败页`（双空格），且每重建一次再累积一个。

        ⚠️ 只跑**一帧**测不出来 —— 首帧的 meta 末尾没有别的段，`parts[1]`
        本来就没有尾随空格（变异实测：去掉 `.trim()` 仍全绿）。必须跑两帧。
        """
        body = """
await respondAt(0, { jobs: [JOB({ status: "analyzing" })], total_pages: 1, total_jobs: 1 });
frame({ id: "J1", status: "analyzing", total_pages: 5, pages_analyzed: 2, failed_pages: [4, 5] });
const first = metaOf("J1");
frame({ id: "J1", status: "analyzing", total_pages: 5, pages_analyzed: 3, failed_pages: [4, 5, 6] });
const second = metaOf("J1");
console.log(JSON.stringify({ first: first, second: second }));
"""
        got = _probe(body)
        assert got["first"] == "J1 · 2026-01-01 10:00 · 失败页 2 页（4,5）"
        assert got["second"] == "J1 · 2026-01-01 10:00 · 失败页 3 页（4,5,6）", (
            f"created_at 未 trim ⇒ 双空格累积（FIX-8 回归）：{got['second']!r}"
        )
        assert "  " not in got["second"], "摘要行不得出现双空格"

    def test_status_dot_and_text_follow_the_frame(self):
        body = """
await respondAt(0, { jobs: [JOB({ status: "analyzing" })], total_pages: 1, total_jobs: 1 });
frame({ id: "J1", status: "ocr_running", total_pages: 5, ocr_progress: { done: 3, total: 9 } });
const dot = rowOf("J1").querySelector(".status-dot");
console.log(JSON.stringify({ text: textIn("J1", ".status-text"),
                             dot: dot.className, expected: expectDotCls("ocr_running"),
                             status: rowOf("J1").dataset.status,
                             pages: textIn("J1", ".job-pages") }));
"""
        got = _probe(body)
        assert got["text"] == "识别中"
        assert got["dot"] == got["expected"], got["dot"]
        assert got["status"] == "ocr_running"
        assert got["pages"] == "OCR 3/9"


class TestStatusDotSurvivesLiveUpdates:
    """**回归门（本轮修复的真实缺陷）**：`updateJobRowLive` 曾用
    `` `w-1.5 h-1.5 rounded-full ${statusDotClass(st)}` `` 整体重写 `className`，
    **漏掉了 `.status-dot` 定位标记**。于是：

      帧 1 之后 `li.querySelector(".status-dot")` 返回 null ⇒ 圆点颜色再也
      不更新，**冻结在首帧状态**；而 `.status-text` 仍在正常变化。

    结果是文案与颜色互相矛盾（例如红点旁边写着"可复核"），对复核者是主动误导。
    这条护栏连打两帧，专门盯住"第二帧还能找到圆点且颜色已更新"。
    """

    def test_dot_is_still_locatable_and_recoloured_on_the_second_frame(self):
        body = """
await respondAt(0, { jobs: [JOB({ status: "analyzing" })], total_pages: 1, total_jobs: 1 });
frame({ id: "J1", status: "ocr_running", total_pages: 5, ocr_progress: { done: 3, total: 9 } });
const first = rowOf("J1").querySelector(".status-dot");
const firstCls = first ? first.className : null;
frame({ id: "J1", status: "ocr_done", total_pages: 5, ocr_progress: { done: 9, total: 9 } });
const second = rowOf("J1").querySelector(".status-dot");
console.log(JSON.stringify({
  firstCls: firstCls,
  secondFound: !!second,
  secondCls: second ? second.className : null,
  expected: expectDotCls("ocr_done"),
  text: textIn("J1", ".status-text"),
}));
"""
        got = _probe(body)
        assert got["firstCls"] == expect_dot("ocr_running"), "首帧 class 不符"
        assert got["secondFound"] is True, (
            "第二帧找不到 .status-dot —— 圆点颜色将冻结在首帧状态"
            "（updateJobRowLive 重写 className 时漏掉定位标记）"
        )
        assert got["secondCls"] == got["expected"], got["secondCls"]
        assert got["text"] == "识别完成"

    def test_dot_class_has_no_stale_status_colour(self):
        """颜色 class 必须是**唯一**的 —— 若改成 add 不 remove，圆点会同时带
        两个 bg-* 类，实际显色取决于 CSS 顺序（不确定）。"""
        body = """
await respondAt(0, { jobs: [JOB({ status: "analyzing" })], total_pages: 1, total_jobs: 1 });
frame({ id: "J1", status: "error", total_pages: 5 });
frame({ id: "J1", status: "ocr_running", total_pages: 5, ocr_progress: { done: 1, total: 4 } });
const dot = rowOf("J1").querySelector(".status-dot");
console.log(JSON.stringify({ cls: dot.className, expected: expectDotCls("ocr_running"),
                             destructive: window.PbcStatus.statusDotClass("error") }));
"""
        got = _probe(body)
        assert got["cls"] == got["expected"], f"圆点 class 不符（可能残留旧颜色）：{got['cls']}"
        assert got["destructive"] not in got["cls"], "残留了上一帧的破坏色"


def expect_dot(st: str) -> str:
    """Python 侧无法调用 PbcStatus，故按 status.js 的真实映射推导。

    与 static/status.js 的 `statusDotClass` 保持逐值一致；`test_status_js.py`
    已锁定该函数本身，这里只需跟着走。
    """
    if st in ("review", "done"):
        return "w-1.5 h-1.5 rounded-full bg-success status-dot"
    if st == "partial_review":
        return "w-1.5 h-1.5 rounded-full bg-warning status-dot"
    if st == "error":
        return "w-1.5 h-1.5 rounded-full bg-destructive status-dot"
    if st in ("cancelled", "cancelling", "archived"):
        return "w-1.5 h-1.5 rounded-full bg-muted-foreground/40 status-dot"
    return "w-1.5 h-1.5 rounded-full bg-info status-dot"

    def test_unknown_job_id_is_ignored(self):
        """帧里的 job 不在当前页 ⇒ 静默跳过，不得抛错。"""
        body = """
await respondAt(0, { jobs: [JOB()], total_pages: 1, total_jobs: 1 });
frame({ id: "NOT-ON-THIS-PAGE", status: "error", error_message: "x" });
console.log(JSON.stringify({ ids: rowIds(), meta: metaOf("J1") }));
"""
        got = _probe(body)
        assert got["ids"] == ["J1"]
        assert got["meta"] == "J1 · 2026-01-01 10:00"


# ─────────────────────────────────────────────────────────────────────
# #127：失败原因行内可见
# ─────────────────────────────────────────────────────────────────────
class TestErrorRowVisibility:
    def _state(self, snap: str) -> dict:
        body = f"""
await respondAt(0, {{ jobs: [JOB({{ status: "analyzing" }})], total_pages: 1, total_jobs: 1 }});
{snap}
const e = rowOf("J1").querySelector(".job-error");
console.log(JSON.stringify({{ hidden: e.classList.contains("hidden"), text: e.textContent }}));
"""
        return _probe(body)

    def test_error_status_shows_reason(self):
        got = self._state('frame({ id: "J1", status: "error", error_message: "模型凭据失效" });')
        assert got == {"hidden": False, "text": "模型凭据失效"}

    def test_partial_review_shows_reason(self):
        """#127：partial_review 同样要显示 —— 这是"0 条 finding 到底怎么解读"的关键。"""
        got = self._state(
            'frame({ id: "J1", status: "partial_review", error_message: "第 3 页失败" });')
        assert got == {"hidden": False, "text": "第 3 页失败"}

    def test_running_status_hides_the_row(self):
        got = self._state('frame({ id: "J1", status: "analyzing", error_message: "旧的错误" });')
        assert got["hidden"] is True, "运行中不应残留上一轮的错误文案"

    def test_error_without_message_stays_hidden(self):
        got = self._state('frame({ id: "J1", status: "error" });')
        assert got["hidden"] is True

    def test_ocr_backend_tag_is_shown_and_hidden(self):
        body = """
await respondAt(0, { jobs: [JOB({ status: "analyzing" })], total_pages: 1, total_jobs: 1 });
frame({ id: "J1", status: "analyzing", ocr_backend_used: "mineru", ocr_backend_display: "MinerU" });
const on = rowOf("J1").querySelector(".job-ocr-backend");
const shown = { hidden: on.classList.contains("hidden"), text: on.textContent };
frame({ id: "J1", status: "analyzing" });
const off = rowOf("J1").querySelector(".job-ocr-backend");
console.log(JSON.stringify({ shown: shown, offHidden: off.classList.contains("hidden") }));
"""
        got = _probe(body)
        assert got["shown"] == {"hidden": False, "text": "OCR: MinerU"}
        assert got["offHidden"] is True


# ─────────────────────────────────────────────────────────────────────
# 终态：用快照重建行
# ─────────────────────────────────────────────────────────────────────
class TestTerminalFrameRebuildsRow:
    def test_row_is_replaced_and_keeps_filename_and_failed_pages(self):
        body = """
await respondAt(0, { jobs: [JOB({ status: "analyzing", filename: "批记录.pdf" })],
                     total_pages: 1, total_jobs: 1 });
const before = rowOf("J1");
frame({ id: "J1", status: "review", total_pages: 5, failed_pages: [2, 7] });
const after = rowOf("J1");
console.log(JSON.stringify({
  replaced: before !== after,
  status: after.dataset.status,
  filename: after.dataset.filename,
  meta: metaOf("J1"),
}));
"""
        got = _probe(body)
        assert got["replaced"] is True, "终态未重建行（按钮可用状态不会刷新）"
        assert got["status"] == "review"
        assert got["filename"] == "批记录.pdf", "重建时丢了文件名"
        assert got["meta"] == "J1 · 2026-01-01 10:00 · 失败页 2 页（2,7）"

    def test_rebuild_enables_actions_for_terminal_status(self):
        """运行中行两个按钮都禁用；转终态重建后必须变为可用 ——
        否则用户看到终态却点不动归档/删除。"""
        body = """
await respondAt(0, { jobs: [JOB({ status: "analyzing" })], total_pages: 1, total_jobs: 1 });
const before = { archive: btnOf("J1", "archive").disabled, del: btnOf("J1", "delete").disabled };
frame({ id: "J1", status: "review", total_pages: 5 });
const after = { archive: btnOf("J1", "archive").disabled, del: btnOf("J1", "delete").disabled };
console.log(JSON.stringify({ before: before, after: after }));
"""
        assert _probe(body) == {
            "before": {"archive": True, "del": True},
            "after": {"archive": False, "del": False},
        }


# ─────────────────────────────────────────────────────────────────────
# 行渲染：按钮门控 / XSS
# ─────────────────────────────────────────────────────────────────────
class TestRowActionGating:
    @staticmethod
    def _render(status: str) -> dict:
        body = f"""
await respondAt(0, {{ jobs: [JOB({{ status: "{status}" }})], total_pages: 1, total_jobs: 1 }});
const a = btnOf("J1", "archive");
const d = btnOf("J1", "delete");
console.log(JSON.stringify({{ archive: a.disabled, delete: d.disabled, deleteTitle: d.title }}));
"""
        return _probe(body)

    def test_review_allows_both(self):
        got = self._render("review")
        assert got["archive"] is False and got["delete"] is False

    def test_error_allows_both(self):
        got = self._render("error")
        assert got["archive"] is False and got["delete"] is False

    def test_done_allows_archive(self):
        got = self._render("done")
        assert got["archive"] is False, "done 在 canArchive 白名单里"

    def test_active_status_blocks_delete_with_actionable_title(self):
        """运行中禁用删除（防孤儿 pipeline task），且 tooltip 要给出**出路**
        （先取消、等终态），不能只说"不能删"。"""
        got = self._render("analyzing")
        assert got["archive"] is True and got["delete"] is True
        assert "取消" in got["deleteTitle"] and "终态" in got["deleteTitle"], got["deleteTitle"]

    def test_archived_status_is_not_archivable(self):
        got = self._render("archived")
        assert got["archive"] is True, "已归档不应再出现「归档」可用按钮"


class TestFilenameXssIsNeutralised:
    def test_markup_in_filename_stays_text(self):
        body = """
const evil = '<img src=x onerror=alert(1)>.pdf';
await respondAt(0, { jobs: [JOB({ filename: evil })], total_pages: 1, total_jobs: 1 });
const row = rowOf("J1");
const title = row.querySelector(".text-\\\\[13px\\\\]") || row.querySelector("div");
console.log(JSON.stringify({
  imgTags: __queryAll(row, "img").length,
  liHtml: row.innerHTML,
  filenameAttr: row.dataset.filename,
}));
"""
        got = _probe(body)
        assert got["imgTags"] == 0, "文件名里的 HTML 被解析成了元素 —— XSS"
        assert "<img" not in got["liHtml"]
        assert got["filenameAttr"] == "<img src=x onerror=alert(1)>.pdf"


# ─────────────────────────────────────────────────────────────────────
# 归档 / 删除（破坏性操作）—— 事件委托 + 确认闸门
# ─────────────────────────────────────────────────────────────────────
class TestDeleteFlow:
    def _click_delete(self, answer: str) -> str:
        return f"""
__confirmAnswer = {answer};
await respondAt(0, {{ jobs: [JOB({{ status: "review" }})], total_pages: 1, total_jobs: 1 }});
clickIn(list(), btnOf("J1", "delete"));
await settle(); await settle(); await settle();
"""

    def test_declining_confirmation_sends_no_request(self):
        """用户点"取消" ⇒ **一个请求都不发**（误触即永久删除 PDF + 审计日志）。"""
        body = self._click_delete("false") + """
console.log(JSON.stringify({
  confirm: __calls.confirm.map((c) => ({ danger: c.danger, title: c.title })),
  fetches: __calls.fetch.map((f) => f.url),
  rows: rowIds(),
}));
"""
        got = _probe(body)
        assert got["confirm"] == [{"danger": True, "title": '彻底删除 "a.pdf"？'}]
        assert got["fetches"] == ["/api/jobs?page=1&page_size=20"], "取消后仍发了请求"
        assert got["rows"] == ["J1"]

    def test_confirming_deletes_row_and_decrements_count(self):
        body = self._click_delete("true") + """
await respondAt(1, {});
__flushTimers(); __flushTimers();
await settle();
console.log(JSON.stringify({
  url: __calls.fetch[1].url,
  method: __calls.fetch[1].opts.method,
  toast: __toasts.map((t) => [t.msg, t.type]),
  rows: rowIds(),
  count: document.getElementById("history-count").textContent,
}));
"""
        got = _probe(body)
        assert got["url"] == "/api/jobs/J1?keep_pdf=false"
        assert got["method"] == "DELETE"
        assert got["toast"] == [["已删除 a.pdf", "ok"]]
        assert got["rows"] == [], "行未从列表移除"
        assert got["count"] == "0 份", f"计数未递减：{got['count']}"

    def test_server_detail_is_surfaced_on_failure(self):
        """409 等失败必须把后端 detail 透出（含可操作引导），不能只显示 HTTP 码。"""
        body = self._click_delete("true") + """
await respondAt(1, { detail: "任务运行中，请先取消" }, false, 409);
console.log(JSON.stringify({ toast: __toasts.map((t) => [t.msg, t.type]), rows: rowIds() }));
"""
        got = _probe(body)
        assert got["toast"] == [["删除失败: 任务运行中，请先取消", "err"]], got["toast"]
        assert got["rows"] == ["J1"], "删除失败却把行移除了"


class TestArchiveFlow:
    def test_archive_posts_and_fades_row(self):
        body = """
await respondAt(0, { jobs: [JOB({ status: "review" })], total_pages: 1, total_jobs: 1 });
clickIn(list(), btnOf("J1", "archive"));
await settle(); await settle(); await settle();
await respondAt(1, {});              // POST /archive
__flushTimers(); __flushTimers();
await settle();
console.log(JSON.stringify({
  url: __calls.fetch[1].url,
  method: __calls.fetch[1].opts.method,
  toast: __toasts.map((t) => [t.msg, t.type]),
  rows: rowIds(),
}));
"""
        got = _probe(body)
        assert got["url"] == "/api/jobs/J1/archive"
        assert got["method"] == "POST"
        assert got["toast"] == [["已归档 a.pdf", "ok"]]
        assert got["rows"] == []

    def test_declining_archive_sends_no_request(self):
        body = """
__confirmAnswer = false;
await respondAt(0, { jobs: [JOB({ status: "review" })], total_pages: 1, total_jobs: 1 });
clickIn(list(), btnOf("J1", "archive"));
await settle(); await settle(); await settle();
console.log(JSON.stringify({ fetches: __calls.fetch.length, rows: rowIds() }));
"""
        assert _probe(body) == {"fetches": 1, "rows": ["J1"]}


# ─────────────────────────────────────────────────────────────────────
# 降级轮询（SSE 失联后）
# ─────────────────────────────────────────────────────────────────────
class TestFallbackPolling:
    def test_polls_active_rows_and_stops_on_terminal(self):
        """降级轮询必须**只跟活跃行**，且终态后停止（否则永久泄漏定时器）。"""
        body = """
await respondAt(0, { jobs: [JOB({ id: "A", status: "analyzing" }),
                            JOB({ id: "B", status: "review" })],
                     total_pages: 1, total_jobs: 2 });
__sse.opts.fallback();               // 触发降级
const afterStart = __timers.filter((t) => t.kind === "interval" && t.ms === 10000).length;
__tickInterval(10000);               // 轮询一次 -> 发出 GET /api/jobs/A
await settle();
await respondAt(__pending.length - 1, { id: "A", status: "review", total_pages: 5 });
const afterDone = __timers.filter(
  (t) => t.kind === "interval" && t.ms === 10000 && !t.cleared).length;
console.log(JSON.stringify({ afterStart: afterStart, afterDone: afterDone,
                             polled: __calls.fetch[__calls.fetch.length - 1].url,
                             status: rowOf("A").dataset.status }));
"""
        got = _probe(body)
        assert got["afterStart"] == 1, "应为唯一的活跃行开一个轮询定时器"
        assert got["polled"] == "/api/jobs/A"
        assert got["status"] == "review", "终态后应重建行"
        assert got["afterDone"] == 0, "终态后轮询未停止 —— 定时器泄漏"

    def test_poll_stops_when_row_disappears(self):
        body = """
await respondAt(0, { jobs: [JOB({ id: "A", status: "analyzing" })], total_pages: 1, total_jobs: 1 });
__sse.opts.fallback();
rowOf("A").remove();                 // 行被归档/删除
__tickInterval(10000);
await settle();
const alive = __timers.filter(
  (t) => t.kind === "interval" && t.ms === 10000 && !t.cleared).length;
console.log(JSON.stringify({ alive: alive }));
"""
        assert _probe(body) == {"alive": 0}


class TestQuotaPublisher:
    """额度活跃数的**口径**：只能数 `ACTIVE_STATUSES`。

    **为什么必须是行为断言**：`ACTIVE_STATUSES.includes(...)` 在本模块出现 3 次
    （额度口径 / 单任务轮询守卫 / 删除按钮守卫），所以"源码里有这句话"这种断言在
    **口径被写坏之后依然成立** —— 本仓库的变异验证实测为 MISSED（假绿）。
    这里断言 `window.PbcJobs.quota()` 这个**可观测输出**。

    口径为什么是"只数活跃"：`/api/jobs/live` 的推送体**还含近 10 分钟内的终态
    job**（行状态切换需要），直接 `jobs.length` 会**高估** ⇒ 明明有空位却让用户
    干等（界面显示"已达并发上限"而实际没满）。
    """

    def test_quota_active_counts_only_active_statuses(self):
        got = _probe(
            # ⚠️ 不能沿用 `_BOOT`：它加载的是**空列表**，而本模块的既有契约是
            # "空列表不建 SSE 订阅"（见 test_empty_list_does_not_start_live_tracking），
            # 于是 `__sse.opts` 仍是 null，`frame()` 会直接抛。故先加载一行**终态**
            # job（终态不会开轮询）把 SSE 句柄拿到手。
            """
await respondAt(0, { jobs: [JOB({ id: "SEED", status: "done" })],
                    total_pages: 1, total_jobs: 1 });
frame([
  JOB({ id: "A", status: "analyzing" }),   // 活跃
  JOB({ id: "B", status: "pending" }),     // 活跃
  JOB({ id: "C", status: "ocr_done" }),    // 活跃
  JOB({ id: "D", status: "review" }),      // 终态（等人工复核）
  JOB({ id: "E", status: "done" }),        // 终态
  JOB({ id: "F", status: "failed" }),      // 终态
  JOB({ id: "G", status: "archived" }),    // 终态
]);
const q = window.PbcJobs.quota();
console.log(JSON.stringify({
  active: q.active, limit: q.limit,
  hasFn: typeof window.PbcJobs.quota === "function",
}));
"""
        )
        assert got["hasFn"] is True, "quota() 没发布 —— 本用例会退化成空断言"
        assert got["limit"] == 3, (
            f"limit 未从 window.__PBC__.concurrency 读到（{got['limit']}）—— "
            f"前端拿不到额度 ⇒ 发送前预检恒放行"
        )
        assert got["active"] == 3, (
            f"active 应为 3（analyzing + pending + ocr_done），实际 {got['active']} —— "
            f"终态 job 被算成占用额度 ⇒ 明明有空位却让用户干等"
        )

    def test_quota_active_drops_when_a_job_reaches_a_terminal_status(self):
        """同一 job 由活跃转终态 ⇒ active 必须**回落**（否则"满额"永不解除）。"""
        got = _probe(
            """
await respondAt(0, { jobs: [JOB({ id: "SEED", status: "done" })],
                    total_pages: 1, total_jobs: 1 });
frame([JOB({ id: "A", status: "analyzing" })]);
const during = window.PbcJobs.quota().active;
frame([JOB({ id: "A", status: "done" })]);
const after = window.PbcJobs.quota().active;
console.log(JSON.stringify({ during: during, after: after }));
"""
        )
        assert got["during"] == 1, f"活跃期 active 应为 1，实际 {got['during']}"
        assert got["after"] == 0, (
            f"job 终态后 active 没回落（{got['after']}）—— 额度永久冻结"
        )
