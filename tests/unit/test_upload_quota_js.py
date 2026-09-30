"""并发额度：发送前预检 + 额度 409 不再被当成"上传失败"（#17/#18/#19 修复的护栏）。

**要防的失效模式**（本文件钉住的就是它）

后端并发配额是 `MAX_CONCURRENT_JOBS`（默认 3），且**硬拒绝**（409，不排队）。
前端多文件上传是**串行**的（`submitFiles` 的队列在"上传结算"时推进，不是
在"job 结束"时）。两条合起来 ⇒ 用户选 5 份时第 4、5 份**必然**吃 409，而吃
409 的那一刻文件**已经完整传完**（大文件几百 MB、分钟级）。旧行为还把它当
"上传失败"报红、`settleOnce(false)` + `releaseDropZone()` 丢弃文件选择 ——
用户白等一次传输，还得重新选、重传一遍。

**修法（本文件逐条钉住）**
  1. 发送**前**读 `window.PbcJobs.quota()`（活跃数由 upload-jobs.js 从它唯一
     持有的 /api/jobs/live 聚合帧登记；额度由服务端注入），满额就**先等空位**，
     不发请求 ⇒ 不再白传。
  2. 仍吃到额度 409（预检与 INSERT 之间的窗口）⇒ `info` 级提示 + 有界自动重试，
     **不丢弃**文件选择。
  3. **fail-open 是硬约束**：额度读不到 / 非法 ⇒ 跳过预检直接发。绝不能因为
     一次注入失误把整个上传功能锁死。

⚠️ 每条断言都配防空转用例。尤其"没发请求"必须靠 `__xhr.sent.length` 这种
**即时**副作用观察 —— 若把"没发"当成"还没轮到他"，断言会变成空断言。
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.js_harness import run_js, run_js_async

REPO = Path(__file__).resolve().parents[2]
UPLOAD_JS = REPO / "static" / "upload.js"
UPLOAD_JOBS_JS = REPO / "static" / "upload-jobs.js"

# 只加载 upload.js：upload-jobs.js 需要一整套历史列表 DOM，本文件测的是
# **消费侧**契约（upload.js 如何用 quota()），发布侧另在
# test_upload_jobs_js.py 里钉。
UPLOAD_FILES = ["status.js", "upload.js"]

# 配额 409 的**真实**后端文案（api/jobs/upload.py / actions.py 单点产出）。
_QUOTA_409 = "已有 3 个任务在处理中，上限为 3。请等待完成或取消后再试。"
_DEDUP_409 = "该文件已上传过（任务 abc123「a.pdf」，状态 review）。点击历史记录即可查看；"

_STUB_TMPL = r"""
globalThis.__toasts = [];
globalThis.__confirmCalls = 0;
globalThis.__redirects = [];
globalThis.__quota = { active: 0, limit: 3 };
/* 时钟可控：waitForQuotaSlot 的 30min 上界不能靠真实等待去触发 */
globalThis.__clock = { now: Date.now() };
Date.now = () => __clock.now;
globalThis.__advanceClock = (ms) => { __clock.now += ms; };
window.__PBC__ = {
  limits: { max_bytes: 1000 },
  needs_setup: false,
  concurrency: { limit: 3 },
};
globalThis.location = {
  _href: "",
  set href(v) { this._href = String(v); __redirects.push(String(v)); },
  get href() { return this._href; },
};
window.PBC = {
  showToast: (m, t) => __toasts.push({ m: String(m), t: t || "info" }),
  confirmDialog: async () => { __confirmCalls += 1; return false; },
};
/* 共享件（正常由 upload-jobs.js 发布）：%s */
window.PbcJobs = %s;
["file-input", "upload-area", "status"].forEach((id) => {
  document.body.appendChild(__el(id, "div"));
});
"""

_SHARED = (
    "{ quota: () => ({ active: __quota.active, limit: __quota.limit }) }",
    "undefined",
)

_PRE = r"""
const input = () => document.getElementById("file-input");
const area = () => document.getElementById("upload-area");
const statusEl = () => document.getElementById("status");
const text = () => statusEl().textContent;
/* setStatus 把 kind 写进 <p> 的 class —— 这是"是 err 还是 info"的**唯一**
   可观测信号。取不到就抛，避免"取不到 ⇒ undefined !== 'err' ⇒ 断言假绿"。 */
const kind = () => {
  const p = statusEl().querySelector("p");
  if (!p) throw new Error("status 里没有 <p> —— setStatus 没跑？");
  return p.className;
};
const isErr = () => kind().includes("text-destructive");
const isInfo = () => kind().includes("text-foreground");

const settle = () => new Promise((r) => setImmediate(r));

/* 交错驱动"定时器队列 + 微任务"：waitForQuotaSlot 的续体在微任务里跑，
   而它下一轮的 sleep 是在续体里才新建的 —— 只 flush 一次会漏掉。 */
const drive = async (rounds) => {
  for (let i = 0; i < (rounds || 4); i++) {
    __flushTimers(20);
    await settle();
  }
};

const mkFile = (name, size) => ({
  name: name,
  size: size === undefined ? 500 : size,
  lastModified: 1700000000000,
});

const pick = (names) => {
  input().dispatchEvent({
    type: "change",
    target: { files: names.map((n) => (typeof n === "string" ? mkFile(n) : n)) },
  });
  return text();
};

const drop = (names) => {
  area().dispatchEvent({
    type: "drop",
    dataTransfer: { files: names.map((n) => (typeof n === "string" ? mkFile(n) : n)) },
    preventDefault() {},
  });
  return text();
};
"""


def _probe(body: str, shared: bool = True, needs_setup: bool = False):
    stub = _STUB_TMPL % (
        "已发布" if shared else "缺失",
        _SHARED[0] if shared else _SHARED[1],
    )
    return run_js_async(UPLOAD_FILES, _PRE + "\n" + body, stub=stub)


# ─────────────────────────────────────────────────────────────────────
# 防空转：证明探针本身有效（否则下面所有"没发请求"的断言都可能假绿）
# ─────────────────────────────────────────────────────────────────────
class TestHarnessIsNotVacuous:
    def test_status_kind_is_observable(self):
        """`kind()` 必须能区分 err / info —— 否则"不是 err"这类断言毫无判别力。

        ⚠️ 409 必须**先入队再 pick**：XHR 桩的 `send()` 在 `pick` 时就消费了
        队列，事后 push 只会在 `_live` 之外排队，`flush()` 结算的仍是那个
        已带默认 200 响应的请求 —— 断言会静默落到成功路径上（假绿）。
        """
        out = _probe(
            """
__xhr.queue.push({ status: 409, body: { detail: "别的东西" } });
pick(["a.pdf"]);
__xhr.flush();
console.log(JSON.stringify({ info: isInfo(), err: isErr(), status: text() }));
"""
        )
        assert "上传失败" in out["status"], out["status"]
        assert out["err"] is True, "setStatus 的 kind 读不到 —— 断言会假绿"
        assert out["info"] is False

    def test_quota_shared_object_is_reachable(self):
        """共享件确实被 upload.js 读到（而非探针自说自话）。"""
        out = _probe(
            """
__quota.active = 2;
__quota.limit = 3;
console.log(JSON.stringify({
  viaWindow: window.PbcJobs.quota(),
  hasQuotaFn: typeof window.PbcJobs.quota === "function",
}));
"""
        )
        assert out["hasQuotaFn"] is True
        assert out["viaWindow"] == {"active": 2, "limit": 3}

    def test_baseline_free_slot_sends_a_request(self):
        """基线：有空位时确实发请求。否则"满额时没发"也可能是"根本不会发"。"""
        out = _probe(
            """
pick(["a.pdf"]);
console.log(JSON.stringify({ sent: __xhr.sent.length, url: __xhr.sent[0] && __xhr.sent[0].url }));
"""
        )
        assert out["sent"] == 1, "基线就没发请求 —— 本文件所有断言失去意义"
        assert "/api/jobs" in out["url"]

    def test_source_no_longer_labels_quota_409_as_a_generic_failure(self):
        """变异锚点自检：通用失败文案仍在，但额度分支必须先于它。

        用**调用点顺序**锚定（不是标识符出现与否）—— 源码里注释也会出现
        `isQuotaRejection`，裸串匹配会把注释当实现（本仓库踩过）。
        """
        src = UPLOAD_JS.read_text(encoding="utf-8")
        i_quota = src.index("if (xhr.status === 409 && isQuotaRejection(detail))")
        i_generic = src.index('setStatus(`上传失败: HTTP ${xhr.status}${detail}`, "err")')
        assert i_quota < i_generic, "额度分支必须在通用失败分支之前，否则永远走不到"


# ─────────────────────────────────────────────────────────────────────
# 1. fail-open + 快路径保持同步
# ─────────────────────────────────────────────────────────────────────
class TestFailOpenAndSynchronousFastPath:
    def test_missing_shared_object_falls_back_to_sending(self):
        """共享件缺失 ⇒ 跳过预检直接发（绝不因为读不到额度而锁死上传）。"""
        out = _probe(
            """
delete window.PbcJobs;
pick(["a.pdf"]);
console.log(JSON.stringify({ sent: __xhr.sent.length, hasPbcJobs: typeof window.PbcJobs }));
""",
            shared=True,
        )
        assert out["hasPbcJobs"] == "undefined"
        assert out["sent"] == 1, "共享件缺失时把上传拦住了 —— fail-open 被破坏"

    def test_limit_zero_means_unknown_and_falls_back_to_sending(self):
        out = _probe(
            """
__quota.limit = 0;
pick(["a.pdf"]);
console.log(JSON.stringify({ sent: __xhr.sent.length }));
"""
        )
        assert out["sent"] == 1, "limit<=0（未注入）应视为未知 ⇒ 放行"

    def test_non_numeric_limit_falls_back_to_sending(self):
        out = _probe(
            """
window.PbcJobs = { quota: () => ({ active: 5, limit: "三" }) };
pick(["a.pdf"]);
console.log(JSON.stringify({ sent: __xhr.sent.length }));
"""
        )
        assert out["sent"] == 1, "limit 非数值应视为未知 ⇒ 放行"

    def test_throwing_quota_reader_falls_back_to_sending(self):
        out = _probe(
            """
window.PbcJobs = { quota: () => { throw new Error("boom"); } };
pick(["a.pdf"]);
console.log(JSON.stringify({ sent: __xhr.sent.length }));
"""
        )
        assert out["sent"] == 1, "quota() 抛错应被吞掉并放行，不能把上传带崩"

    def test_free_slot_keeps_the_synchronous_send(self):
        """有空位时**同步**发出（不引入微任务跳变）—— 既有"pick 后立刻发"的
        时序是多条既有测试与用户观感的基础。"""
        out = _probe(
            """
__quota.active = 2;
__quota.limit = 3;
pick(["a.pdf"]);
console.log(JSON.stringify({ sentBeforeFlush: __xhr.sent.length }));
"""
        )
        assert out["sentBeforeFlush"] == 1, (
            "有空位却要等微任务才发 —— 快路径被破坏（既有断言会一起变脆）"
        )

    def test_batch_of_three_within_quota_is_all_synchronous(self):
        out = _probe(
            """
__quota.active = 0;
__quota.limit = 3;
pick(["a.pdf", "b.pdf", "c.pdf"]);
__xhr.flush();
console.log(JSON.stringify({ sent: __xhr.sent.length }));
"""
        )
        assert out["sent"] == 3, f"额度内 3 份应全部发出，实际 {out['sent']}"


# ─────────────────────────────────────────────────────────────────────
# 2. 满额：**不发请求**（不再白传），并给出 info 级可操作提示
# ─────────────────────────────────────────────────────────────────────
class TestAtCapacityDoesNotWasteAnUpload:
    def test_no_request_is_sent_while_at_capacity(self):
        """本文件的核心断言：满额时**一个字节都不发**。"""
        out = _probe(
            """
__quota.active = 3;
__quota.limit = 3;
pick(["a.pdf", "b.pdf"]);
await drive(2);
console.log(JSON.stringify({ sent: __xhr.sent.length, status: text() }));
"""
        )
        assert out["sent"] == 0, (
            f"满额仍发了 {out['sent']} 次请求 —— 用户会白传整个文件再吃 409"
        )
        assert "3/3" in out["status"], f"提示未说明额度占用：{out['status']!r}"

    def test_waiting_status_is_informational_not_an_error(self):
        """等待是**正常容量状态**，不是错误 —— 不能报红吓用户。"""
        out = _probe(
            """
__quota.active = 3;
__quota.limit = 3;
pick(["a.pdf", "b.pdf"]);
await drive(2);
console.log(JSON.stringify({ isInfo: isInfo(), isErr: isErr(), status: text() }));
"""
        )
        assert out["isErr"] is False, f"等待被报成错误：{out['status']!r}"
        assert out["isInfo"] is True

    def test_waiting_status_names_the_pending_file_and_position(self):
        out = _probe(
            """
__quota.active = 3;
__quota.limit = 3;
pick(["a.pdf", "b.pdf"]);
await drive(2);
console.log(JSON.stringify({ status: text() }));
"""
        )
        assert "a.pdf" in out["status"], out["status"]
        assert "1/2" in out["status"], out["status"]

    def test_upload_proceeds_once_a_slot_frees(self):
        """等空位 → 空位出现 → 请求发出（而不是一直等下去）。"""
        out = _probe(
            """
__quota.active = 3;
__quota.limit = 3;
pick(["a.pdf"]);
await drive(1);
const mid = __xhr.sent.length;
__quota.active = 2;          // 某个 job 完成，空出一个名额
await drive(3);
console.log(JSON.stringify({ mid: mid, after: __xhr.sent.length, status: text() }));
"""
        )
        assert out["mid"] == 0, f"满额期间不该发请求，实际 {out['mid']}"
        assert out["after"] == 1, f"空位出现后应发出请求，实际 {out['after']}"

    def test_batch_resumes_and_sends_the_rest_after_a_slot_frees(self):
        out = _probe(
            """
__quota.active = 3;
__quota.limit = 3;
pick(["a.pdf", "b.pdf", "c.pdf"]);
await drive(1);
__quota.active = 0;          // 额度全部释放
await drive(2);
__xhr.flush();
await drive(2);
console.log(JSON.stringify({ sent: __xhr.sent.length }));
"""
        )
        assert out["sent"] == 3, f"空位释放后应把 3 份都发出去，实际 {out['sent']}"

    def test_wait_timeout_on_a_single_file_reports_the_timeout(self):
        out = _probe(
            """
__quota.active = 3;
__quota.limit = 3;
pick(["a.pdf"]);
await drive(1);
__advanceClock(31 * 60 * 1000);   // 越过 30min 上界
await drive(3);
console.log(JSON.stringify({ sent: __xhr.sent.length, status: text(), isErr: isErr() }));
"""
        )
        assert out["sent"] == 0, f"超时不该发出任何请求，实际 {out['sent']}"
        assert out["isErr"] is True, "超时是必须让用户看见的失败，不能是 info"
        assert "超时" in out["status"], out["status"]

    def test_batch_after_a_timeout_does_not_wait_again_per_file(self):
        """超时**熔断**：额度是全局的，一轮超时后剩余每一份都会撞同一堵墙。

        若不熔断，3 份 = 3×30min 逐个扣住（用户只能干等 1.5 小时）。

        ⚠️ **判别信号的选择**（本仓库踩过）：**不能**只断言"时钟只前进了一轮"。
        假时钟只在测试显式 `__advanceClock` 时才走，所以"每份各等 30min"在时钟上
        **看不出来** —— 该断言恒真，是空断言（变异验证会报 MISSED，而实际是
        断言缺口，不是护栏有牙）。能真正区分的是**有没有新排定时器**：熔断生效
        时余下两份连 `sleep` 都不排；熔断失效时每份都会排一个新的 2s 轮询。
        """
        out = _probe(
            """
__quota.active = 3;
__quota.limit = 3;
const t0 = __clock.now;
const countTimers = () => __timers.filter((t) => t.kind === "timeout").length;
const timersBefore = countTimers();
pick(["a.pdf", "b.pdf", "c.pdf"]);
await drive(1);
__advanceClock(31 * 60 * 1000);   // 第一份等超时
const timersAtTimeout = countTimers();
await drive(6);                    // 余下两份应**立刻**失败，不再各排一轮
console.log(JSON.stringify({
  sent: __xhr.sent.length,
  waitedMs: __clock.now - t0,
  timersBefore: timersBefore,
  newTimersAfterTimeout: countTimers() - timersAtTimeout,
  status: text(),
  isErr: isErr(),
}));
"""
        )
        assert out["sent"] == 0, f"整批都不该发请求，实际 {out['sent']}"
        assert out["timersBefore"] == 0, (
            f"pick 之前就有 {out['timersBefore']} 个定时器 —— 计数口径不可信"
        )
        assert out["newTimersAfterTimeout"] == 0, (
            f"超时后余下文件又排了 {out['newTimersAfterTimeout']} 个轮询定时器 —— "
            f"熔断失效，整批 N 份 = N×30min"
        )
        assert out["waitedMs"] == 31 * 60 * 1000, (
            f"时钟前进了 {out['waitedMs']}ms（辅助信号，单独看恒真）"
        )
        assert out["isErr"] is True
        assert "3 份失败" in out["status"], (
            f"整批汇总应如实报告 3 份都未提交：{out['status']!r}"
        )

    def test_new_selection_clears_the_timeout_latch(self):
        """一次超时不该永久锁死上传（额度是动态的）。

        断言落在**熔断解除**上：新一轮选择后必须重新进入"等待"（info），
        而不是立刻吃上一轮留下的熔断（err）。这是 `submitFiles` 批次边界那行
        重置语句的**唯一**判别信号。
        """
        out = _probe(
            """
__quota.active = 3;
__quota.limit = 3;
pick(["a.pdf"]);
await drive(1);
__advanceClock(31 * 60 * 1000);
await drive(3);
const first = text();
// 仍满额：若熔断没被新一轮选择清掉，这里会**立刻**失败而不是重新等待
pick(["b.pdf"]);
await drive(2);
console.log(JSON.stringify({
  first: first,
  sent: __xhr.sent.length,
  status: text(),
  isErr: isErr(),
  isInfo: isInfo(),
}));
"""
        )
        assert "超时" in out["first"], out["first"]
        assert out["sent"] == 0, "仍满额时不该发请求"
        assert out["isErr"] is False, (
            f"新一轮选择仍吃上一轮的熔断（err）：{out['status']!r} —— "
            f"批次边界没解除熔断"
        )
        assert out["isInfo"] is True, out["status"]

    def test_drop_zone_is_released_after_a_timeout(self):
        out = _probe(
            """
__quota.active = 3;
__quota.limit = 3;
pick(["a.pdf", "b.pdf"]);
await drive(1);
__advanceClock(31 * 60 * 1000);
await drive(3);
console.log(JSON.stringify({ pe: area().style.pointerEvents }));
"""
        )
        assert out["pe"] == "", "超时后上传区必须恢复，否则页面永久不可用"


# ─────────────────────────────────────────────────────────────────────
# 3. 额度 409：不是"上传失败"，且有界自动重试
# ─────────────────────────────────────────────────────────────────────
class TestQuotaRejectionIsNotAnUploadFailure:
    def test_quota_409_is_not_reported_as_upload_failure(self):
        out = _probe(
            """
__xhr.queue.push({ status: 409, body: { detail: %s } });
pick(["a.pdf"]);
__xhr.flush();
console.log(JSON.stringify({ status: text(), isErr: isErr(), isInfo: isInfo() }));
"""
            % json.dumps(_QUOTA_409)
        )
        assert "上传失败" not in out["status"], (
            f"额度不足被说成「上传失败」（语义错，且吓用户）：{out['status']!r}"
        )
        assert out["isErr"] is False, out["status"]
        assert out["isInfo"] is True, out["status"]

    def test_quota_409_mentions_the_retry_in_progress(self):
        out = _probe(
            """
__xhr.queue.push({ status: 409, body: { detail: %s } });
pick(["a.pdf"]);
__xhr.flush();
console.log(JSON.stringify({ status: text() }));
"""
            % json.dumps(_QUOTA_409)
        )
        assert "重试" in out["status"], out["status"]
        assert "1/2" in out["status"], out["status"]

    def test_quota_409_does_not_ask_the_user_anything(self):
        """额度不足是暂时容量问题，不该弹对话框打断（去重 409 才需要问）。"""
        out = _probe(
            """
__xhr.queue.push({ status: 409, body: { detail: %s } });
pick(["a.pdf"]);
__xhr.flush();
await drive(1);
console.log(JSON.stringify({ confirmCalls: __confirmCalls }));
"""
            % json.dumps(_QUOTA_409)
        )
        assert out["confirmCalls"] == 0, "额度 409 不该弹确认框"

    def test_quota_409_retries_the_same_file_once_a_slot_frees(self):
        out = _probe(
            """
__quota.active = 0;
__quota.limit = 3;
__xhr.queue.push({ status: 409, body: { detail: %s } });
pick(["a.pdf"]);
__xhr.flush();
__quota.active = 3;          // 进入等待：此刻确实没有空位
await drive(2);
const afterFirst = __xhr.sent.length;
__quota.active = 1;          // 空位出现
await drive(3);
console.log(JSON.stringify({
  afterFirst: afterFirst,
  after: __xhr.sent.length,
  urls: __xhr.sent.map((s) => s.url),
}));
"""
            % json.dumps(_QUOTA_409)
        )
        assert out["afterFirst"] == 1, "等待期间不该额外发请求"
        assert out["after"] == 2, f"空位出现后应重传同一份，实际 {out['after']}"
        assert out["urls"][0] == out["urls"][1], "重试的必须是同一个端点/同一份文件"

    def test_quota_409_retry_does_not_stall_the_batch_queue(self):
        """重试成功 ⇒ 队列必须继续推进（`onDone` 只结算一次且不丢）。"""
        out = _probe(
            """
__quota.active = 0;
__quota.limit = 3;
__xhr.queue.push({ status: 409, body: { detail: %s } });
pick(["a.pdf", "b.pdf"]);
__xhr.flushOne();            // 第一份 → 额度 409 → 进入等待
await drive(3);              // 空位可用 → 重传第一份
__xhr.flush();               // 第一份成功 → 队列推进 → 第二份
await drive(2);
console.log(JSON.stringify({ sent: __xhr.sent.length, status: text() }));
"""
            % json.dumps(_QUOTA_409)
        )
        assert out["sent"] == 3, (
            f"重试后队列没继续（应为 第一份 + 重传第一份 + 第二份 = 3），实际 {out['sent']}"
        )

    def test_quota_409_gives_up_after_bounded_retries_with_an_error(self):
        """有界：不能无限重传（每次都重发整个文件，用户等不起）。"""
        out = _probe(
            """
__quota.active = 0;
__quota.limit = 3;
for (let i = 0; i < 6; i++) {
  __xhr.queue.push({ status: 409, body: { detail: %s } });
}
pick(["a.pdf"]);
await drive(1);
__xhr.flush();               // 第 1 次 409 → 等位 → 重试 1
await drive(3);
__xhr.flush();               // 第 2 次 409 → 等位 → 重试 2
await drive(3);
__xhr.flush();               // 第 3 次 409 → 超过上限 → 报错
await drive(3);
console.log(JSON.stringify({
  sent: __xhr.sent.length, status: text(), isErr: isErr(),
}));
"""
            % json.dumps(_QUOTA_409)
        )
        assert out["isErr"] is True, f"重试耗尽必须报错：{out['status']!r}"
        assert "重试" in out["status"], out["status"]
        assert out["sent"] <= 4, f"重试次数失控（发了 {out['sent']} 次）"

    def test_dedup_409_still_routes_to_the_confirmation_dialog(self):
        """回归：两个 409 语义不同，**不能合并**。

        去重 409 要问用户（重传会重新完整分析）；额度 409 不该问。
        合并任一方向都会造成真实伤害：去重不问 ⇒ 用户以为重复上传成功；
        额度要问 ⇒ 每次撞额度都弹框打断。
        """
        out = _probe(
            """
__xhr.queue.push({ status: 409, body: { detail: %s } });
pick(["a.pdf"]);
__xhr.flush();
await drive(1);
console.log(JSON.stringify({
  confirmCalls: __confirmCalls,
  status: text(),
  isErr: isErr(),
}));
"""
            % json.dumps(_DEDUP_409)
        )
        assert out["confirmCalls"] == 1, "去重 409 必须弹确认框（回归被破坏）"
        assert "重试" not in out["status"], "去重 409 不该走额度重试路径"

    def test_quota_recognizer_does_not_swallow_a_dedup_409(self):
        """**识别器的排他性**：额度识别器绝不能匹配去重文案。

        为什么直接对识别器求值（而不是行为驱动）：去重分支在额度分支**之前**，
        且带 `!force` 守卫 ⇒ 把两个识别器合并成一个，在 force=false 的行为路径
        上**完全不可观测**（那是结构性防线）。故这类"合并两个 409 语义"的回归
        只能在识别器这一层拦住 —— 直接对**生产源码里的那个函数**求值，
        两个方向的放宽（`|已上传过`、`/已上传/` 之类）都会被抓住。

        这里不新增生产代码：从 `static/upload.js` 切出 `isQuotaRejection` 的
        源码原文交给 node 求值，断言的是**生产实现**而不是副本。
        """
        src = UPLOAD_JS.read_text(encoding="utf-8")
        i = src.index("function isQuotaRejection")
        j = src.index("\n  }", i) + len("\n  }")
        recognizer = src[i:j]
        assert "return /" in recognizer, (
            f"没抽到识别器实现（源码形状变了？）—— 本用例会退化成空断言：\n{recognizer}"
        )
        out = run_js(
            [],
            recognizer
            + "\nconsole.log(JSON.stringify({"
            + " quota: isQuotaRejection(" + json.dumps(_QUOTA_409) + "),"
            + " dedup: isQuotaRejection(" + json.dumps(_DEDUP_409) + "),"
            + " empty: isQuotaRejection(undefined),"
            + " }));",
            use_dom=False,
        )
        assert out["quota"] is True, (
            "额度文案没被识别 —— 额度 409 会掉进通用失败分支（报红 + 丢文件选择）"
        )
        assert out["dedup"] is False, (
            "去重文案被额度识别器匹配了 —— 两个 409 语义被合并"
            "（用户点「仍要重新上传」时会看到额度重试文案）"
        )
        assert out["empty"] is False, "detail 缺失时必须安全返回 false（不能抛）"

    def test_other_4xx_still_reports_a_plain_failure(self):
        """负向对照：非 409 的失败路径不变（别把额度分支做成万能吞错）。"""
        out = _probe(
            """
__xhr.queue.push({ status: 500, body: { detail: "后端炸了" } });
pick(["a.pdf"]);
__xhr.flush();
console.log(JSON.stringify({ status: text(), isErr: isErr() }));
"""
        )
        assert out["isErr"] is True
        assert "上传失败" in out["status"], out["status"]
        assert "后端炸了" in out["status"], "后端 detail 必须透出"


# ─────────────────────────────────────────────────────────────────────
# 4. 发布侧：upload-jobs.js 的活跃数口径
# ─────────────────────────────────────────────────────────────────────
class TestQuotaPublisherContract:
    def test_registration_precedes_the_render_short_circuit(self):
        """登记必须早于渲染循环的短路。

        `/api/jobs/live` 的推送体**还含近 10 分钟内的终态 job**（行状态切换
        需要）—— 登记若放在 `if (!li) return` 之后，当前页没有该行的活跃 job
        会被漏计 ⇒ active 偏低 ⇒ 预检放行 ⇒ 又白传一次。

        ⚠️ 这里只钉**顺序**。活跃数的**口径**（只数 ACTIVE_STATUSES）由
        `test_upload_jobs_js.py::test_quota_active_counts_only_active_statuses`
        做**行为**断言 —— 源码子串断言在本文件里是**假绿源**：同文件还有
        2 处 `ACTIVE_STATUSES.includes(...)`，删掉 `recountQuota` 里的那处
        子串断言照样成立（变异验证实测 MISSED）。
        """
        src = UPLOAD_JOBS_JS.read_text(encoding="utf-8")
        i_set = src.index("liveStatuses.set(snap.id, snap.status)")
        i_render_guard = src.index("const li = findJobRow(snap.id);")
        assert i_set < i_render_guard, (
            "额度登记必须早于行渲染短路 —— 否则当前页没有该行的活跃 job 会被漏计"
        )
        assert "function recountQuota" in src

    def test_publisher_is_published_under_a_stable_name(self):
        src = UPLOAD_JOBS_JS.read_text(encoding="utf-8")
        assert "window.PbcJobs = window.PbcJobs || {};" in src
        assert "window.PbcJobs.quota = function ()" in src

    def test_fallback_polling_also_updates_the_registry(self):
        """SSE 断线降级后 active 不能冻结 —— 否则有 job 跑完也永远"满额"。"""
        src = UPLOAD_JOBS_JS.read_text(encoding="utf-8")
        i_fallback = src.index("function startFallbackPolling")
        i_end = src.index("function updateJobRowLive")
        block = src[i_fallback:i_end]
        assert "liveStatuses.set(jid, d.status)" in block, (
            "降级轮询路径没有维护额度登记表 —— active 会冻结在最后一次 SSE 帧"
        )
        assert "recountQuota()" in block
