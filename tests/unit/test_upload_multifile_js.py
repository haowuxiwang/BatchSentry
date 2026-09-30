"""多文件上传：静默丢弃 → 串行提交（对抗审查 P0）。

**要防的失效模式**

`templates/upload.html` 的 `<input type="file">` 没有 `multiple`，且
`static/upload.js` 的 change / drop 处理器都只取 **`files[0]`**
⇒ 用户拖入 3 份 PDF，**只有 1 份被处理，另外 2 份无任何提示地消失**
（静默数据丢失 —— 用户以为传上去了）。后端 `MAX_CONCURRENT_JOBS=3` 的
并发能力因此对用户完全不可见。

**为什么"只加 `multiple`"是伪修复**（本文件的核心断言）

`uploadFile` 的成功流程是**单文件语义**：

  1. 成功后 1.5s `window.location.href = /jobs/{id}/review` **跳转**；
  2. 上传区被 `pointerEvents = "none"` **禁用**；
  3. 进度条是**单例**（`#upload-progress`）。

⇒ 第 1 份成功就跳走并杀掉其余上传。所以多文件路径必须
**不跳转** + **串行** + 汇总，本文件逐条钉住这三点。

⚠️ 每条断言都配防空转用例。尤其"串行"必须靠
`__xhr.sent.length`（send 的**即时**副作用）观察 —— 若 XHR 桩自动结算，
串行与并行就不可区分，断言会变成空断言。
"""
from __future__ import annotations

from pathlib import Path

from tests.js_harness import run_js_async

REPO = Path(__file__).resolve().parents[2]
UPLOAD_JS = REPO / "static" / "upload.js"
UPLOAD_HTML = REPO / "templates" / "upload.html"

UPLOAD_FILES = ["status.js", "upload.js"]

# 限额设成 1000 B：默认文件 500 B（放行），"过大"用 2000 B。
_STUB_TMPL = r"""
globalThis.__toasts = [];
globalThis.__confirmCalls = 0;
globalThis.__redirects = [];
window.__PBC__ = { limits: { max_bytes: 1000 }, needs_setup: %s };
globalThis.location = {
  _href: "",
  set href(v) { this._href = String(v); __redirects.push(String(v)); },
  get href() { return this._href; },
};
window.PBC = {
  showToast: (m, t) => __toasts.push({ m: String(m), t: t || "info" }),
  confirmDialog: async () => { __confirmCalls += 1; return false; },
};
["file-input", "upload-area", "status"].forEach((id) => {
  document.body.appendChild(__el(id, "div"));
});
"""

_PRE = r"""
const input = () => document.getElementById("file-input");
const area = () => document.getElementById("upload-area");
const status = () => document.getElementById("status");
const text = () => status().textContent;
const settle = () => new Promise((r) => setImmediate(r));

const mkFile = (name, size) => ({
  name: name,
  size: size === undefined ? 500 : size,
  lastModified: 1700000000000,
});

/* 走真实入口：input 的 change 监听器 */
const pick = (names) => {
  input().dispatchEvent({
    type: "change",
    target: { files: names.map((n) => (typeof n === "string" ? mkFile(n) : n)) },
  });
  return text();
};

/* 走真实入口：upload-area 的 drop 监听器 */
const drop = (names) => {
  area().dispatchEvent({
    type: "drop",
    dataTransfer: { files: names.map((n) => (typeof n === "string" ? mkFile(n) : n)) },
    preventDefault() {},
  });
  return text();
};
"""


def _probe(body: str, needs_setup: bool = False):
    stub = _STUB_TMPL % ("true" if needs_setup else "false")
    return run_js_async(UPLOAD_FILES, _PRE + "\n" + body, stub=stub)


# ─────────────────────────────────────────────────────────────────────
# 防空转：证明 XHR 桩真的被 upload.js 用到
# ─────────────────────────────────────────────────────────────────────
class TestHarnessIsNotVacuous:
    def test_xhr_stub_receives_the_request(self):
        """若 `__xhr.sent` 恒为空，下面所有"发了几次请求"的断言都会假绿。"""
        out = _probe(
            """
pick(["a.pdf"]);
console.log(JSON.stringify({
  sent: __xhr.sent.length,
  url: __xhr.sent[0] && __xhr.sent[0].url,
  status: text(),
}));
"""
        )
        assert out["sent"] == 1, "XHR 桩没收到请求 —— upload.js 没走 XMLHttpRequest？"
        assert "/api/jobs" in out["url"]
        assert "上传中" in out["status"], "change 事件没驱动到 uploadFile"

    def test_send_does_not_auto_settle(self):
        """`send()` **不得**自动结算 —— 否则串行与并行不可区分（空断言温床）。"""
        out = _probe(
            """
pick(["a.pdf"]);
console.log(JSON.stringify({ live: __xhr._live.length }));
"""
        )
        assert out["live"] == 1, "XHR 桩自动结算了 —— 串行断言会失去意义"


# ─────────────────────────────────────────────────────────────────────
# 1. 不再静默丢弃
# ─────────────────────────────────────────────────────────────────────
class TestFilesAreNotSilentlyDropped:
    def test_change_with_three_files_sends_three_requests(self):
        out = _probe(
            """
pick(["a.pdf", "b.pdf", "c.pdf"]);
__xhr.flush();
console.log(JSON.stringify({ sent: __xhr.sent.length, status: text() }));
"""
        )
        assert out["sent"] == 3, (
            f"拖入 3 份只发了 {out['sent']} 次请求 —— 其余被静默丢弃（原缺陷）"
        )

    def test_drop_with_three_files_sends_three_requests(self):
        out = _probe(
            """
drop(["a.pdf", "b.pdf", "c.pdf"]);
__xhr.flush();
console.log(JSON.stringify({ sent: __xhr.sent.length }));
"""
        )
        assert out["sent"] == 3, "拖拽入口仍在静默丢弃文件"

    def test_single_file_still_sends_exactly_one(self):
        out = _probe(
            """
pick(["a.pdf"]);
__xhr.flush();
console.log(JSON.stringify({ sent: __xhr.sent.length }));
"""
        )
        assert out["sent"] == 1

    def test_source_no_longer_indexes_files_zero(self):
        """源码守卫：原缺陷的**字面形态**是两个处理器里的 `files[0]` 取值。

        ⚠️ 不能断言裸串 `files[0]` —— 函数内注释也会出现它（本文件的
        `submitFiles` 注释里就写着原缺陷的形态），裸串匹配会把注释当违规。
        必须锚到**取值表达式**本身。
        """
        src = UPLOAD_JS.read_text(encoding="utf-8")
        for bad in ("e.target.files[0]", "e.dataTransfer.files[0]"):
            assert bad not in src, (
                f"upload.js 仍在用 `{bad}` —— 多选会被静默截断成 1 份"
            )
        # 防空转：确认锚点确实能匹配到"取值"形态（否则上面两条可能永远为真）
        assert ".files" in src, "提取器/锚点失效 —— 找不到任何 .files 引用"

    def test_template_input_allows_multiple(self):
        html = UPLOAD_HTML.read_text(encoding="utf-8")
        idx = html.index('id="file-input"')
        # 取该 input 标签的整个开标签范围再断言，避免命中别处的 multiple
        start = html.rindex("<input", 0, idx)
        end = html.index(">", idx)
        assert "multiple" in html[start:end], "file-input 缺 multiple 属性"


# ─────────────────────────────────────────────────────────────────────
# 2. 串行（共享单例进度条才有意义；也避免一次打满后端并发配额）
# ─────────────────────────────────────────────────────────────────────
class TestMultiFileIsSequential:
    def test_only_one_request_is_in_flight_at_a_time(self):
        """**关键可观测信号**：change 之后立刻查 `__xhr.sent`。

        串行 ⇒ 只有 1 次 send（其余排队等结算）；并行 ⇒ 3 次。
        """
        out = _probe(
            """
pick(["a.pdf", "b.pdf", "c.pdf"]);
console.log(JSON.stringify({
  sent: __xhr.sent.length,
  live: __xhr._live.length,
}));
"""
        )
        assert out["sent"] == 1, (
            f"change 后已发出 {out['sent']} 次请求 —— 不是串行（进度条会互相覆盖）"
        )
        assert out["live"] == 1

    def test_next_request_goes_out_only_after_the_previous_settles(self):
        out = _probe(
            """
pick(["a.pdf", "b.pdf", "c.pdf"]);
const after0 = __xhr.sent.length;
__xhr.flushOne();
const after1 = __xhr.sent.length;
__xhr.flushOne();
const after2 = __xhr.sent.length;
__xhr.flushOne();
const after3 = __xhr.sent.length;
console.log(JSON.stringify({ after0, after1, after2, after3 }));
"""
        )
        assert [out["after0"], out["after1"], out["after2"], out["after3"]] == [1, 2, 3, 3], (
            f"请求不是逐份串行推进：{out}"
        )

    def test_progress_label_counts_the_batch(self):
        """串行的收益之一：状态行能报"第几份 / 共几份"。"""
        out = _probe(
            """
pick(["a.pdf", "b.pdf", "c.pdf"]);
const first = text();
__xhr.flushOne();
const second = text();
console.log(JSON.stringify({ first, second }));
"""
        )
        assert "（1/3）" in out["first"], f"首份未标注批次进度：{out['first']}"
        assert "（2/3）" in out["second"], f"第二份未标注批次进度：{out['second']}"


# ─────────────────────────────────────────────────────────────────────
# 3. 多文件**不跳转**（跳走会杀掉其余上传）
# ─────────────────────────────────────────────────────────────────────
class TestMultiFileDoesNotRedirect:
    def test_multi_file_never_redirects(self):
        out = _probe(
            """
pick(["a.pdf", "b.pdf", "c.pdf"]);
__xhr.flush();
console.log(JSON.stringify({ redirects: __redirects }));
"""
        )
        assert out["redirects"] == [], (
            f"多文件路径发生了跳转（{out['redirects']}）—— 会杀掉其余上传"
        )

    def test_multi_file_schedules_no_redirect_timer(self):
        """更严格的形态：连**定时器**都不该排（排了就是"1.5s 后跳走"）。"""
        out = _probe(
            """
pick(["a.pdf", "b.pdf"]);
__xhr.flush();
console.log(JSON.stringify({
  pending: __timers.filter((t) => t.kind === "timeout" && !t.cleared && !t._ran).length,
}));
"""
        )
        assert out["pending"] == 0, "多文件路径仍排了跳转定时器"

    def test_single_file_still_redirects(self):
        """单文件必须**保留**既有体验（上传完自动进复核页）。"""
        out = _probe(
            """
pick(["a.pdf"]);
__xhr.flush();
const before = __redirects.length;
__flushTimers();
console.log(JSON.stringify({ before, after: __redirects }));
"""
        )
        assert out["before"] == 0, "单文件不该立即跳转（应等延迟读完提示）"
        assert len(out["after"]) == 1 and "/jobs/" in out["after"][0], (
            f"单文件未跳转复核页：{out['after']}"
        )


# ─────────────────────────────────────────────────────────────────────
# 4. 不合格文件一次性报出（不是逐个覆盖成"只剩最后一条"）
# ─────────────────────────────────────────────────────────────────────
class TestRejectionsAreReportedOnce:
    def test_all_rejected_names_appear_in_one_message(self):
        """被跳过的文件名必须出现在**最终汇总**里。

        ⚠️ 不能断言 `pick()` 的即时返回值：`submitFiles` 设的"已跳过…"会被紧接着
        的逐份状态（"上传中 ok.pdf…"）覆盖，即时读到的必然是后者。汇总才是
        持久可见的位置 —— 断在即时值上会得到一条**假失败**，也会掩盖真正的
        缺口（首版实现只在即时提示里列名字 ⇒ 用户永远看不到）。
        """
        out = _probe(
            """
pick(["ok.pdf", "bad1.txt", "bad2.txt"]);
__xhr.flush();
await settle();
console.log(JSON.stringify({ msg: text(), sent: __xhr.sent.length }));
"""
        )
        assert "bad1.txt" in out["msg"] and "bad2.txt" in out["msg"], (
            f"汇总里未列出被跳过的文件（用户无法得知哪几份没传）：{out['msg']}"
        )
        assert out["sent"] == 1, "合格的那份没被提交"

    def test_rejection_notice_is_not_lost_to_the_per_file_status(self):
        """防空转：证明逐份状态**确实**会覆盖即时提示 —— 所以汇总才是唯一可靠位置。"""
        out = _probe(
            """
const immediate = pick(["ok.pdf", "bad1.txt"]);
__xhr.flush();
await settle();
console.log(JSON.stringify({ immediate, final: text() }));
"""
        )
        assert "bad1.txt" not in out["immediate"], (
            "即时提示里出现了被跳过的文件名 —— 前提变了，请复核本用例的结论"
        )
        assert "bad1.txt" in out["final"], "最终汇总里没有被跳过的文件名"

    def test_all_rejected_sends_nothing_and_names_every_file(self):
        out = _probe(
            """
const msg = pick(["bad1.txt", "bad2.exe"]);
__xhr.flush();
await settle();
console.log(JSON.stringify({ msg, sent: __xhr.sent.length }));
"""
        )
        assert out["sent"] == 0, "全部不合格却仍发了请求"
        assert "bad1.txt" in out["msg"] and "bad2.exe" in out["msg"]

    def test_all_rejected_does_not_claim_a_batch_was_submitted(self):
        """全部不合格时必须**只报拒绝**，不能走"批次汇总"那条路。

        否则状态行变成「已提交 **0** 份文件，下方任务列表逐条显示分析进度」——
        把"什么都没传"说成"提交完成"，用户会以为传上去了在等分析。
        （这条断言来自一次 `MISSED` 的分诊：去掉"全部不合格时提前返回"这个
        守卫后，其余断言都仍通过 —— 差异只在这一句措辞上，而它是**真实**的
        诚实性问题，故补断言而非放松变异。）
        """
        out = _probe(
            """
pick(["bad1.txt", "bad2.exe"]);
__xhr.flush();
await settle();
console.log(JSON.stringify({ msg: text() }));
"""
        )
        assert "已提交" not in out["msg"], (
            f"全部不合格却报出了提交汇总（把「没传」说成「提交完成」）：{out['msg']}"
        )
        assert "已跳过" in out["msg"], f"未报出拒绝信息：{out['msg']}"

    def test_oversized_file_is_rejected_with_its_reason(self):
        out = _probe(
            """
const msg = pick([mkFile("big.pdf", 2000)]);
console.log(JSON.stringify({ msg, sent: __xhr.sent.length }));
"""
        )
        assert out["sent"] == 0
        assert "上限" in out["msg"], f"超限未报出原因：{out['msg']}"

    def test_file_exactly_at_the_limit_is_accepted(self):
        """边界：`>` 而非 `>=` —— 恰好等于上限必须放行。"""
        out = _probe(
            """
pick([mkFile("edge.pdf", 1000)]);
__xhr.flush();
console.log(JSON.stringify({ sent: __xhr.sent.length, msg: text() }));
"""
        )
        assert out["sent"] == 1, "恰好等于上限的文件被拒了"


# ─────────────────────────────────────────────────────────────────────
# 5. 未配置 LLM：多文件只弹**一次**对话框
# ─────────────────────────────────────────────────────────────────────
class TestNeedsSetupDialogFiresOnce:
    def test_multi_file_shows_a_single_dialog_and_sends_nothing(self):
        """逐个 uploadFile 会让每份文件各弹一次（N 个叠加对话框）—— 用户以为卡死。"""
        out = _probe(
            """
pick(["a.pdf", "b.pdf", "c.pdf"]);
await settle();
console.log(JSON.stringify({ calls: __confirmCalls, sent: __xhr.sent.length }));
""",
            needs_setup=True,
        )
        assert out["calls"] == 1, f"弹了 {out['calls']} 次对话框（应为 1 次）"
        assert out["sent"] == 0, "未配置 LLM 却仍发了上传请求"

    def test_single_file_also_shows_the_dialog(self):
        out = _probe(
            """
pick(["a.pdf"]);
await settle();
console.log(JSON.stringify({ calls: __confirmCalls, sent: __xhr.sent.length }));
""",
            needs_setup=True,
        )
        assert out["calls"] == 1
        assert out["sent"] == 0


# ─────────────────────────────────────────────────────────────────────
# 6. 汇总 + 上传区恢复 + 失败不卡队列
# ─────────────────────────────────────────────────────────────────────
class TestBatchSummaryAndRecovery:
    def test_summary_reports_the_batch_size(self):
        out = _probe(
            """
pick(["a.pdf", "b.pdf", "c.pdf"]);
__xhr.flush();
console.log(JSON.stringify({ msg: text() }));
"""
        )
        assert "已提交 3 份" in out["msg"], f"批次结束后无汇总：{out['msg']}"

    def test_drop_zone_is_released_after_the_batch(self):
        """uploadFile 会禁用上传区（防重复提交）；整批结束后必须恢复，
        否则用户再也传不了第二批。"""
        out = _probe(
            """
pick(["a.pdf", "b.pdf"]);
__xhr.flush();
console.log(JSON.stringify({ pe: area().style.pointerEvents }));
"""
        )
        assert out["pe"] == "", f"整批结束后上传区仍被禁用：{out['pe']!r}"

    def test_drop_zone_stays_locked_during_the_batch(self):
        out = _probe(
            """
pick(["a.pdf", "b.pdf"]);
console.log(JSON.stringify({ pe: area().style.pointerEvents }));
"""
        )
        assert out["pe"] == "none", "批次进行中上传区未锁定（可插入新批次）"

    def test_a_failed_file_does_not_stall_the_queue(self):
        out = _probe(
            """
__xhr.queue.push({ status: 500, body: { detail: "服务端炸了" } });
pick(["a.pdf", "b.pdf", "c.pdf"]);
__xhr.flush();
console.log(JSON.stringify({ sent: __xhr.sent.length, msg: text() }));
"""
        )
        assert out["sent"] == 3, (
            f"第 1 份失败后队列停住（只发了 {out['sent']} 次）—— 其余文件被吞"
        )
        assert "失败" in out["msg"], f"汇总未报出失败份数：{out['msg']}"

    def test_network_error_does_not_stall_the_queue(self):
        out = _probe(
            """
__xhr.queue.push({ networkError: true });
pick(["a.pdf", "b.pdf"]);
__xhr.flush();
console.log(JSON.stringify({ sent: __xhr.sent.length }));
"""
        )
        assert out["sent"] == 2, "网络错误后队列停住"

    def test_409_cancel_does_not_stall_the_queue(self):
        """409 弹窗被取消时也必须结算，否则后面几份永远发不出去。

        ⚠️ `confirmDialog(...).then(...)` 是**微任务**：`__xhr.flush()` 之后必须
        `await settle()` 才能看到队列推进。不排空就断言，看到的是"还没跑"而非
        "没跑" —— 那是空断言（本项目踩过的同族坑）。
        """
        out = _probe(
            """
__xhr.queue.push({
  status: 409,
  body: { detail: "文件已上传过: 任务 J0「a.pdf」，状态 review" },
});
pick(["a.pdf", "b.pdf"]);
await settle();
__xhr.flush();          // 结算第 1 份 → 409 → 弹确认框
await settle();         // 让 .then 跑完 → 取消 → 结算 → next()
await settle();
__xhr.flush();          // 结算第 2 份
await settle();
await settle();
console.log(JSON.stringify({
  sent: __xhr.sent.length,
  confirmCalls: __confirmCalls,
  msg: text(),
}));
"""
        )
        assert out["confirmCalls"] == 1, "409 未弹去重确认框"
        assert out["sent"] == 2, (
            f"409 取消后队列停住（只发了 {out['sent']} 次）—— 后续文件被吞"
        )

    def test_force_retry_passes_opts_through(self):
        """409 确认「仍要重传」时 opts 必须透传 —— 否则该份不结算，队列卡死。"""
        out = _probe(
            """
window.PBC.confirmDialog = async () => true;
__xhr.queue.push({
  status: 409,
  body: { detail: "文件已上传过: 任务 J0「a.pdf」，状态 review" },
});
pick(["a.pdf", "b.pdf"]);
await settle();
__xhr.flush();          // 第 1 份 → 409 → 确认框（返回 true）
await settle();         // .then → uploadFile(file, true, opts) → 重传请求
await settle();
__xhr.flush();          // 结算重传 → 成功 → next() → 第 2 份
await settle();
__xhr.flush();          // 结算第 2 份
await settle();
await settle();
console.log(JSON.stringify({
  sent: __xhr.sent.length,
  urls: __xhr.sent.map((s) => s.url),
}));
"""
        )
        assert out["sent"] == 3, (
            f"force 重传后队列未推进（共 {out['sent']} 次）—— opts 未透传"
        )
        assert "force=1" in out["urls"][1], f"重传未带 force=1：{out['urls']}"
