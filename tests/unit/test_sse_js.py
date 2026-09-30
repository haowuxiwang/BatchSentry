"""PbcSse 连接骨架的 node 实跑单测（R63 P1-C 收敛的安全网）。

覆盖两种模式与两类错误帧的全部处置语义（与 review.js / upload.js 被
替换前的行为逐一对齐）：

manual（复核页单任务流）：
  - onerror → 立即 close + 指数退避 2s/4s/8s 手动重连；
  - onopen 重置退避序列；
  - 重试耗尽 → fallback 恰好一次，此后不再自愈；
  - done 事件 → 关流不重连；
  - terminal 错误帧 → 关流；transient 错误帧 → 保持长连。

auto（上传页聚合流）：
  - onerror 不 close（EventSource 内建重连），只计数；
  - 任何成功帧重置计数（cr-19）；
  - 连续达阈值 → close + fallback 一次。

实现：node 里注入 FakeEventSource 与假定时器（收集回调手动触发），
断言落在「close 调用时机 / 重连序列 / 回调次数」等行为上，可被变异打红。
node 不可用时整体跳过（打包链不依赖）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SSE_JS = REPO / "static" / "sse.js"

# 测试驱动（node 侧）：FakeEventSource + 假定时器 + 断言收集器
HARNESS = r"""
// ── 假定时器：收集回调，手动 flush ──
var timers = {};
var timerSeq = 0;
globalThis.setTimeout = function (fn, delay) {
  var id = ++timerSeq;
  timers[id] = { fn: fn, delay: delay };
  return id;
};
globalThis.clearTimeout = function (id) { delete timers[id]; };
function pendingDelays() {
  return Object.values(timers).map(function (t) { return t.delay; });
}
function flushTimers() {
  var fns = Object.values(timers).map(function (t) { return t.fn; });
  timers = {};
  fns.forEach(function (fn) { fn(); });
}

// ── FakeEventSource ──
function FakeEventSource(url) {
  this.url = url;
  this.closed = false;
  this.listeners = {};
  FakeEventSource.all.push(this);
}
FakeEventSource.all = [];
FakeEventSource.prototype.close = function () { this.closed = true; };
FakeEventSource.prototype.addEventListener = function (type, fn) {
  (this.listeners[type] = this.listeners[type] || []).push(fn);
};
FakeEventSource.prototype.emit = function (type, payload) {
  if (type === "message") {
    if (this.onmessage) this.onmessage({ data: JSON.stringify(payload) });
  } else if (type === "error") {
    if (this.onerror) this.onerror({});
  } else if (type === "open") {
    if (this.onopen) this.onopen();
  } else {
    var ls = this.listeners[type] || [];
    for (var i = 0; i < ls.length; i++) ls[i]({ data: payload });
  }
};

// ── 断言收集 ──
var events = [];
globalThis.addEventListener = function () {}; // beforeunload no-op

var PbcSse = globalThis.PbcSse;
"""


def _run_node(script: str):
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "sse_probe.js"
        p.write_text(
            SSE_JS.read_text(encoding="utf-8") + "\n" + HARNESS + "\n" + script,
            encoding="utf-8",
        )
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    out = r.stdout.strip().splitlines()
    assert out, "node 无输出"
    return json.loads(out[-1])


class TestManualMode:
    """复核页单任务流的语义（对齐被替换的 review.js subscribeProgress）。"""

    def test_error_triggers_exponential_backoff_sequence(self):
        """onerror → 立即 close → 2s/4s/8s 退避重连（旧代码逐字行为）。"""
        out = _run_node(r"""
var events = [];
var h = PbcSse.create({
  url: "/api/jobs/j1/stream", mode: "manual", maxRetries: 3,
  EventSource: FakeEventSource,
  onRetry: function (n, d) { events.push(["retry", n, d]); },
  fallback: function () { events.push(["fallback"]); },
});
// 三次连续失败（每次 onerror 后需 flush 退避 timer 才会真正重连）
for (var i = 0; i < 3; i++) {
  FakeEventSource.all[FakeEventSource.all.length - 1].emit("error");
  flushTimers();
}
console.log(JSON.stringify({
  events: events,
  instances: FakeEventSource.all.length,
  closedCount: FakeEventSource.all.filter(function (e) { return e.closed; }).length,
}));
""")
        # 退避序列 2s/4s/8s（base=2000，指数）
        assert [e for e in out["events"] if e[0] == "retry"] == [
            ["retry", 1, 2000], ["retry", 2, 4000], ["retry", 3, 8000],
        ]
        # 3 次失败共 4 个实例（初始 + 3 次重连）：前 3 个（出错的）都被
        # 立即 close，第 4 个是最后一次重连成功建立的连接 —— 未出错，
        # 不应被关（manual 模式 onerror 即 close 的是**废弃连接**）。
        assert out["instances"] == 4
        assert out["closedCount"] == 3
        assert ["fallback"] not in out["events"]

    def test_onopen_resets_backoff(self):
        """重连成功（onopen）后退避序列从头开始（旧代码 onopen 重置）。"""
        out = _run_node(r"""
var events = [];
PbcSse.create({
  url: "/u", mode: "manual", maxRetries: 3, EventSource: FakeEventSource,
  onRetry: function (n, d) { events.push(["retry", n, d]); },
  fallback: function () { events.push(["fallback"]); },
});
// 失败一次 → 退避 2s → flush 重连 → onopen 重置 → 再失败两次
var es1 = FakeEventSource.all[FakeEventSource.all.length - 1];
es1.emit("error");
flushTimers(); // 触发重连
var es2 = FakeEventSource.all[FakeEventSource.all.length - 1];
es2.emit("open"); // onopen（FakeEventSource 没自动触发，这里手动）
es2.emit("error");
flushTimers();
console.log(JSON.stringify({ events: events }));
""")
        # 第二轮失败后退避应从 2000 重新开始（而非 4000）
        assert out["events"] == [["retry", 1, 2000], ["retry", 1, 2000]]

    def test_retries_exhausted_falls_back_exactly_once(self):
        """3 次重试耗尽 → fallback 恰一次，且不再自愈（继续 onerror 无效）。"""
        out = _run_node(r"""
var fallbacks = 0;
PbcSse.create({
  url: "/u", mode: "manual", maxRetries: 3, EventSource: FakeEventSource,
  fallback: function () { fallbacks++; },
});
for (var i = 0; i < 5; i++) {
  var es = FakeEventSource.all[FakeEventSource.all.length - 1];
  es.emit("error");
  flushTimers(); // 耗尽前每次都触发重连
}
console.log(JSON.stringify({
  fallbacks: fallbacks,
  instances: FakeEventSource.all.length,
}));
""")
        assert out["fallbacks"] == 1
        # 初始 1 + 重连 3 = 4 个实例；耗尽后的额外 emit 不再新建连接
        assert out["instances"] == 4

    def test_done_event_closes_without_reconnect(self):
        out = _run_node(r"""
var dones = 0;
PbcSse.create({
  url: "/u", mode: "manual", EventSource: FakeEventSource,
  onDone: function () { dones++; },
});
var es = FakeEventSource.all[0];
es.emit("done", "{}");
// done 之后继续 onerror —— 不应产生任何重连
es.emit("error");
flushTimers();
console.log(JSON.stringify({
  dones: dones, closed: es.closed,
  instances: FakeEventSource.all.length,
}));
""")
        assert out["dones"] == 1
        assert out["closed"] is True
        assert out["instances"] == 1

    def test_terminal_error_frame_closes_and_stops(self):
        out = _run_node(r"""
globalThis.PbcStatus = {
  sseErrorAction: function (d) { return d.terminal === true ? "terminal" : "transient"; },
};
var terminal = [], transients = [];
PbcSse.create({
  url: "/u", mode: "manual", EventSource: FakeEventSource,
  onTerminalError: function (d) { terminal.push(d.message); },
  onTransientError: function (d) { transients.push(d.message); },
});
var es = FakeEventSource.all[0];
es.emit("message", { type: "error", terminal: true, message: "任务不存在" });
// terminal 后 onerror 不再引发重连
es.emit("error");
flushTimers();
console.log(JSON.stringify({
  terminal: terminal, transients: transients, closed: es.closed,
  instances: FakeEventSource.all.length,
}));
""")
        assert out["terminal"] == ["任务不存在"]
        assert out["transients"] == []
        assert out["closed"] is True
        assert out["instances"] == 1

    def test_transient_error_frame_keeps_stream(self):
        """瞬态错误帧（terminal=false）不关流，后续正常帧照常到达。"""
        out = _run_node(r"""
var transients = [], frames = [];
PbcSse.create({
  url: "/u", mode: "manual", EventSource: FakeEventSource,
  onTransientError: function (d) { transients.push(d.message); },
  onFrame: function (d) { frames.push(d.status); },
});
var es = FakeEventSource.all[0];
es.emit("message", { type: "error", terminal: false, message: "进度查询失败" });
es.emit("message", { status: "analyzing", pages_analyzed: 3 });
console.log(JSON.stringify({
  transients: transients, frames: frames, closed: es.closed,
}));
""")
        assert out["transients"] == ["进度查询失败"]
        assert out["frames"] == ["analyzing"]
        assert out["closed"] is False

    def test_transient_frame_does_not_close_pending_poll(self):
        """瞬态分支不得触发 close/fallback（旧代码曾连带清掉轮询兜底）。"""
        out = _run_node(r"""
var fallbacks = 0;
PbcSse.create({
  url: "/u", mode: "manual", EventSource: FakeEventSource,
  fallback: function () { fallbacks++; },
});
var es = FakeEventSource.all[0];
es.emit("message", { type: "error", terminal: false, message: "抖动" });
console.log(JSON.stringify({
  fallbacks: fallbacks, closed: es.closed,
}));
""")
        assert out["fallbacks"] == 0
        assert out["closed"] is False


class TestAutoMode:
    """上传页聚合流的语义（对齐被替换的 upload.js startLiveTracking）。"""

    def test_error_does_not_close_until_threshold(self):
        """连续错误 <3 次不 close（靠 EventSource 内建重连），不建新实例。"""
        out = _run_node(r"""
PbcSse.create({
  url: "/api/jobs/live", mode: "auto", maxRetries: 3,
  EventSource: FakeEventSource,
  fallback: function () { events.push("fallback"); },
});
var events = [];
var es = FakeEventSource.all[0];
es.emit("error"); es.emit("error");
console.log(JSON.stringify({
  closed: es.closed, instances: FakeEventSource.all.length,
}));
""")
        assert out["closed"] is False
        assert out["instances"] == 1

    def test_successful_frame_resets_error_count(self):
        """任何成功帧重置计数（cr-19：内建重连后无 onopen 窗口）。"""
        out = _run_node(r"""
var fallbacks = 0;
PbcSse.create({
  url: "/api/jobs/live", mode: "auto", maxRetries: 3,
  EventSource: FakeEventSource,
  onFrame: function () {},
  fallback: function () { fallbacks++; },
});
var es = FakeEventSource.all[0];
// 错2次 → 正常帧重置 → 再错2次 → 不应降级
es.emit("error"); es.emit("error");
es.emit("message", { jobs: [] });
es.emit("error"); es.emit("error");
console.log(JSON.stringify({
  fallbacks: fallbacks, closed: es.closed,
}));
""")
        assert out["fallbacks"] == 0
        assert out["closed"] is False

    def test_three_consecutive_errors_close_and_fallback_once(self):
        out = _run_node(r"""
var fallbacks = 0;
PbcSse.create({
  url: "/api/jobs/live", mode: "auto", maxRetries: 3,
  EventSource: FakeEventSource,
  fallback: function () { fallbacks++; },
});
var es = FakeEventSource.all[0];
es.emit("error"); es.emit("error"); es.emit("error");
// 降级后再 emit —— 不得二次 fallback
es.emit("error");
console.log(JSON.stringify({
  fallbacks: fallbacks, closed: es.closed,
}));
""")
        assert out["fallbacks"] == 1
        assert out["closed"] is True


class TestManualClose:
    def test_page_can_close_stream(self):
        """页面主动 close（如 beforeunload 复用同一入口）后不再重连。"""
        out = _run_node(r"""
var h = PbcSse.create({
  url: "/u", mode: "manual", EventSource: FakeEventSource,
  fallback: function () { events.push("fallback"); },
});
var events = [];
h.close();
var es = FakeEventSource.all[0];
es.emit("error");
flushTimers();
console.log(JSON.stringify({
  isClosed: h.isClosed(), closed: es.closed,
  instances: FakeEventSource.all.length, events: events,
}));
""")
        assert out["isClosed"] is True
        assert out["closed"] is True
        assert out["instances"] == 1
        assert out["events"] == []

    def test_pending_retry_timer_cancelled_by_close(self):
        """退避等待中的 timer 被 close 清掉 —— 不得在卸载后开新连接。"""
        out = _run_node(r"""
var h = PbcSse.create({
  url: "/u", mode: "manual", EventSource: FakeEventSource,
});
var es = FakeEventSource.all[0];
es.emit("error"); // 进入 2s 退避等待
h.close();
flushTimers(); // 若 timer 未被清理，这里会开第二个连接
console.log(JSON.stringify({
  instances: FakeEventSource.all.length,
}));
""")
        assert out["instances"] == 1


class TestErrorFrameAdjudication:
    """terminal/transient 判定默认走 PbcStatus.sseErrorAction。"""

    def test_default_adjudication_uses_pbc_status(self):
        out = _run_node(r"""
globalThis.PbcStatus = {
  sseErrorAction: function (d) { return d.terminal === true ? "terminal" : "transient"; },
};
var terminal = [], transients = [];
PbcSse.create({
  url: "/u", mode: "manual", EventSource: FakeEventSource,
  onTerminalError: function (d) { terminal.push(d.message); },
  onTransientError: function (d) { transients.push(d.message); },
});
var es = FakeEventSource.all[0];
es.emit("message", { type: "error", terminal: true, message: "A" });
es.emit("message", { type: "error", terminal: false, message: "B" });
console.log(JSON.stringify({ terminal: terminal, transients: transients }));
""")
        # terminal 关流后第二条帧实际到不了（es 已 close，Fake 手动 emit
        # 仍会派发）—— 但实现上 onmessage 在 closed 后直接 return。
        assert out["terminal"] == ["A"]
        assert out["transients"] == []
