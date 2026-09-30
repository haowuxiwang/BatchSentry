/* SSE 连接生命周期骨架（重连 / 降级 / 错误帧分流）— review 页与 upload 页共享。
 *
 * 由来（R62 审计 P1-C）：同一套「EventSource + 断线重连 + 重试耗尽降级轮询」
 * 逻辑在两个文件里各写了一遍：
 *   - review.js subscribeProgress：单任务流 /api/jobs/{id}/stream，
 *     onerror → **手动 close** → 指数退避（2s/4s/8s）手动重连 → 3 次耗尽
 *     → 10s 轮询单 job 直至终态 reload；
 *   - upload.js startLiveTracking：聚合流 /api/jobs/live，onerror →
 *     **不 close**（依赖 EventSource 按服务端 retry: 2000 内建重连）→
 *     连续 3 次错误才 close → 降级为逐 job 10s 轮询。
 * 两处计数重置时机也不一致（review 在 onopen 重置；upload 在成功帧重置，
 * 因聚合流可能长期 onopen 后无帧、或帧正常到达期间从未再触发 onopen）。
 * 任何一侧调优（如退避上限、terminal 帧语义）都要记得改另一处 —— 已实际
 * 发生过（upload 曾因 errCount 粘滞被降级，review 却早已修过同类问题）。
 *
 * 本模块把两种重连策略收敛为参数化模式，语义与两侧现状逐一对齐：
 *   mode "manual"（review）：onerror 即 close，指数退避 setTimeout 重连；
 *   mode "auto"（upload）：onerror 只计数，靠 EventSource 内建重连；
 *     连续错误达阈值才 close 并降级。
 *
 * 错误帧（type=error）的两类语义（terminal / transient）由
 * PbcStatus.sseErrorAction 判定（单一真值，见 static/status.js），本模块
 * 只执行处置：terminal → 关流且不再重连；transient → 保持长连。
 *
 * 纯浏览器环境（node 单测通过注入 stub EventSource / setTimeout 驱动）。
 */
(function (global) {
  "use strict";

  function create(opts) {
    var url = opts.url;
    var mode = opts.mode === "auto" ? "auto" : "manual";
    var maxRetries = typeof opts.maxRetries === "number" ? opts.maxRetries : 3;
    var backoffBaseMs =
      typeof opts.backoffBaseMs === "number" ? opts.backoffBaseMs : 2000;
    var EventSourceCtor = opts.EventSource || global.EventSource;

    var retryCount = 0; // manual: 已用掉的退避次数; auto: 连续错误数
    var es = null;
    var retryTimer = null;
    var closed = false; // 页面主动 close / done / terminal 后不再重连

    // 错误帧处置判定：优先页面注入，缺省走共享真值 PbcStatus.sseErrorAction
    var decideErrorFrame =
      opts.decideErrorFrame ||
      function (d) {
        return global.PbcStatus ? global.PbcStatus.sseErrorAction(d) : null;
      };

    function clearRetryTimer() {
      if (retryTimer !== null) {
        global.clearTimeout(retryTimer);
        retryTimer = null;
      }
    }

    function closeStream() {
      closed = true;
      clearRetryTimer();
      if (es) {
        es.close();
        es = null;
      }
    }

    function scheduleRetry(delayMs) {
      retryTimer = global.setTimeout(function () {
        retryTimer = null;
        if (!closed) connect();
      }, delayMs);
    }

    function connect() {
      if (closed) return;
      es = new EventSourceCtor(url);

      es.onopen = function () {
        // 成功建立连接 → 重置计数（manual：退避序列从头开始；
        // auto：连续错误计数清零）
        retryCount = 0;
        if (opts.onOpen) opts.onOpen();
      };

      es.onmessage = function (e) {
        if (closed) return;
        var d;
        try {
          d = JSON.parse(e.data);
        } catch (err) {
          if (opts.onParseError) opts.onParseError(err);
          return;
        }
        // auto 模式：任何成功到达的帧都证明连接活着 —— 重置连续错误计数
        // （upload.js cr-19 修复的语义：仅靠 onopen 重置会漏掉「内建重连
        // 成功但服务端迟迟不触发 onopen」的窗口）。
        if (mode === "auto" && retryCount > 0) retryCount = 0;
        // 应用级错误帧（type=error）：terminal → 关流（不再重连）；
        // transient → 保持长连（服务端发完该帧会继续推正常帧）。
        if (d && d.type === "error") {
          var action = decideErrorFrame(d);
          if (action === "terminal") {
            closeStream();
            if (opts.onTerminalError) opts.onTerminalError(d);
            return;
          }
          if (opts.onTransientError) opts.onTransientError(d);
          return;
        }
        if (opts.onFrame) opts.onFrame(d, es);
      };

      // 服务端主动推送 done 事件（单任务流）：终态，关流不重连。
      if (opts.onDone) {
        es.addEventListener("done", function (e) {
          closeStream();
          opts.onDone(e, es);
        });
      }

      es.onerror = function () {
        if (closed) return;
        if (mode === "manual") {
          // 手动重连：立即 close（阻止 EventSource 内建重连 —— 退避节奏
          // 必须由我们控制，否则「内建 2s 重连」与「我们的指数退避」叠加
          // 会出现双连接）。
          if (es) {
            es.close();
            es = null;
          }
          if (retryCount < maxRetries) {
            var delayMs = backoffBaseMs * Math.pow(2, retryCount);
            retryCount += 1;
            if (opts.onRetry) opts.onRetry(retryCount, delayMs);
            scheduleRetry(delayMs);
          } else {
            // 重试耗尽：交由页面降级（轮询）。此后本流不再自愈。
            closed = true;
            if (opts.onGiveUp) opts.onGiveUp();
            if (opts.fallback) opts.fallback();
          }
        } else {
          // auto：不 close —— EventSource 按服务端 retry 提示内建重连。
          retryCount += 1;
          if (retryCount >= maxRetries) {
            if (es) {
              es.close();
              es = null;
            }
            closed = true;
            if (opts.onGiveUp) opts.onGiveUp();
            if (opts.fallback) opts.fallback();
          }
        }
      };
    }

    connect();

    // 页面卸载：统一清理（连 SSE 带走是常见泄漏；pending 的退避 timer
    // 若不清理，卸载后仍会触发 connect 打开新连接）。
    if (typeof global.addEventListener === "function") {
      global.addEventListener("beforeunload", function () {
        closed = true;
        clearRetryTimer();
        if (es) {
          es.close();
          es = null;
        }
      });
    }

    return {
      close: closeStream,
      isClosed: function () {
        return closed;
      },
    };
  }

  global.PbcSse = { create: create };
})(typeof window !== "undefined" ? window : globalThis);
