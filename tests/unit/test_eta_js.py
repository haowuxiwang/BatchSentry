"""round-23 B2：static/eta.js 纯函数单测（node 驱动）。

Stage 2 ETA 的速率/剩余时间计算是纯函数（nowMs 注入、无 DOM），按项目
"实证驱动"约定用 node 直接执行断言 — Python 侧只做编排与结果校验。
node 不可用时 skip（打包链不依赖该测试，CI 有 node 则跑）。
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ETA_JS = REPO / "static" / "eta.js"

_PREAMBLE = """
let __n = 0;
function eq(a, b, msg) {
  const ja = JSON.stringify(a), jb = JSON.stringify(b);
  if (ja !== jb) throw new Error(`FAIL ${msg}: ${ja} != ${jb}`);
  __n++;
}
"""


def _run_node(body: str):
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "eta_test.js"
        p.write_text(
            ETA_JS.read_text(encoding="utf-8") + "\n" + _PREAMBLE + "\n" + body,
            encoding="utf-8",
        )
        r = subprocess.run(
            ["node", str(p)], capture_output=True, text=True, timeout=30
        )
    return r


class TestAnalyzeEta:
    def test_rate_and_eta_from_window(self):
        """5 页 / 60s、总量 20 → done=5 pct=25% eta=180s。"""
        r = _run_node("""
const e = PbcEta.analyzeEta([{n:0,t:0},{n:5,t:60000}], 20, 70000);
eq(e.done, 5, "done");
eq(e.pct, 25, "pct");
eq(e.etaSec, 180, "eta");
eq(Math.round(e.rate * 1000) / 1000, 0.083, "rate");
""")
        assert r.returncode == 0, r.stderr

    def test_insufficient_inputs_return_null(self):
        r = _run_node("""
eq(PbcEta.analyzeEta([], 20, 0), null, "empty");
eq(PbcEta.analyzeEta([{n:1,t:0}], 20, 0), null, "single");
eq(PbcEta.analyzeEta([{n:1,t:0},{n:2,t:1000}], 0, 2000), null, "no total");
""")
        assert r.returncode == 0, r.stderr

    def test_dense_samples_no_rate(self):
        """首尾跨度 < MIN_SPAN_MS（8s）→ rate/etaSec=null 但 pct 照常。"""
        r = _run_node("""
const e = PbcEta.analyzeEta([{n:1,t:0},{n:2,t:3000},{n:3,t:6000}], 10, 6000);
eq(e.rate, null, "rate null");
eq(e.etaSec, null, "eta null");
eq(e.pct, 30, "pct");
""")
        assert r.returncode == 0, r.stderr

    def test_zero_progress_in_window(self):
        """窗口内 pages_analyzed 无增长（上游卡住）→ 不给虚假 ETA。"""
        r = _run_node("""
const e = PbcEta.analyzeEta([{n:3,t:0},{n:3,t:30000}], 10, 30000);
eq(e.rate, null, "rate null");
eq(e.pct, 30, "pct");
""")
        assert r.returncode == 0, r.stderr

    def test_old_samples_outside_window_excluded(self):
        """窗口外旧样本不参与速率（拥堵日早期排队不失真 ETA）— 语义：
        窗口外样本至多 1 个作基线（稀疏采样兜底），更早的整段排除。"""
        r = _run_node("""
const s = [{n:5,t:0},{n:6,t:1000},{n:7,t:2000},
           {n:20,t:301000},{n:21,t:302000}];
const e = PbcEta.analyzeEta(s, 31, 302000);
eq(e.done, 21, "done");
// 基线 = 窗口内首个样本(t=2000)的前一个(t=1000, n=6)；
// t=0 的样本(n=5)不参与 — 若全量参与 rate 会是 16/302。
eq(e.rate, 15 / 301, "rate with one pre-window baseline");
eq(e.etaSec, Math.round(10 / (15 / 301)), "eta");
""")
        assert r.returncode == 0, r.stderr

    def test_sparse_samples_use_predecessor_baseline(self):
        """稀疏采样（两帧间隔 > 窗口）：前帧作基线而非整帧丢弃返回 null。"""
        r = _run_node("""
const e = PbcEta.analyzeEta([{n:0,t:0},{n:3,t:600000}], 10, 600000);
eq(e.done, 3, "done");
eq(e.rate, 3 / 600, "rate over full sparse span");
eq(e.etaSec, Math.round(7 / (3 / 600)), "eta");
""")
        assert r.returncode == 0, r.stderr

    def test_completion(self):
        """done >= total → pct=100 / etaSec=0（终帧不显示残余 ETA）。"""
        r = _run_node("""
const e = PbcEta.analyzeEta([{n:0,t:0},{n:20,t:600000}], 20, 600000);
eq(e.pct, 100, "pct");
eq(e.etaSec, 0, "eta");
""")
        assert r.returncode == 0, r.stderr


class TestFmt:
    def test_fmt_eta_boundaries(self):
        r = _run_node("""
eq(PbcEta.fmtEta(null), "", "null");
eq(PbcEta.fmtEta(55), "≈1 分钟内", "sub-minute");
eq(PbcEta.fmtEta(90), "约 2 分钟", "minutes round");
eq(PbcEta.fmtEta(3600), "约 60 分钟", "60min stays minutes");
eq(PbcEta.fmtEta(3660), "约 61 分钟", "61min stays minutes");
eq(PbcEta.fmtEta(5400), "约 1 小时 30 分", "hour+min");
eq(PbcEta.fmtEta(7200), "约 2 小时", "whole hours");
eq(PbcEta.fmtEta(400000), "99+ 小时", "cap");
eq(PbcEta.fmtEta(-5), "", "negative");
""")
        assert r.returncode == 0, r.stderr

    def test_fmt_rate(self):
        r = _run_node("""
eq(PbcEta.fmtRate(null), "", "null");
eq(PbcEta.fmtRate(0), "", "zero");
eq(PbcEta.fmtRate(1 / 60), "1.0 页/分", "1/min");
eq(PbcEta.fmtRate(10 / 60), "10 页/分", "10/min integer");
""")
        assert r.returncode == 0, r.stderr


class TestPushSample:
    def test_append_regression_reset_and_cap(self):
        """正常追加 / n 回退清池重采（job 重跑）/ 采样封顶防泄漏。"""
        r = _run_node("""
const arr = [];
const orig = Date.now;
let fake = 1000;
Date.now = () => fake;
PbcEta.pushSample(arr, 3);
fake += 3000;
PbcEta.pushSample(arr, 4);
eq(arr, [{n:3,t:1000},{n:4,t:4000}], "append");
PbcEta.pushSample(arr, 1); // 回退 → 清池重采
eq(arr, [{n:1,t:4000}], "reset on regression");
const big = [];
for (let i = 0; i < PbcEta.MAX_SAMPLES + 10; i++) {
  fake += 1000;
  PbcEta.pushSample(big, i);
}
eq(big.length, PbcEta.MAX_SAMPLES, "capped");
Date.now = orig;
""")
        assert r.returncode == 0, r.stderr
