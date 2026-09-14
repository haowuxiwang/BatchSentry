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

  /* ── S4（M6/T6.3）：阶段内已耗时 ────────────────────────────────────
   *
   * 背景：Stage 3 跨页语义分析单次 LLM 调用可达数分钟，其间服务端
   * 进度帧可能毫无变化（cross_progress.done 不动）→ 文案看着像卡死。
   * 服务端 2s 推一帧不足以表达"还活着"，故引入**前端本地秒级计时**：
   * 阶段（phase）一变就重置起点，文案里追加"已用 X"。
   *
   * 计时维度用服务端 phase（ocr/analyze/cross/done/idle），而非 status ——
   * analyzing 同时覆盖 Stage 2 与 Stage 3，只有 phase 能区分"页分析"与
   * "跨页分析"，否则 Stage 3 的计时会从 Stage 2 起点开始算，虚高。 */

  // 需要显示"已用"的阶段（终态/空闲不显示）。
  var LONG_PHASES = ["ocr", "analyze", "cross"];
  // 起步几秒内不显示（避免"已用 0 秒"闪一下）。
  var MIN_ELAPSED_SHOW_SEC = 5;

  /** 阶段计时推进：phase 变化即重置起点。
   *
   * prev: 上一次的 {phase, startedAt}（首次传 null/undefined）
   * 返回 {phase, startedAt, elapsedSec}。纯函数（nowMs 注入，无 Date.now
   * 依赖便于单测）。
   */
  function tickPhase(prev, phase, nowMs) {
    var now = nowMs == null ? Date.now() : nowMs;
    if (!prev || prev.phase !== phase || typeof prev.startedAt !== "number") {
      return { phase: phase, startedAt: now, elapsedSec: 0 };
    }
    return {
      phase: phase,
      startedAt: prev.startedAt,
      elapsedSec: Math.max(0, Math.round((now - prev.startedAt) / 1000)),
    };
  }

  /** 该阶段是否需要显示"已用"（起步阈值内的不显示）。 */
  function showElapsed(state) {
    if (!state || typeof state.elapsedSec !== "number") return false;
    if (LONG_PHASES.indexOf(state.phase) < 0) return false;
    return state.elapsedSec >= MIN_ELAPSED_SHOW_SEC;
  }

  /** 已耗时文案："45 秒" / "3 分 07 秒" / "1 小时 05 分"。
   *  null / 负数 / 非有限值返回 ""（调用方不加该段）。 */
  function fmtElapsed(sec) {
    if (sec == null || !isFinite(sec) || sec < 0) return "";
    var s = Math.floor(sec);
    if (s < 60) return s + " 秒";
    var m = Math.floor(s / 60);
    var pad = function (n) {
      return (n < 10 ? "0" : "") + n;
    };
    if (m < 60) return m + " 分 " + pad(s % 60) + " 秒";
    var h = Math.floor(m / 60);
    return h + " 小时 " + pad(m % 60) + " 分";
  }

  global.PbcEta = {
    analyzeEta: analyzeEta,
    pushSample: pushSample,
    fmtEta: fmtEta,
    fmtRate: fmtRate,
    tickPhase: tickPhase,
    showElapsed: showElapsed,
    fmtElapsed: fmtElapsed,
    WINDOW_MS: WINDOW_MS,
    MIN_SPAN_MS: MIN_SPAN_MS,
    MAX_SAMPLES: MAX_SAMPLES,
    LONG_PHASES: LONG_PHASES,
    MIN_ELAPSED_SHOW_SEC: MIN_ELAPSED_SHOW_SEC,
  };
})(typeof window !== "undefined" ? window : globalThis);
