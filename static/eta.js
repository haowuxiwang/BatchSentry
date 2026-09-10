/* Stage 2 分析进度 ETA（round-23 B）— upload.js / review.js 共享纯函数。
 *
 * 背景：15 分钟级的 Stage 2 逐页 LLM 分析此前只有 "分析 x/y" 计数，
 * 用户无法判断"还要等多久 / 是不是卡死了"。SSE 快照已带 pages_analyzed
 * （3s 推送），速率与剩余时间纯前端可算，无需后端改动。
 *
 * 采样窗口设计：上游 LLM 排队抖动大（拥堵日单页排队 500-1000s），
 * 全程平均速率会把 ETA 拉爆且收敛极慢 — 用 5 分钟时间窗口内的
 * (pages_analyzed, t) 采样序列算速率，跟随近期实际速率。
 *
 * 纯函数（无 DOM/无 Date.now 依赖，nowMs 注入）— node 单测锁定
 * （tests/unit/test_eta_js.py）。 */
(function (global) {
  "use strict";

  var WINDOW_MS = 5 * 60 * 1000; // 速率采样窗口
  var MIN_SPAN_MS = 8000; // 窗口内首尾跨度下限（防两帧同秒抖动误算速率）
  var MAX_SAMPLES = 240; // 采样上限（3s 一帧 ≈ 12 分钟，足够覆盖窗口）

  /** 从采样序列算 {done, total, pct, rate, etaSec}。
   *
   * samples: [{n: pages_analyzed, t: epoch_ms}] 升序追加；
   * total: 总页数（<=0 或样本不足返回 null — 调用方退回纯计数显示）。
   * 速率单位：页/秒；rate/etaSec 为 null 表示"暂不可估"（样本太密 /
   * 窗口内零进展 — 前者等几帧，后者说明上游真卡住，显示计数不加 ETA）。
   */
  function analyzeEta(samples, total, nowMs) {
    if (!Array.isArray(samples) || samples.length < 2 || !total || total <= 0) {
      return null;
    }
    var now = nowMs == null ? Date.now() : nowMs;
    // 窗口基线 = 窗口内首个样本的前一个（跨窗口边界的样本作起点），
    // 否则稀疏采样（两帧间隔 > 5 分钟）会把基线整帧丢掉返回 null。
    var firstIn = -1;
    for (var i = 0; i < samples.length; i++) {
      if (now - samples[i].t <= WINDOW_MS) {
        firstIn = i;
        break;
      }
    }
    var start;
    if (firstIn < 0) start = samples.length - 1; // 全部过期：最新样本兜底
    else if (firstIn > 0) start = firstIn - 1;
    else start = 0;
    var recent = samples.slice(start);
    if (recent.length < 2) return null;
    var first = recent[0];
    var last = recent[recent.length - 1];
    var done = last.n;
    if (done >= total) {
      return { done: done, total: total, pct: 100, rate: 0, etaSec: 0 };
    }
    var dp = done - first.n;
    var dt = last.t - first.t;
    if (dt < MIN_SPAN_MS || dp <= 0) {
      return {
        done: done,
        total: total,
        pct: Math.round((done / total) * 100),
        rate: null,
        etaSec: null,
      };
    }
    var rate = dp / (dt / 1000);
    var etaSec = Math.round((total - done) / rate);
    return {
      done: done,
      total: total,
      pct: Math.round((done / total) * 100),
      rate: rate,
      etaSec: etaSec,
    };
  }

  /** 采样追加（封顶防泄漏）：n 回退（job 重跑复用行）时清空重采。 */
  function pushSample(samples, n) {
    var t = Date.now();
    if (samples.length && n < samples[samples.length - 1].n) {
      samples.length = 0;
    }
    samples.push({ n: n, t: t });
    if (samples.length > MAX_SAMPLES) {
      samples.splice(0, samples.length - MAX_SAMPLES);
    }
  }

  /** ETA 文案：<60s "≈1 分钟内"；<90 分钟 "约 N 分钟"；此后 "约 H 小时
   * (M 分)"；≥99 小时 "99+ 小时"。null/非有限值返回 ""（不加该段）。 */
  function fmtEta(sec) {
    if (sec == null || !isFinite(sec) || sec < 0) return "";
    if (sec < 60) return "≈1 分钟内";
    var m = Math.round(sec / 60);
    if (m < 90) return "约 " + m + " 分钟";
    var h = Math.floor(m / 60);
    if (h >= 99) return "99+ 小时";
    if (m % 60 === 0) return "约 " + h + " 小时";
    return "约 " + h + " 小时 " + (m % 60) + " 分";
  }

  /** 速率文案：页/分钟，一位小数；null 返回 ""。 */
  function fmtRate(rate) {
    if (rate == null || !isFinite(rate) || rate <= 0) return "";
    var perMin = rate * 60;
    return (perMin >= 10 ? Math.round(perMin) : perMin.toFixed(1)) + " 页/分";
  }

  global.PbcEta = {
    analyzeEta: analyzeEta,
    pushSample: pushSample,
    fmtEta: fmtEta,
    fmtRate: fmtRate,
    WINDOW_MS: WINDOW_MS,
    MIN_SPAN_MS: MIN_SPAN_MS,
    MAX_SAMPLES: MAX_SAMPLES,
  };
})(typeof window !== "undefined" ? window : globalThis);
