"""review-progress.js 轮询兜底的**行为**护栏（SSE 重试耗尽后的恢复路径）。

**要防的失效模式**（对抗审查发现）：SSE 重试耗尽后降级为 10s 轮询
`/api/jobs/{id}`。旧实现里 `if (!r.ok) return;` 与 `catch { log.warn }`
都是**静默**的 —— 后端持续不可用时页面会每 10s 无反馈地重试到永远：
既没有终态、也不给用户任何提示，定时器与连接永不释放，用户只能干等。

本护栏用 stub 驱动真实回调（不 mock 业务逻辑），锁三件事：

1. **有界**：连续失败达阈值 ⇒ 停止轮询 + 显示可操作提示（不再无限重试）；
2. **不过早**：失败未达阈值时不得停（否则一次抖动就永久失去自动刷新）；
3. **能恢复**：中途成功一次即清零计数（偶发抖动不累积成"连续失败"）。

另含正向对照：终态响应必须停止轮询并触发自动刷新 —— 证明 harness
真的驱动了 `fallback`（而非空转）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PROGRESS_JS = REPO / "static" / "review-progress.js"

# 驱动真实回调所需的最小浏览器环境（DOM/定时器/fetch/EventSource 全 stub）。
# ⚠️ `globalThis.window = globalThis` 必须显式设置 —— review-progress.js 内
# 使用裸 `window.addEventListener`，node 下无 window 会 ReferenceError。
_HARNESS = r"""
globalThis.window = globalThis;
const __intervals = [];
globalThis.setInterval = (fn, ms) => { const id = { fn, ms, cleared: false }; __intervals.push(id); return id; };
globalThis.clearInterval = (id) => { if (id) id.cleared = true; };
globalThis.setTimeout = () => 0;
globalThis.addEventListener = () => {};
globalThis.EventSource = function () {};
const __els = {};
function __el(id) {
  return __els[id] || (__els[id] = {
    id, textContent: "", dataset: {}, style: {},
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    querySelector: () => null, querySelectorAll: () => [],
  });
}
globalThis.document = { getElementById: __el, querySelector: () => null,
  querySelectorAll: () => [], addEventListener: () => {}, body: { style: {} } };
globalThis.PbcStatus = { statusZh: (s) => s, statusDotClass: () => "", sseErrorAction: () => null };
globalThis.PbcSse = { create: (o) => { globalThis.__captured = o; return { close(){}, isClosed(){ return false; } }; } };
globalThis.PbcReview = {
  log: Object.assign(function(){}, { warn(){}, err(){} }),
  state: { currentPage: 1, totalPages: 0 },
  syncNavButtons(){},
  safeAutoReload(){ globalThis.__reloaded = true; },
};
"""


def _drive(probe: str):
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    src = (
        _HARNESS
        + "\n"
        + PROGRESS_JS.read_text(encoding="utf-8")
        + "\n(async () => {\n"
        + probe
        + "\n})();\n"
    )
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "probe.js"
        p.write_text(src, encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


# 公共前置：订阅 → 取出 fallback 回调 → 手动起轮询并拿到 interval 句柄
_PRELUDE = r"""
const R = globalThis.PbcReview;
R.progress.subscribe("j1");
const opts = globalThis.__captured;
if (!opts || typeof opts.fallback !== "function") {
  console.log(JSON.stringify({ error: "fallback 回调未被捕获 —— harness 失效" }));
  return;
}
function startPolling() {
  __intervals.length = 0;
  opts.fallback();
  return __intervals.filter((i) => i.ms === 10000).pop();
}
const okNonTerminal = async () => ({ ok: true, json: async () => ({ status: "analyzing" }) });
const okTerminal = async () => ({ ok: true, json: async () => ({ status: "review" }) });
const notOk = async () => ({ ok: false, status: 503 });
const netErr = async () => { throw new Error("network down"); };
"""


class TestFallbackPollingIsBounded:
    def test_harness_is_not_vacuous(self):
        out = _drive(_PRELUDE + "\nconsole.log(JSON.stringify({ captured: !!opts.fallback }));")
        assert out == {"captured": True}

    def test_consecutive_failures_stop_polling_with_message(self):
        out = _drive(
            _PRELUDE
            + """
const fb = startPolling();
globalThis.fetch = notOk;
for (let k = 0; k < 6; k++) await fb.fn();
console.log(JSON.stringify({
  cleared: fb.cleared,
  text: __els["progress-text"].textContent,
}));
"""
        )
        assert out["cleared"] is True, (
            "连续 6 次非 ok 后仍未停止轮询 ⇒ 后端持续不可用时页面会永远"
            "静默重试（无终态、无提示、定时器不释放）"
        )
        assert "连续失败" in out["text"], f"未给出可操作提示：{out['text']!r}"

    def test_network_exceptions_are_counted_too(self):
        """fetch 抛异常（断网）也必须计入连续失败，不能只算 !r.ok。"""
        out = _drive(
            _PRELUDE
            + """
const fb = startPolling();
globalThis.fetch = netErr;
for (let k = 0; k < 6; k++) await fb.fn();
console.log(JSON.stringify({ cleared: fb.cleared }));
"""
        )
        assert out["cleared"] is True

    def test_does_not_stop_before_threshold(self):
        """未达阈值不得停 —— 否则一次抖动就永久失去自动刷新。"""
        out = _drive(
            _PRELUDE
            + """
const fb = startPolling();
globalThis.fetch = notOk;
for (let k = 0; k < 5; k++) await fb.fn();
console.log(JSON.stringify({ cleared: fb.cleared }));
"""
        )
        assert out["cleared"] is False

    def test_success_resets_the_failure_counter(self):
        """中途成功一次即清零：5 次失败 + 1 次成功 + 5 次失败 不应停。

        （若计数不清零，累计 10 ≥ 6 会误停 —— 本用例正是为区分该行为。）
        """
        out = _drive(
            _PRELUDE
            + """
const fb = startPolling();
globalThis.fetch = notOk;
for (let k = 0; k < 5; k++) await fb.fn();
globalThis.fetch = okNonTerminal;
await fb.fn();
globalThis.fetch = notOk;
for (let k = 0; k < 5; k++) await fb.fn();
console.log(JSON.stringify({ cleared: fb.cleared }));
"""
        )
        assert out["cleared"] is False, (
            "失败计数未在成功后清零 —— 偶发抖动会累积成'连续失败'而误停轮询"
        )


class TestFallbackTerminalPath:
    def test_terminal_status_stops_polling_and_reloads(self):
        """正向对照：终态必须停轮询并触发自动刷新（证明 harness 真驱动了回调）。"""
        out = _drive(
            _PRELUDE
            + """
globalThis.__reloaded = false;
const fb = startPolling();
globalThis.fetch = okTerminal;
await fb.fn();
console.log(JSON.stringify({ cleared: fb.cleared, reloaded: !!globalThis.__reloaded }));
"""
        )
        assert out == {"cleared": True, "reloaded": True}
