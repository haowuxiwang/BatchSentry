"""`window.PBC` 降级兜底（P1-9）的护栏。

**为什么必须测**：`confirm-dialog.js` 提供 `window.PBC.{showToast, confirmDialog,
promptDialog}`，全站 10 个模块直接消费。若它未能加载（网络 / 缓存 / CSP 异常、
被扩展拦截），调用点抛 TypeError；而这些调用多在 async 回调与事件处理器里，
异常逃出 Promise 边界后**无人 catch** ⇒ "点了删除 / 取消 / 重试没反应"：
没有请求、没有报错、没有提示。

`pbc-fallback.js` 只补缺失方法、不覆盖已有实现，由此得到两个必须被钉住的
性质（也是本文件的断言核心）：

1. **加载顺序无关** —— 谁先执行，最终生效的都是真实实现；只有 confirm-dialog.js
   缺席时降级版才留下。（confirm-dialog.js 结尾是无条件赋值。）
2. **两条降级不变量** —— 不可逆操作仍必须被确认（问不到就否决）；失败必须可见。

驱动方式全部经**真实入口**（`deleteJob` 走事件委托、`cancelJob` 是导出函数），
未新增生产代码。
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.js_harness import run_js_async

REPO = Path(__file__).resolve().parents[2]
SHIM = "pbc-fallback.js"

UPLOAD_FILES = [SHIM, "status.js", "upload-jobs.js"]
# review.js 的**模块作用域**只依赖 `R.progress.safeAutoReload`（见其"挂载跨模块
# 入口"段），故只需 review-state.js + review-progress.js。
REVIEW_FILES = [SHIM, "review-state.js", "review-progress.js", "review.js"]

# ── 页面骨架 ──────────────────────────────────────────────────────────
_SKELETON_UPLOAD = r"""
["history-list", "history-pagination", "history-count",
 "archived-list", "archived-count"].forEach((id) => {
  const e = __el(id, id.indexOf("list") >= 0 ? "ul" : "div");
  document.body.appendChild(e);
});
"""

# ── 桩：模拟"confirm-dialog.js 未加载"（DOM_STUB 自带一个 PBC，必须先删） ──
_NO_PBC = r"""
delete globalThis.PBC;

globalThis.__calls = { confirm: [], alert: [], warn: [], prompt: [], fetch: [] };
globalThis.__confirmAnswer = true;
globalThis.__promptAnswer = null;
globalThis.__failWrite = false;
globalThis.__toasts = [];

window.confirm = (msg) => { __calls.confirm.push(String(msg)); return __confirmAnswer; };
window.alert = (msg) => { __calls.alert.push(String(msg)); };
window.prompt = (msg, def) => { __calls.prompt.push([String(msg), def]); return __promptAnswer; };
console.warn = (...a) => { __calls.warn.push(a.map(String).join(" ")); };

globalThis.location = { reload: () => { __calls.reload = (__calls.reload || 0) + 1; } };

globalThis.fetch = async (url, opts) => {
  const method = (opts && opts.method) || "GET";
  __calls.fetch.push({ url: String(url), method });
  if (method !== "GET" && __failWrite) {
    // 两个模块读失败原因的方式不同，一个响应体同时满足：
    //   upload-jobs 看 `r.ok` → 再取 body.detail；review 看 body.ok → 再取 body.message
    return { ok: false, status: 500,
             json: async () => ({ ok: false, detail: "boom", message: "boom" }) };
  }
  if (String(url).indexOf("/api/jobs/archived") === 0) {
    return { ok: true, status: 200, json: async () => ({ count: 0, archived: [] }) };
  }
  return { ok: true, status: 200, json: async () => ({
    total_pages: 1, total_jobs: 0, jobs: [], ok: true,
  }) };
};

globalThis.__writes = () => __calls.fetch.filter((c) => c.method !== "GET");

/* 降级路径**不得**产生 unhandled rejection —— 这正是"点了没反应"的机理。
 * 用监听器把它变成**可断言**的信号，而不是让 node 直接崩（崩了也能红，
 * 但红在 returncode 上，说不清是哪条不变量破了）。
 * ⚠️ 注册监听器会**吞掉** node 默认的崩溃行为；因此必须同时 console.error，
 * 否则探针里的 TypeError 会表现为"rc=0 且 stdout 为空"（IndexError），
 * 极难定位。 */
globalThis.__unhandled = [];
process.on("unhandledRejection", (e) => {
  const msg = String((e && e.message) || e);
  __unhandled.push(msg);
  console.error("[probe] unhandledRejection:", msg);
});
"""

_STUB_UPLOAD = _SKELETON_UPLOAD + _NO_PBC
_STUB_REVIEW = "globalThis.__PBC__ = { job_id: 'J1' };\n" + _NO_PBC
_PRE = r"""
const settle = () => new Promise((r) => setImmediate(r));
const drain = async (n) => { for (let i = 0; i < (n || 6); i++) await settle(); };
const listEl = () => document.getElementById("history-list");
const clickIn = (container, el) => container.dispatchEvent({ type: "click", target: el });

const mkDeleteBtn = (jid, st) => {
  const li = __el("row-" + jid, "li");
  li.dataset.jobId = jid;
  li.dataset.filename = jid + ".pdf";
  li.dataset.status = st || "error";
  const btn = document.createElement("button");
  btn.dataset.action = "delete";
  li.appendChild(btn);
  listEl().appendChild(li);
  return btn;
};
const askDelete = async (jid) => {
  clickIn(listEl(), mkDeleteBtn(jid));
  await drain();
};
const askCancel = async () => {
  let threw = null;
  try { await window.cancelJob({ currentTarget: null }); }
  catch (e) { threw = String((e && e.message) || e); }
  await drain();
  return threw;
};

/* ⚠️ 必须先排空模块**加载期**发起的那次 loadHistory(1)：它的 `log()` 会
 * 落在探针输出**之后**，把 `run_js` 取"最后一行当 JSON"的约定打乱。 */
await drain();
"""


def _upload(body: str):
    return run_js_async(UPLOAD_FILES, _PRE + "\n" + body, stub=_STUB_UPLOAD)


def _review(body: str):
    return run_js_async(REVIEW_FILES, _PRE + "\n" + body, stub=_STUB_REVIEW)


def _shim(body: str):
    """只加载 shim（模拟 confirm-dialog.js 缺席）。

    ⚠️ `_NO_PBC` 必须走 **stub** 位（在 shim 之前执行）。若把它塞进探针，
    shim 就会先看到 DOM_STUB 自带的那套 PBC 并跳过安装，随后探针再把 PBC
    删掉 —— 于是断言看到的是"什么都没装"。更阴的是：探针里访问
    `window.PBC.showToast` 抛的 TypeError 会被 `_NO_PBC` 注册的
    `unhandledRejection` 处理器**吞掉**（node 不再崩、stdout 为空），
    表现为 `IndexError: list index out of range` 而不是一条清晰的报错。
    """
    return run_js_async([SHIM], _PRE + "\n" + body, stub=_NO_PBC)


# ══════════════════════════════════════════════════════════════════════
class TestHarnessIsNotVacuous:
    """先证明驱动链真的通、且桩真的没装 PBC —— 否则下面全是空断言。"""

    def test_shim_loads_without_pbc(self):
        assert _shim("""
console.log(JSON.stringify({
  pbc: typeof window.PBC,
  toast: typeof window.PBC.showToast,
  confirm: typeof window.PBC.confirmDialog,
  prompt: typeof window.PBC.promptDialog,
}));
""") == {"pbc": "object", "toast": "function",
         "confirm": "function", "prompt": "function"}

    def test_upload_module_loaded_without_confirm_dialog(self):
        assert _upload("""
console.log(JSON.stringify({
  goToPage: typeof window.goToPage,
  confirmIsNative: typeof window.confirm === "function",
}));
""") == {"goToPage": "function", "confirmIsNative": True}

    def test_review_module_loaded_without_confirm_dialog(self):
        assert _review("""
console.log(JSON.stringify({
  cancelJob: typeof window.cancelJob,
  retryJob: typeof window.retryJob,
}));
""") == {"cancelJob": "function", "retryJob": "function"}

    def test_confirm_probe_actually_records(self):
        """防空转：证明 `__calls.confirm` 真的会被记到（不是恒空数组）。"""
        assert _shim("""
window.confirm("probe-canary");
console.log(JSON.stringify({ n: __calls.confirm.length, first: __calls.confirm[0] }));
""") == {"n": 1, "first": "probe-canary"}


# ══════════════════════════════════════════════════════════════════════
class TestShimProvidesWorkingFallbacks:
    """confirm-dialog.js 缺席时，三个方法仍必须可用且语义正确。"""

    def test_confirm_delegates_to_native_and_returns_the_answer(self):
        assert _shim("""
__confirmAnswer = false;
const no = await window.PBC.confirmDialog({ title: "t", message: "m" });
__confirmAnswer = true;
const yes = await window.PBC.confirmDialog({ title: "t", message: "m" });
console.log(JSON.stringify({
  no: no, yes: yes,
  text: __calls.confirm[0],
  n: __calls.confirm.length,
}));
""") == {"no": False, "yes": True, "text": "t\n\nm", "n": 2}

    def test_missing_native_confirm_refuses_the_action(self):
        """**核心安全不变量**：连原生 confirm 都没有时，必须否决而非放行。"""
        assert _shim("""
delete window.confirm;
const v = await window.PBC.confirmDialog({ title: "删除？" });
console.log(JSON.stringify({ v: v, unhandled: __unhandled }));
""") == {"v": False, "unhandled": []}

    def test_error_toast_escalates_to_alert(self):
        assert _shim("""
window.PBC.showToast("删除失败: boom", "err");
console.log(JSON.stringify({
  alerts: __calls.alert,
  warned: __calls.warn.join(" | "),
}));
""") == {
            "alerts": ["删除失败: boom"],
            "warned": "[PBC] showToast 降级（confirm-dialog.js 未加载）: err 删除失败: boom",
        }

    def test_success_toast_does_not_alert(self):
        """成功类是正常路径，不该弹阻塞式 alert 打断用户。"""
        assert _shim("""
window.PBC.showToast("已删除", "ok");
window.PBC.showToast("提示", "info");
console.log(JSON.stringify({ alerts: __calls.alert.length }));
""") == {"alerts": 0}

    def test_prompt_returns_null_on_cancel_and_string_on_confirm(self):
        """契约与真实实现一致：取消 → null；确认 → 字符串（**空串也算确认**）。"""
        assert _shim("""
__promptAnswer = null;
const cancelled = await window.PBC.promptDialog({ title: "原因" });
__promptAnswer = "";
const empty = await window.PBC.promptDialog({ title: "原因" });
__promptAnswer = "ocr 乱码";
const typed = await window.PBC.promptDialog({ title: "原因", defaultValue: "x" });
console.log(JSON.stringify({
  cancelled: cancelled, empty: empty, typed: typed,
  def: __calls.prompt[2][1],
}));
""") == {"cancelled": None, "empty": "", "typed": "ocr 乱码", "def": "x"}

    def test_missing_native_prompt_returns_null(self):
        """没有原生 prompt 时必须返回 null（= 取消），绝不能返回空串
        （空串在下游是"已确认但没写原因"，会被当成有效输入）。"""
        assert _shim("""
delete window.prompt;
const v = await window.PBC.promptDialog({ title: "原因" });
console.log(JSON.stringify({ v: v }));
""") == {"v": None}


# ══════════════════════════════════════════════════════════════════════
class TestRealImplementationWins:
    """shim 只补缺失，绝不覆盖真实实现；且与加载顺序无关。"""

    _REAL_TOAST = """
const tc = () => document.getElementById("toast-container");
window.PBC.showToast("hi", "err");
const box = tc();
console.log(JSON.stringify({
  alerts: __calls.alert.length,
  domToasts: box ? box.children.length : 0,
  role: box && box.children[0] ? box.children[0].getAttribute("role") : null,
  unhandled: __unhandled,
}));
"""

    def test_shim_then_confirm_dialog_uses_real_implementation(self):
        out = run_js_async([SHIM, "confirm-dialog.js"], _PRE + self._REAL_TOAST,
                           stub=_NO_PBC)
        assert out == {"alerts": 0, "domToasts": 1, "role": "alert",
                       "unhandled": []}

    def test_confirm_dialog_then_shim_still_uses_real_implementation(self):
        """顺序无关性：即便 shim 后加载，也不得把真实实现换掉。"""
        out = run_js_async(["confirm-dialog.js", SHIM], _PRE + self._REAL_TOAST,
                           stub=_NO_PBC)
        assert out == {"alerts": 0, "domToasts": 1, "role": "alert",
                       "unhandled": []}

    def test_real_confirm_dialog_renders_a_modal(self):
        """再补一条与 showToast 无关的观测量，证明换的是整个实现。"""
        out = run_js_async([SHIM, "confirm-dialog.js"], _PRE + """
window.PBC.confirmDialog({ title: "t" });
console.log(JSON.stringify({
  nativeConfirm: __calls.confirm.length,
  modals: __queryAll(document.body, '[role="dialog"]').length,
}));
""", stub=_NO_PBC)
        assert out == {"nativeConfirm": 0, "modals": 1}


# ══════════════════════════════════════════════════════════════════════
class TestConsumersSurviveWithoutConfirmDialog:
    """端到端：只有 shim 时，删除 / 取消流程仍可用且不抛错。"""

    def test_upload_delete_still_gates_and_proceeds(self):
        body = """
await askDelete("J1");
console.log(JSON.stringify({
  confirms: __calls.confirm.length,
  text: __calls.confirm[0] || "",
  writes: __writes().map((c) => c.method + " " + c.url),
  unhandled: __unhandled,
}));
"""
        out = _upload(body)
        assert out["confirms"] == 1
        # ⚠️ 只查文件名是**空断言**：文件名本来就在 title 里，去掉 message 它照样绿
        # （变异 M11 实测没抓到）。必须锚定**只有 message 才含**的字样。
        assert "J1.pdf" in out["text"]
        assert "不可恢复" in out["text"], "降级确认框必须带上后果说明"
        assert "审计日志" in out["text"]
        assert out["writes"] == ["DELETE /api/jobs/J1?keep_pdf=false"]
        assert out["unhandled"] == []

    def test_upload_decline_sends_no_request(self):
        assert _upload("""
__confirmAnswer = false;
await askDelete("J1");
console.log(JSON.stringify({ writes: __writes().length, alerts: __calls.alert.length }));
""") == {"writes": 0, "alerts": 0}

    def test_upload_missing_confirm_refuses(self):
        assert _upload("""
delete window.confirm;
const threw = await askDelete("J1").then(() => null, (e) => String(e));
console.log(JSON.stringify({
  writes: __writes().length, threw: threw, unhandled: __unhandled,
}));
""") == {"writes": 0, "threw": None, "unhandled": []}

    def test_upload_failed_delete_surfaces_via_alert(self):
        assert _upload("""
__failWrite = true;
await askDelete("J1");
console.log(JSON.stringify({ alerts: __calls.alert.join(" | ") }));
""") == {"alerts": "删除失败: boom"}

    def test_review_cancel_still_gates_and_proceeds(self):
        body = """
const threw = await askCancel();
console.log(JSON.stringify({
  threw: threw,
  confirms: __calls.confirm.length,
  writes: __writes().map((c) => c.method + " " + c.url),
  unhandled: __unhandled,
}));
"""
        out = _review(body)
        assert out["threw"] is None
        assert out["confirms"] == 1
        assert out["writes"] == ["POST /api/jobs/J1/cancel"]
        assert out["unhandled"] == []

    def test_review_missing_confirm_refuses(self):
        assert _review("""
delete window.confirm;
const threw = await askCancel();
console.log(JSON.stringify({
  threw: threw, writes: __writes().length, unhandled: __unhandled,
}));
""") == {"threw": None, "writes": 0, "unhandled": []}

    def test_review_failed_cancel_surfaces_via_alert(self):
        assert _review("""
__failWrite = true;
await askCancel();
console.log(JSON.stringify({ alerts: __calls.alert.join(" | ") }));
""") == {"alerts": "取消失败: boom"}


# ══════════════════════════════════════════════════════════════════════
class TestTemplatesLoadTheShim:
    """三个页面都必须引入 shim，且**先于** confirm-dialog.js。

    漏引一个页面 = 该页悄悄退回"点了没反应"的旧形态，而没有任何测试会红。
    """

    TEMPLATES = ["review.html", "upload.html", "settings.html"]

    def _srcs(self, name: str) -> list[str]:
        html = (REPO / "templates" / name).read_text(encoding="utf-8")
        return re.findall(r'<script[^>]*src="/static/([\w.-]+\.js)[?"]', html)

    def test_every_page_loads_the_shim(self):
        for t in self.TEMPLATES:
            assert SHIM in self._srcs(t), f"{t} 未引入 {SHIM}"

    def test_shim_precedes_confirm_dialog(self):
        for t in self.TEMPLATES:
            srcs = self._srcs(t)
            assert srcs.index(SHIM) < srcs.index("confirm-dialog.js"), (
                f"{t} 里 {SHIM} 必须在 confirm-dialog.js 之前"
            )

    def test_shim_has_no_dependencies(self):
        """shim 必须自足 —— 依赖别的共享件就又把加载顺序风险引回来了。"""
        src = (REPO / "static" / SHIM).read_text(encoding="utf-8")
        for dep in ("PbcStatus", "PbcSse", "PbcEta", "PbcFindingsMap", "PbcReview"):
            assert dep not in src, f"{SHIM} 不应依赖 {dep}"
