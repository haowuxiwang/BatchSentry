"""Upload 页的两项韧性护栏（P2）。

**10. 亚 MB 限额的文案**：`upload.js` 旧写法用
`Math.floor(max_bytes / 1024 / 1024)` 得到 `limitMB`，当 `max_bytes < 1MB` 时
结果是 **0**，于是提示变成"文件超过 **0MB** 上限" —— 荒谬，且让用户以为限额被
配坏了（真实原因是他传的文件确实超了，只是限额本身就小于 1MB）。改为按量级
选单位（B / KB / MB）。

**11. 容器缺失时的提前返回**：`loadHistory(1)` 在 `upload-jobs.js` 的**模块加载期**
被调用。旧代码紧接着 `listEl.innerHTML = ""`，容器一缺就抛 TypeError，
异常中断整个 IIFE ⇒ 后续所有事件绑定（删除 / 归档 / 翻页）全部失效，
页面表现为"加载完了但点什么都没反应"。
"""
from __future__ import annotations

from tests.js_harness import run_js_async

UPLOAD_FILES = ["status.js", "upload.js"]
JOBS_FILES = ["status.js", "upload-jobs.js"]

# ── upload.js：三个加载期必需元素 ─────────────────────────────────────
_UPLOAD_STUB = r"""
globalThis.__toasts = [];
window.__PBC__ = { limits: { max_bytes: %d }, needs_setup: true };
window.location = {};
window.PBC = {
  showToast: (m, t) => __toasts.push({ m: String(m), t: t || "info" }),
  confirmDialog: async () => false,
};
["file-input", "upload-area", "status"].forEach((id) => {
  document.body.appendChild(__el(id, "div"));
});
"""

_UPLOAD_PRE = r"""
const input = () => document.getElementById("file-input");
const status = () => document.getElementById("status");
/* 走真实入口：input 的 change 监听器 → uploadFile */
const pick = (size, name) => {
  input().dispatchEvent({
    type: "change",
    // lastModified 不能省：uploadFile 会 `new Date(file.lastModified).toISOString()`，
    // 缺了直接 RangeError（真实 File 对象一定有这个字段）。
    target: { files: [{ name: name || "a.pdf", size: size, lastModified: 1700000000000 }] },
  });
  return status().textContent;
};
"""


def _upload(max_bytes: int, body: str):
    return run_js_async(UPLOAD_FILES, _UPLOAD_PRE + "\n" + body,
                        stub=_UPLOAD_STUB % max_bytes)


class TestHarnessIsNotVacuous:
    def test_change_handler_is_reachable(self):
        """防空转：证明 change 事件真的驱动到了 uploadFile（能改到 status）。"""
        assert _upload(1024, 'console.log(JSON.stringify({ msg: pick(2048) }));') == {
            "msg": "文件超过 1 KB 上限"
        }


class TestUploadLimitMessageUnits:
    """限额文案必须按量级选单位 —— 亚 MB 时不能渲染成"0MB"。"""

    def test_sub_megabyte_limit_shows_kilobytes(self):
        assert _upload(512000, 'console.log(JSON.stringify({ msg: pick(600000) }));') == {
            "msg": "文件超过 500 KB 上限"
        }

    def test_sub_kilobyte_limit_shows_bytes(self):
        assert _upload(512, 'console.log(JSON.stringify({ msg: pick(600) }));') == {
            "msg": "文件超过 512 B 上限"
        }

    def test_whole_megabyte_limit_has_no_decimals(self):
        assert _upload(10485760, 'console.log(JSON.stringify({ msg: pick(11 * 1024 * 1024) }));') == {
            "msg": "文件超过 10 MB 上限"
        }

    def test_fractional_megabyte_limit_keeps_one_decimal(self):
        assert _upload(1572864, 'console.log(JSON.stringify({ msg: pick(2 * 1024 * 1024) }));') == {
            "msg": "文件超过 1.5 MB 上限"
        }

    def test_file_at_the_limit_is_not_rejected(self):
        """边界：`>` 而非 `>=` —— 恰好等于上限必须放行，否则用户传不了上限大小的文件。"""
        out = _upload(512000, 'console.log(JSON.stringify({ msg: pick(512000) }));')
        assert "上限" not in out["msg"]

    def test_under_limit_is_not_rejected(self):
        out = _upload(512000, 'console.log(JSON.stringify({ msg: pick(1) }));')
        assert "上限" not in out["msg"]


# ── upload-jobs.js：容器缺失时的加载韧性 ───────────────────────────────
_JOBS_STUB_TMPL = r"""
globalThis.__toasts = [];
window.PBC = {
  showToast: (m, t) => __toasts.push({ m: String(m), t: t || "info" }),
  confirmDialog: async () => false,
};
globalThis.__unhandled = [];
process.on("unhandledRejection", (e) => {
  const msg = String((e && e.message) || e);
  __unhandled.push(msg);
  console.error("[probe] unhandledRejection:", msg);
});
globalThis.fetch = async () => ({
  ok: true, status: 200,
  json: async () => ({ total_pages: 1, total_jobs: 0, jobs: [] }),
});
%s
"""


def _jobs(elements_js: str, body: str):
    stub = _JOBS_STUB_TMPL % elements_js
    pre = """
const settle = () => new Promise((r) => setImmediate(r));
const drain = async (n) => { for (let i = 0; i < (n || 6); i++) await settle(); };
/* 排空模块加载期发起的那次 loadHistory(1)，让列表渲染完成后再断言 */
await drain();
"""
    return run_js_async(JOBS_FILES, pre + "\n" + body, stub=stub)


_ALL_ELEMENTS = """
["history-list", "history-pagination", "history-count",
 "archived-list", "archived-count"].forEach((id) => {
  document.body.appendChild(__el(id, id.indexOf("list") >= 0 ? "ul" : "div"));
});
"""


class TestLoadHistoryContainerGuards:
    """`loadHistory(1)` 在模块加载期执行；容器缺失不得中断整个 IIFE。"""

    _ALIVE = """
console.log(JSON.stringify({
  goToPage: typeof window.goToPage,
  refreshHistory: typeof window.refreshHistory,
  unhandled: __unhandled,
}));
"""

    def test_missing_list_container_does_not_abort_the_module(self):
        """核心：列表容器缺失时，后续绑定（goToPage / refreshHistory）必须仍在。"""
        assert _jobs("", self._ALIVE) == {
            "goToPage": "function", "refreshHistory": "function", "unhandled": [],
        }

    def test_missing_pagination_container_does_not_abort_the_module(self):
        assert _jobs("""
document.body.appendChild(__el("history-list", "ul"));
""", self._ALIVE) == {
            "goToPage": "function", "refreshHistory": "function", "unhandled": [],
        }

    def test_missing_count_container_still_renders_the_list(self):
        """计数是次要 UI：它缺了不能连带把列表渲染也跳掉。

        ⚠️ 只断言 `children > 0` 是**空断言**：`loadHistory` 的 `catch` 也会往
        列表里写一条"加载失败"的 `<li>`，长度同样是 1。必须锚定**正常空态**
        文案，才能区分"渲染成功"与"内部 TypeError 被兜住"。
        """
        out = _jobs("""
document.body.appendChild(__el("history-list", "ul"));
document.body.appendChild(__el("history-pagination", "div"));
""", """
const list = document.getElementById("history-list");
console.log(JSON.stringify({
  text: list.textContent,
  goToPage: typeof window.goToPage,
  unhandled: __unhandled,
}));
""")
        assert "暂无历史记录" in out["text"], "计数缺失不得把正常渲染变成错误态"
        assert "加载失败" not in out["text"]
        assert out["goToPage"] == "function"
        assert out["unhandled"] == []

    def test_all_containers_present_is_the_baseline(self):
        """防空转：容器齐全时同样不抛错（否则上面几条测的可能是"永远都缺"）。"""
        assert _jobs(_ALL_ELEMENTS, self._ALIVE) == {
            "goToPage": "function", "refreshHistory": "function", "unhandled": [],
        }
