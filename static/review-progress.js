/* ============================================================
   Review 页 — SSE 实时进度订阅（拆分自 review.js，R63 P1-A/P1-C）

   订阅 /api/jobs/{id}/stream（每 3s 一帧进度快照；终态时服务端推送
   done 事件并关流）。职责：
   - 顶栏进度条（阶段文案 / 百分比 / 双阶段进度 / Stage3 子进度）
   - ETA（PbcEta 采样池）与阶段内已耗时（本地 1s ticker）
   - 停滞可见性横幅（服务端派生 stall 字段）
   - 头栏状态徽章 / 状态点 / 取消按钮 / OCR 后端徽章实时更新
   - 错误帧 terminal/transient 分流（走 PbcStatus.sseErrorAction）
   - 断线重连（指数退避 2s/4s/8s）→ 耗尽降级 10s 轮询

   连接生命周期（EventSource 创建 / 重连退避 / 降级触发 / 卸载清理）
   由共享件 static/sse.js（PbcSse）承担 —— 此前 review.js 与 upload.js
   各写了一份重连骨架，语义有细微分叉（P1-C 收敛点）。
   ============================================================ */
(function (global) {
  "use strict";

  const R = global.PbcReview;
  const log = R.log;

  // UX P1-3: 终态自动刷新保护 — 分析完成时的自动 reload 不得打断用户
  // 正在进行的输入（修正弹窗的 textarea / prompt 输入框）。检测焦点在
  // 可编辑元素或对话框打开 → 跳过自动刷新，仅 toast 提示；否则延迟后刷新。
  function safeAutoReload(delay) {
    const a = document.activeElement;
    const typing =
      a &&
      (a.tagName === "TEXTAREA" ||
        a.tagName === "INPUT" ||
        a.isContentEditable);
    const dialogOpen = document.querySelector('[role="dialog"]:not([hidden])');
    if (typing || dialogOpen) {
      log.warn("safeAutoReload — user busy, skipping auto reload", {
        typing: !!typing,
        dialogOpen: !!dialogOpen,
      });
      window.PBC.showToast(
        "分析已完成 — 当前有未完成的输入，请完成后手动刷新查看最终结果",
        "info",
      );
      return;
    }
    setTimeout(() => location.reload(), delay);
  }

  // B2-10 ③：SSR 的错误横幅（#job-error-banner，见 templates/review.html）
  // 只在**首屏** status=error 时渲染；而 SSE 把状态切到 error 时页面不会
  // 重渲染 ⇒ 用户只看到"出错"，要等 1.5s 自动刷新后才知道原因。
  // 这里按需创建/更新同一条横幅（幂等），让转态期即刻可见原因。
  // 文案与 SSR 保持逐字一致；原因一律 textContent 注入（后端/LLM 文本不可信）。
  const showJobErrorBanner = (reason) => {
    if (!reason) return;
    let banner = document.getElementById("job-error-banner");
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "job-error-banner";
      banner.className =
        "job-error-banner sticky top-0 z-20 rounded-md border-l-2 border-destructive bg-card px-3 py-3 flex items-start gap-2";
      banner.innerHTML =
        '<span class="w-1.5 h-1.5 rounded-full bg-destructive shrink-0 mt-1.5"></span>' +
        '<div class="flex-1 min-w-0">' +
        '<div class="text-[12px] font-semibold text-foreground mb-1">任务处理失败</div>' +
        '<p class="text-[12px] text-muted-foreground leading-relaxed break-words"></p>' +
        '<p class="text-[11px] text-muted-foreground mt-1.5">可点击右上角"重试"重新提交任务；如反复失败，请检查设置页面 LLM/OCR 配置或日志。</p>' +
        "</div>";
      // 与 SSR 同位置：右侧栏顶部、severity-summary 之前
      const host = document.querySelector(".severity-summary");
      if (host && host.parentElement) {
        host.parentElement.insertBefore(banner, host);
      } else {
        document.body.appendChild(banner);
      }
    }
    const p = banner.querySelector("p");
    if (p) p.textContent = reason;
    banner.classList.remove("hidden");
  };

  function subscribe(jid) {
    const bar = document.getElementById("progress-bar-container");
    const txt = document.getElementById("progress-text");
    const fill = document.getElementById("progress-fill");
    if (!bar || !txt || !fill) return;

    bar.classList.remove("hidden");
    bar.classList.add("inline-flex");
    txt.textContent = "连接中…";

    let pollTimer = null;

    // S4（M6/T6.3）：阶段内已耗时 —— 秒级**本地**计时。
    // 服务端 2s 推一帧，但 Stage 3 跨页语义分析单次调用可达数分钟、
    // 帧间 cross_progress 不动 → 文案看着像卡死。本地 1s ticker 只刷新
    // "已用 X"段，不依赖下一帧到达。阶段（phase）变化即重置起点。
    //
    // 注意作用域：计时器必须建在 PbcSse.create **之外** —— create 会因断线
    // 重试被多次调用，建在里面会导致定时器叠加（每次重连多一个 ticker）。
    let phaseState = null;
    let lastLabel = "";
    let labelEditable = true; // 断开/错误提示期间不由 ticker 覆写
    const elapsedSuffix = () => {
      if (typeof PbcEta === "undefined" || !phaseState) return "";
      if (!PbcEta.showElapsed(phaseState)) return "";
      const t = PbcEta.fmtElapsed(phaseState.elapsedSec);
      return t ? ` · 已用 ${t}` : "";
    };
    const renderProgressText = () => {
      txt.textContent = lastLabel + elapsedSuffix();
    };
    // P2 停滞可见性（2026-09-16）：任务长时间无新进展时**先告知**用户 ——
    // 停滞阈值经两轮抬高后（1800→4200s）最长约 70 分钟才会被看门狗收敛，
    // 不能让用户在此期间毫无反馈、只能干等或盲目重启。
    // 数据来自服务端**派生**的 stall 字段（空闲秒数 + 同一份阈值表），
    // 无额外请求、不写库；overdue 时文案升级为"即将判定失败"。
    const renderStall = (d) => {
      const el = document.getElementById("stall-banner");
      if (!el) return;
      const st = d && d.stall;
      if (!st || !st.warn) {
        el.classList.add("hidden");
        return;
      }
      const idleMin = Math.max(1, Math.round(st.idle_seconds / 60));
      const limitMin = Math.max(1, Math.round(st.limit_seconds / 60));
      const textEl = document.getElementById("stall-text");
      if (textEl) {
        textEl.textContent = st.overdue
          ? `任务已 ${idleMin} 分钟无新进展（超过 ${limitMin} 分钟阈值），系统即将判定为失败 — 建议取消后重试`
          : `任务已 ${idleMin} 分钟无新进展（阈值约 ${limitMin} 分钟）— 可取消后重试，或继续等待上游恢复`;
      }
      el.classList.remove("hidden");
    };
    const elapsedTimer = setInterval(() => {
      if (typeof PbcEta === "undefined" || !phaseState) return;
      phaseState = PbcEta.tickPhase(phaseState, phaseState.phase);
      if (labelEditable && lastLabel) renderProgressText();
    }, 1000);
    const stopElapsedTimer = () => clearInterval(elapsedTimer);

    // 流式输出：跟踪 pages_analyzed 变化，当当前页被分析完成时
    // 自动 AJAX 刷新该页 findings，让用户在 Stage 2 进行中就能看到
    // 已分析页的结果，无需等全部页完成。
    let lastPagesAnalyzed = -1;
    // round-23 B：Stage 2 进度 ETA 采样池（5 分钟时间窗口速率）—
    // 15 分钟级的逐页分析只有计数时用户无从判断"还要多久"。
    const etaSamples = [];
    const etaSuffix = (total) => {
      if (typeof PbcEta === "undefined" || !total) return "";
      const eta = PbcEta.analyzeEta(etaSamples, total);
      if (!eta || eta.etaSec == null) return "";
      // 注意：不要命名 txt —— 外层 txt 是 #progress-text 元素
      const etaTxt = PbcEta.fmtEta(eta.etaSec);
      return etaTxt ? ` · 剩余${etaTxt}` : "";
    };

    // ── 正常进度帧渲染（原 onmessage 主体；error 帧已由 PbcSse 分流） ──
    const onFrame = (d) => {
      try {
        const state = R.state;
        const total = d.total_pages || 0;
        // OCR 完成后 total_pages 从 0 → 51，同步标题栏（不重置 iframe）
        if (total > 0 && total !== state.totalPages) {
          state.totalPages = total;
          const label = String(total);
          const pageTotalEl = document.getElementById("page-total");
          if (pageTotalEl) pageTotalEl.textContent = label;
          const counterEl = document.getElementById("page-counter");
          if (counterEl)
            counterEl.textContent = `${state.currentPage} / ${label}`;
          const navTotalEl = document.getElementById("page-nav-total");
          if (navTotalEl) navTotalEl.textContent = label;
          // 同步翻页按钮状态（totalPages 已知后允许翻页）
          R.syncNavButtons();
        }
        // 流式：OCR 完成后若仍在占位态（total_pages=0 时进入页面），
        // 重建页码导航；随后每页圆点随 findings 实时点亮
        if (d.page_finding_counts) {
          if (
            state.totalPages > 0 &&
            !document.querySelector(".page-nav-item")
          ) {
            R.pageview.buildPageNav();
          }
          R.pageview.updatePageNavDots(d.page_finding_counts);
        }
        const analyzedCount = d.pages_analyzed || 0;
        // round-23 B：每帧追采样（SSE 3s 推送；n 回退自动清池重采）
        if (typeof PbcEta !== "undefined" && analyzedCount > 0) {
          PbcEta.pushSample(etaSamples, analyzedCount);
        }
        if (
          analyzedCount > lastPagesAnalyzed &&
          analyzedCount > 0 &&
          state.currentPage <= analyzedCount
        ) {
          lastPagesAnalyzed = analyzedCount;
          log(
            "SSE stream — page analyzed, refreshing current page",
            { currentPage: state.currentPage, pagesAnalyzed: analyzedCount, status: d.status },
          );
          // 静默刷新当前页 findings（不显示 loading overlay，避免干扰）
          R.refreshCurrentPageFindings();
        }

        // 计算进度百分比
        let pct = 0;
        let label = d.status;
        if (d.status === "pending") {
          pct = 0;
          label = "排队中";
        } else if (d.status === "ocr_running" || d.status === "ocr_done") {
          // OCR 阶段：用 ocr_progress（轮询进度 extracted/total），
          // 比 pages_ocr_done（OCR 完成后才写入 page_cache）实时得多。
          // 分片 OCR 期间分析也在进行（pages_analyzed>0），显示双进度。
          const prog = d.ocr_progress || {};
          const ocrDone = prog.done || 0;
          const ocrTotal = prog.total || 0;
          const analyzePct =
            33 +
            (total > 0 ? Math.round((analyzedCount / total) * 60) : 0);
          if (ocrTotal > 0) {
            pct = Math.max(Math.round((ocrDone / ocrTotal) * 33), analyzePct);
            const sh = d.self_heal_progress;
            if (sh && sh.total > 0) {
              // 空页自愈阶段：主 OCR 进度已 done==total 但状态未前进 —
              // 不显示自愈进度的话用户会误判卡死
              label = `空页自愈 ${sh.done}/${sh.total}` + (
                analyzedCount > 0 ? ` · 分析 ${analyzedCount}/${total}` : ""
              );
            } else {
              label = analyzedCount > 0
                ? `OCR ${ocrDone}/${ocrTotal} · 分析 ${analyzedCount}/${total}` + etaSuffix(total)
                : `OCR ${ocrDone}/${ocrTotal}`;
            }
          } else if (total > 0) {
            pct = total > 0 ? Math.round((d.pages_ocr_done / total) * 33) : 0;
            label = analyzedCount > 0
              ? `OCR ${d.pages_ocr_done}/${total} · 分析 ${analyzedCount}/${total}`
              : `OCR ${d.pages_ocr_done}/${total}`;
          } else {
            label = "OCR 处理中…";
          }
        } else if (d.status === "analyzing" && d.phase === "cross") {
          // Todo 13: stage3 阶段指示 — analyzing 含 Stage 2+3，
          // 页分析完成后推断已进入跨页语义分析
          pct = 93;
          // P1-6: Stage 3 子进度（规则校验/LLM 兜底/LLM 语义里程碑）
          const cr = d.cross_progress;
          label = cr && cr.total > 0
            ? `跨页分析 ${cr.done}/${cr.total} · ${cr.label}`
            : `跨页分析中（${analyzedCount}/${total} 页）`;
        } else if (d.status === "analyzing") {
          pct =
            33 +
            (total > 0 ? Math.round((analyzedCount / total) * 60) : 0);
          // round-23 B：分析计数 + 时间窗口速率 ETA（首 ~8s 无速率，
          // 只有计数 — 与旧行为一致，速率就绪后自动出现"剩余约 N 分钟"）
          label = `分析 ${analyzedCount}/${total}` + etaSuffix(total);
        } else if (d.status === "cancelling" || d.status === "cancelled") {
          pct = 0;
          label = d.status === "cancelling" ? "取消中…" : "已取消";
        } else if (d.status === "error") {
          pct = 0;
          label = "出错";
        } else {
          // B2-10 ②：未知状态兜底此前显示**裸英文 token**（review /
          // partial_review / done 都会落到这里），与同页中文徽章
          // 自相矛盾（同一状态、同页两处文案不一致）。改走共享件
          // static/status.js（状态中文的单一真值）。
          label =
            (window.PbcStatus ? PbcStatus.statusZh(d.status) : "") ||
            d.status;
        }

        // cr-19：头栏状态徽章/取消按钮随 SSE 实时更新 — 旧实现是 SSR
        // 一次性渲染：阶段切换（ocr_running→analyzing）徽章不变化；
        // 终态后 done 事件与 1.5s reload 之间取消按钮仍可点（后端
        // 400 Invalid transition）。
        // B2-10 ④：此处曾自带第 3 份状态中文映射（status.js / upload.js
        // 之外），而状态点颜色却又走共享件 PbcStatus.statusDotClass ⇒
        // **文字与颜色不同源**（改一处改不动另一处）。统一走共享件。
        const badgeEl = document.getElementById("status-badge");
        // 修复：旧代码 `|| 未知()` 引用未定义函数，ReferenceError 被外层
        // catch 吞掉 → 整个 SSE 帧更新中断；与上方 label 兜底逻辑对齐
        if (badgeEl) {
          badgeEl.textContent =
            (window.PbcStatus ? PbcStatus.statusZh(d.status) : "") ||
            d.status;
        }
        // #133(P0)：状态点颜色必须跟着状态走。旧实现只改 textContent，
        // 点保持模板里硬编码的 bg-success ⇒ error/partial_review 显示
        // "绿点 + 出错"，与"记录确实无异常"不可区分（GMP 假阴性）。
        // 真值源 static/status.js（与 core/zh_map.py 由机检锁定一致）。
        const dotEl = document.getElementById("status-dot");
        if (dotEl && window.PbcStatus) {
          dotEl.className =
            "w-1.5 h-1.5 rounded-full " + window.PbcStatus.statusDotClass(d.status);
        }
        const cancelBtn = document.getElementById("cancel-btn");
        if (cancelBtn) {
          const canCancel = ["pending", "ocr_running", "ocr_done", "analyzing"].includes(d.status);
          cancelBtn.disabled = !canCancel;
          cancelBtn.classList.toggle("opacity-40", !canCancel);
          cancelBtn.classList.toggle("pointer-events-none", !canCancel);
        }
        const ocrBadge = document.getElementById("ocr-backend-badge");
        if (ocrBadge) {
          if (d.ocr_backend_used) {
            ocrBadge.textContent = d.ocr_backend_display || d.ocr_backend_used;
            ocrBadge.classList.remove("hidden");
          }
        }

        fill.style.width = pct + "%";
        // S4（M6/T6.3）：阶段内已耗时 —— 以服务端 phase 为计时维度
        // （analyzing 同时含 Stage 2/3，只有 phase 能区分）。phase 变化
        // 即重置起点；随后由 1s ticker 持续刷新，不等下一帧。
        const phase = d.phase || d.status;
        if (typeof PbcEta !== "undefined") {
          phaseState = PbcEta.tickPhase(phaseState, phase);
        }
        lastLabel = label;
        labelEditable = true;
        renderProgressText();
        renderStall(d);
        // B2-10 ③：status 切到 error 的帧本身带 error_message（见
        // api/jobs/status.py 的进度快照负载）⇒ 转态期即可显示原因，
        // 不必等 1.5s 自动刷新后由 SSR 横幅给出。
        if (d.status === "error" && d.error_message) {
          showJobErrorBanner(d.error_message);
        }
        log("SSE progress", { status: d.status, pct, label, phase });
      } catch (err) {
        log.warn("SSE parse error", err);
      }
    };

    log("SSE subscribe", `/api/jobs/${jid}/stream`);
    PbcSse.create({
      url: `/api/jobs/${jid}/stream`,
      mode: "manual", // 指数退避手动重连（语义与拆分前 review.js 一致）
      maxRetries: 3,
      backoffBaseMs: 2000,
      onFrame: onFrame,
      // 应用级错误帧（terminal/transient）的处置由 PbcSse 走
      // PbcStatus.sseErrorAction 分流（B2-10 ①，单一真值）。
      onTransientError: (d) => {
        // 瞬态（进度查询失败，服务端发完继续推帧）：不关流 / 不清
        // pollTimer / 不停计时器 —— 只把进度文案换成如实提示，
        // 下一帧正常数据会自动还原。
        log.err("SSE job error (transient)", d);
        labelEditable = false;
        txt.textContent = "进度查询异常，重试中…";
      },
      onTerminalError: (d) => {
        log.err("SSE job error (terminal)", d);
        if (pollTimer) clearInterval(pollTimer);
        labelEditable = false;
        stopElapsedTimer();
        const reason = d.message || "任务不存在或已被删除";
        txt.textContent = reason;
        const barEl = document.getElementById("progress-bar-container");
        if (barEl) barEl.classList.add("opacity-60");
        showJobErrorBanner(reason); // B2-10 ③：终态即刻给出原因
      },
      onDone: () => {
        log("SSE done — closing stream, reloading page");
        stopElapsedTimer();
        // 终态：1.5s 后自动刷新页面，加载最终 findings
        R.safeAutoReload(1500);
      },
      onRetry: (count, delayMs) => {
        // 指数退避重试：2s / 4s / 8s
        log.warn("SSE connection error — retry scheduled", { count, delayMs });
        labelEditable = false; // 提示文案不被 1s ticker 覆写
        txt.textContent = `连接断开，${delayMs / 1000} 秒后重试…`;
      },
      onGiveUp: () => {
        // 重试耗尽：fallback 到 10s 轮询 /api/jobs/{id}
        log.warn("SSE retries exhausted, fallback to polling");
        stopElapsedTimer();
        txt.textContent = "实时连接不可用，切换轮询…";
      },
      fallback: () => {
        const terminalStatuses = [
          "review",
          "partial_review",
          "error",
          "cancelled",
          "archived",
        ];
        // 轮询兜底失败上限：连续 6 次（≈60s）拿不到进度即停止轮询并给出
        // 可操作提示。旧实现 `if (!r.ok) return;` + catch 静默吞异常 ⇒
        // 后端持续不可用时页面每 10s 无反馈地重试到永远（既无终态、又无
        // 提示，用户只能干等，且连接/定时器永不释放）。
        const MAX_POLL_FAILURES = 6;
        let pollFailures = 0;
        const stopPollingWithError = (reason) => {
          if (pollTimer) clearInterval(pollTimer);
          pollTimer = null;
          labelEditable = false; // 提示文案不被 1s ticker 覆写
          txt.textContent = reason;
          const barEl = document.getElementById("progress-bar-container");
          if (barEl) barEl.classList.add("opacity-60");
        };
        pollTimer = setInterval(async () => {
          try {
            const r = await fetch(`/api/jobs/${jid}`);
            if (!r.ok) {
              pollFailures += 1;
              log.warn("poll — non-ok response", {
                status: r.status,
                pollFailures,
              });
              if (pollFailures >= MAX_POLL_FAILURES) {
                stopPollingWithError(
                  "进度查询连续失败，已停止自动刷新 — 请检查后端服务后手动刷新页面",
                );
              }
              return;
            }
            // 成功一次即清零：偶发抖动不应累积成"连续失败"
            pollFailures = 0;
            const d = await r.json();
            if (terminalStatuses.includes(d.status)) {
              clearInterval(pollTimer);
              pollTimer = null;
              R.safeAutoReload(500);
            }
          } catch (err) {
            pollFailures += 1;
            log.warn("poll failed", { err, pollFailures });
            if (pollFailures >= MAX_POLL_FAILURES) {
              stopPollingWithError(
                "进度查询连续失败，已停止自动刷新 — 请检查后端服务后手动刷新页面",
              );
            }
          }
        }, 10000);
      },
    });

    // 页面卸载时清理轮询/阶段计时定时器（SSE 连接本体由 PbcSse 统一清理）
    window.addEventListener("beforeunload", () => {
      if (pollTimer) clearInterval(pollTimer);
      stopElapsedTimer();
    });
  }

  const progress = (global.PbcReview = global.PbcReview || {});
  progress.progress = {
    subscribe: subscribe,
    safeAutoReload: safeAutoReload,
    showJobErrorBanner: showJobErrorBanner,
  };
})(typeof window !== "undefined" ? window : globalThis);
