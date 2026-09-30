/* ============================================================
   Upload 页 — Job 历史列表与实时状态（拆分自 upload.js，R63 P2-1）

   职责：
   - 历史记录 AJAX 加载 / 分页 / 骨架屏
   - 单条 /api/jobs/live 聚合 SSE 订阅（按 job_id 分发到行内）
   - 断线降级：连续 3 次错误后逐 job 10s 轮询直至终态
   - Stage 2 ETA 采样池 / 阶段内已耗时（本地 1s ticker）
   - 归档 / 删除 / 已归档列表（事件委托，无内联 onclick）

   连接生命周期（EventSource 创建 / 自动重连 / 降级触发 / 卸载清理）
   由共享件 static/sse.js（PbcSse）承担 —— 此前 upload.js 与 review.js
   各写一份重连骨架，语义有细微分叉（P1-C 收敛点）。本模块用
   PbcSse auto 模式：onerror 只计数、靠 EventSource 内建重连
   （服务端 retry: 2000），成功帧重置计数，连续 3 次才降级轮询。

   依赖（upload.html 按序加载）：status.js → sse.js → eta.js →
   confirm-dialog.js → 本文件。文件上传交互仍在 upload.js。
   ============================================================ */
(function () {
  "use strict";

  const log = (...args) =>
    console.log("%c[PBC]", "color:#0ea5e9;font-weight:bold", ...args);
  log.warn = (...args) =>
    console.warn("%c[PBC]", "color:#f59e0b;font-weight:bold", ...args);
  log.err = (...args) =>
    console.error("%c[PBC]", "color:#ef4444;font-weight:bold", ...args);

  // 状态 → 中文 / 状态点颜色：**单一真值在 static/status.js**（缺陷 #133/#139）。
  // 保留 PbcStatus 缺失时的兜底，是为了"共享件没加载"时不至于整页崩掉
  // （降级为不显色，而非报错）。
  const PbcStatus = window.PbcStatus || null;
  if (!PbcStatus) {
    log.warn("PbcStatus 未加载 — 状态徽章将退化为兜底显示（检查 status.js 是否引入）");
  }

  function statusZh(st) {
    return PbcStatus
      ? PbcStatus.statusZh(st)
      : (st || "");
  }

  function statusDotClass(st) {
    return PbcStatus ? PbcStatus.statusDotClass(st) : "bg-muted-foreground/40";
  }

  // 状态点的**完整** class（含 `.status-dot` 定位标记）。
  //
  // 这个标记是 renderJobRow（产出方）与 updateJobRowLive（消费方）之间的**契约**：
  // 实时更新靠 `li.querySelector(".status-dot")` 找回圆点。此前两处各自拼 class，
  // 实时那处**漏掉了标记** ⇒ 第一帧之后圆点就再也找不到，颜色从此冻结在首帧状态
  // （而 `.status-text` 仍在正常变化 ⇒ 文案与颜色互相矛盾，对复核者是主动误导：
  // 例如红点旁边写着"可复核"）。收敛为单一函数后，标记不可能再被漏掉 ——
  // 与 buildMetaLine 收敛"静态/实时两条路径"是同一个理由。
  function statusDotClassName(st) {
    return `w-1.5 h-1.5 rounded-full ${statusDotClass(st)} status-dot`;
  }

  // 从摘要行取回 created_at：SSE 快照**不带**该字段，只能从既有行继承。
  // 此前两处各写一份 split，**分隔符还不一致**（`" · "` vs `"· "`），后者会留下
  // 尾随空格 ⇒ 重建出的行出现 "10:00  · 失败页" 双空格，且每重建一次再累积一个。
  // 收敛为单一函数并 trim，消除这类"同一派生两处实现"的漂移。
  function createdAtFromMeta(li) {
    const meta = li.querySelector(".job-meta");
    const parts = ((meta && meta.textContent) || "").split("· ");
    return (parts[1] || "").trim();
  }

  // 与后端 _ACTIVE_STATUSES 对齐：运行中状态禁用删除（防孤儿 pipeline task）。
  // 真值源 static/status.js（PbcStatus.ACTIVE_STATUSES），同样避免多份副本漂移。
  const ACTIVE_STATUSES = (PbcStatus && PbcStatus.ACTIVE_STATUSES) || [
    "pending",
    "ocr_running",
    "ocr_done",
    "analyzing",
    "cancelling",
  ];
  const TERMINAL_STATUSES = [
    "review",
    "partial_review",
    "error",
    "cancelled",
    "archived",
  ];

  // === 并发额度共享状态（供 upload.js 的"发送前预检"消费）===
  //
  // 为什么需要：多文件上传是**串行**的（见 upload.js submitFiles），配额满时
  // 后端对第 4 份直接 409 —— 而那一刻文件**已经完整传完**（大文件分钟级）。
  // 前端在发送前知道"还有没有空位"，就能把"白传一次 + 红错"变成"等空位再传"。
  //
  // 单一写点：本模块（唯一持有 /api/jobs/live 的地方）。
  // 单一读点：upload.js 的 waitForQuotaSlot()。
  // 契约：window.PbcJobs.quota() -> { active, limit }；
  //       **limit <= 0 表示"未知"**（服务端未注入 concurrency）⇒ 消费方必须
  //       跳过预检（fail-open：后端本来就权威，且其 409 有独立分支兜底）。
  //
  // active 的口径必须与后端 `_count_active_jobs` 一致：**只数 ACTIVE_STATUSES**。
  // /api/jobs/live 的推送体还包含近 10 分钟内的**终态** job（行状态切换需要），
  // 直接 `jobs.length` 会**高估** ⇒ 明明有空位却一直等（把可用性换成假死）。
  // 故用 liveStatuses 登记表按状态过滤，且 SSE 帧与降级轮询**共同维护**它 ——
  // 否则降级后 active 会冻结在最后一次帧的值。
  const liveStatuses = new Map(); // jobId -> status
  const quota = {
    active: 0,
    limit: (function () {
      const injected = (window.__PBC__ || {}).concurrency;
      const n = injected && Number(injected.limit);
      return Number.isFinite(n) && n > 0 ? n : 0;
    })(),
  };
  function recountQuota() {
    let n = 0;
    liveStatuses.forEach((st) => {
      if (ACTIVE_STATUSES.includes(st)) n += 1;
    });
    quota.active = n;
  }
  window.PbcJobs = window.PbcJobs || {};
  window.PbcJobs.quota = function () {
    return { active: quota.active, limit: quota.limit };
  };
  if (quota.limit > 0) {
    log("并发额度已下发", { limit: quota.limit });
  } else {
    log.warn(
      "window.__PBC__.concurrency.limit 缺失 — 跳过发送前额度预检，交由服务端 409 判定",
    );
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  let currentPage = 1;
  let totalPages = 1;
  let totalJobs = 0;
  // 翻页竞态守卫：每次 loadHistory 递增 token，仅最后一次请求的结果生效。
  // 否则快速点 "下一页→上一页" 时，先发的 page 2 响应可能后到，覆盖 page 1。
  let loadHistoryToken = 0;

  async function loadHistory(page) {
    const token = ++loadHistoryToken;
    currentPage = page;
    closeAllLiveSources(); // 翻页/重载：关闭旧页 job 的实时订阅
    const listEl = document.getElementById("history-list");
    const pagEl = document.getElementById("history-pagination");
    const countEl = document.getElementById("history-count");
    // 容器缺失（模板变更 / 局部渲染 / 测试桩不完整）时必须**提前返回**：
    // 旧代码下一行 `listEl.innerHTML` 就抛 TypeError，而本函数在**模块加载期**
    // 被调用（文件末尾的 `loadHistory(1)`）—— 异常会中断整个 IIFE，
    // 后续所有事件绑定（删除 / 归档 / 翻页按钮）随之全部失效，
    // 页面看起来"加载完了但点什么都没反应"。
    if (!listEl || !pagEl) {
      log.warn("loadHistory — 容器缺失，跳过渲染", {
        list: !!listEl,
        pagination: !!pagEl,
      });
      return;
    }
    listEl.innerHTML = "";
    // P3-2: 行形骨架屏（Linear 风格 — 占位形态预测最终行形态，无布局跳动）
    for (let i = 0; i < 3; i++) {
      const li = document.createElement("li");
      li.className = "animate-pulse";
      li.innerHTML =
        '<div class="flex items-center gap-4 px-5 py-3.5">' +
        '<div class="flex-1 min-w-0">' +
        '<div class="h-3 w-2/5 bg-muted rounded"></div>' +
        '<div class="h-2.5 w-1/4 bg-muted/60 rounded mt-2"></div>' +
        "</div>" +
        '<div class="w-16 h-5 bg-muted rounded"></div>' +
        "</div>";
      listEl.appendChild(li);
    }
    pagEl.classList.add("hidden");
    try {
      const r = await fetch(`/api/jobs?page=${page}&page_size=20`);
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      // 竞态守卫：若期间又发起了新请求，丢弃本次结果
      if (token !== loadHistoryToken) {
        log("loadHistory — stale response dropped", { page, token });
        return;
      }
      totalPages = data.total_pages || 1;
      totalJobs = data.total_jobs || 0;
      // 计数是次要 UI：缺元素不影响列表本身
      if (countEl) countEl.textContent = `${totalJobs} 份`;
      log("loadHistory", { page, jobs: data.jobs.length, totalJobs });

      if (data.jobs.length === 0) {
        listEl.innerHTML =
          '<li class="px-5 py-12 text-center text-[12px] text-muted-foreground">暂无历史记录</li>';
        return;
      }
      listEl.innerHTML = "";
      const frag = document.createDocumentFragment();
      data.jobs.forEach((job, i) => frag.appendChild(renderJobRow(job, i)));
      listEl.appendChild(frag);
      renderPagination();
      startLiveTracking();
    } catch (err) {
      if (token !== loadHistoryToken) return; // 同样丢弃过期错误
      log.err("loadHistory failed", err);
      listEl.innerHTML = `<li class="px-5 py-8 text-center text-[12px] text-destructive">加载失败: ${esc(err.message)}</li>`;
    }
  }

  let liveHandle = null; // PbcSse 句柄（auto 模式聚合流）
  const pollTimers = new Map(); // jobId -> interval (fallback polling after SSE loss)
  // round-23 B：Stage 2 分析进度 ETA 采样池（jobId -> [{n, t}]，5 分钟
  // 时间窗口速率）— 上传页任务行的 "分析 x/y" 此前只有计数，15 分钟级
  // 的逐页 LLM 分析无从判断还要多久。终态时清理防 Map 泄漏。
  const etaSamplesByJob = new Map();
  const etaSuffixFor = (jid, total) => {
    if (typeof PbcEta === "undefined" || !total) return "";
    const samples = etaSamplesByJob.get(jid);
    if (!samples) return "";
    const eta = PbcEta.analyzeEta(samples, total);
    if (!eta || eta.etaSec == null) return "";
    const etaTxt = PbcEta.fmtEta(eta.etaSec);
    return etaTxt ? ` · 剩余${etaTxt}` : "";
  };

  // S4（M6/T6.3）：阶段内已耗时 —— jobId -> {phase, startedAt}（跨帧保持，
  // 故用 Map 而非行内变量）。Stage 3 跨页分析单次调用可达数分钟，帧间
  // cross_progress 不动 → 行内文案看着像卡死；"已用 X" 每帧前进即证明
  // 进程仍在工作。终态清理防 Map 泄漏。
  const phaseTimerByJob = new Map();
  const elapsedSuffixFor = (jid, phase) => {
    if (typeof PbcEta === "undefined" || !phase) return "";
    const st = PbcEta.tickPhase(phaseTimerByJob.get(jid) || null, phase);
    phaseTimerByJob.set(jid, st);
    if (!PbcEta.showElapsed(st)) return "";
    const t = PbcEta.fmtElapsed(st.elapsedSec);
    return t ? ` · 已用 ${t}` : "";
  };
  // 活跃行文案刷新表（jobId -> {el, base, phase}）：1s 本地 ticker 只重写
  // "已用"段，不等 SSE 帧（服务端 2s 一帧，秒级观感要靠本地计时）。
  // 生命周期与实时订阅配对：startLiveTracking 启动 / closeAllLiveSources 停止。
  const stageRows = new Map();
  let stageTicker = null;
  function startStageTicker() {
    if (stageTicker) return;
    stageTicker = setInterval(() => {
      stageRows.forEach((e, jid) => {
        if (!e.el.isConnected) {
          stageRows.delete(jid); // 行被 SPA 重建/替换 → 停止追踪
          return;
        }
        e.el.textContent = e.base + elapsedSuffixFor(jid, e.phase);
      });
    }, 1000);
  }
  function stopStageTicker() {
    if (stageTicker) {
      clearInterval(stageTicker);
      stageTicker = null;
    }
    stageRows.clear();
  }

  // === Job 行实时状态（SSE 聚合） ===
  // 单条 /api/jobs/live 连接推送所有活跃任务快照，按 job_id 分发到行内。
  // 对比逐任务 EventSource：HTTP/1.1 每域 6 连接上限下，多任务并行 +
  // 多标签页不会饿死普通请求。断线 3 次后降级为逐 job 10s 轮询。
  //
  // R63 P1-C：连接骨架改用共享件 PbcSse（auto 模式）—— onerror 只计数、
  // 靠 EventSource 内建重连（cr-19：成功帧重置计数，瞬时抖动不再粘滞）；
  // 连续 3 次错误才 close + 降级轮询。此前这里手写同一套骨架，与
  // review 侧语义有细微分叉（见 sse.js 头注）。
  function startLiveTracking() {
    if (liveHandle && !liveHandle.isClosed()) return;
    startStageTicker(); // S4：活跃行的"已用 X"本地秒级刷新
    liveHandle = PbcSse.create({
      url: "/api/jobs/live",
      mode: "auto",
      maxRetries: 3,
      onFrame: (d) => {
        // cr-19：成功帧重置断线计数（由 PbcSse auto 模式内部处理）—
        // 旧实现 errCount 粘滞，EventSource 正常自动重连（retry: 2000）
        // 也会累积计数，3 次瞬时抖动后即使连接已恢复也错误降级为 10s 轮询。
        log("SSE aggregated update", { jobs: (d.jobs || []).length });
        const jobs = Array.isArray(d) ? d : d.jobs || [];
        // 额度登记：**在渲染之前**无条件登记（渲染有 `if (!li) return` 短路，
        // 而"当前页没有这一行"的活跃 job 同样占着后端额度 —— 挂在渲染循环里
        // 会漏计，active 偏小 ⇒ 预检放行 ⇒ 又回到"白传 + 409"）。
        jobs.forEach((snap) => {
          if (snap && snap.id) liveStatuses.set(snap.id, snap.status);
        });
        recountQuota();
        jobs.forEach((snap) => {
          if (!snap || !snap.id) return;
          const li = findJobRow(snap.id);
          if (!li) return;
          updateJobRowLive(li, snap);
          if (TERMINAL_STATUSES.includes(snap.status)) {
            // 终态：重建该行以刷新按钮可用状态（归档/删除/复核链接）
            const fresh = buildRowFromSnapshot(li, snap);
            if (fresh) li.replaceWith(fresh);
          }
        });
      },
      onGiveUp: () => {
        log.warn("SSE aggregated stream lost, falling back to polling");
      },
      fallback: () => {
        startFallbackPolling();
      },
    });
  }

  function findJobRow(jid) {
    return document.querySelector(
      `#history-list li[data-job-id="${CSS.escape(jid)}"]`,
    );
  }

  // SSE 失效后兜底：对当前可见的每个活跃 job 开 10s 轮询直至终态
  function startFallbackPolling() {
    document
      .querySelectorAll("#history-list li[data-job-id]")
      .forEach((li) => {
        const jid = li.dataset.jobId;
        const st = li.dataset.status;
        if (!jid || !ACTIVE_STATUSES.includes(st)) return;
        if (pollTimers.has(jid)) return;
        const timer = setInterval(async () => {
          try {
            const row = findJobRow(jid);
            if (!row) {
              // 行已被移除（归档/删除）：清掉定时器，避免永久泄漏
              clearInterval(timer);
              pollTimers.delete(jid);
              return;
            }
            const r = await fetch(`/api/jobs/${encodeURIComponent(jid)}`);
            if (!r.ok) {
              // 404（job 被删除）或 5xx：终态无条件退出轮询
              clearInterval(timer);
              pollTimers.delete(jid);
              return;
            }
            const d = await r.json();
            // 降级路径也要维护额度登记：SSE 已断，若只靠帧更新，active 会
            // 冻结在最后一次帧的值 —— 有 job 跑完也永远"满额"，预检变成
            // 永久等待（比不做预检更糟）。
            liveStatuses.set(jid, d.status);
            recountQuota();
            updateJobRowLive(row, d);
            if (TERMINAL_STATUSES.includes(d.status)) {
              clearInterval(timer);
              pollTimers.delete(jid);
              const fresh = buildRowFromSnapshot(row, d);
              if (fresh) row.replaceWith(fresh);
            }
          } catch (_) {
            /* transient network error — keep polling */
          }
        }, 10000);
        pollTimers.set(jid, timer);
      });
  }

  function updateJobRowLive(li, d) {
    const st = d.status;
    li.dataset.status = st;
    // round-23 B：追 ETA 采样（SSE 与兜底轮询共用此入口）；
    // 终态清理采样池（防 Map 泄漏）。
    if (typeof PbcEta !== "undefined" && d.pages_analyzed > 0) {
      let samples = etaSamplesByJob.get(li.dataset.jobId);
      if (!samples) {
        samples = [];
        etaSamplesByJob.set(li.dataset.jobId, samples);
      }
      PbcEta.pushSample(samples, d.pages_analyzed);
    }
    if (TERMINAL_STATUSES.includes(st)) {
      etaSamplesByJob.delete(li.dataset.jobId);
      phaseTimerByJob.delete(li.dataset.jobId);
    }
    const dot = li.querySelector(".status-dot");
    const stText = li.querySelector(".status-text");
    const pages = li.querySelector(".job-pages");
    const err = li.querySelector(".job-error");
    const ocrTag = li.querySelector(".job-ocr-backend");
    if (dot) dot.className = statusDotClassName(st);
    if (stText) stText.textContent = statusZh(st);
    if (pages) {
      const prog = d.ocr_progress || {};
      const sh = d.self_heal_progress;
      // S4（M6/T6.3）：先算出"基础文案"，再统一追加阶段已耗时 ——
      // 统一出口避免各分支各自拼接导致漏加/重复加。
      let base;
      if (
        (st === "ocr_running" || st === "ocr_done") &&
        sh && sh.total > 0
      ) {
        base = `空页自愈 ${sh.done}/${sh.total}`;
      } else if ((st === "ocr_running" || st === "ocr_done") && prog.total > 0) {
        base =
          d.pages_analyzed > 0
            ? `OCR ${prog.done}/${prog.total} · 分析 ${d.pages_analyzed}/${d.total_pages || "?"}` +
              etaSuffixFor(li.dataset.jobId, d.total_pages)
            : `OCR ${prog.done}/${prog.total}`;
      } else if (st === "analyzing" && d.phase === "cross") {
        // Todo 14: stage3 阶段指示 — 页分析完成后已进入跨页语义分析
        // P1-6: 子进度里程碑（规则校验/LLM 兜底/LLM 语义）
        const cr = d.cross_progress;
        base = cr && cr.total > 0
          ? `跨页分析 ${cr.done}/${cr.total} · ${cr.label}`
          : `跨页分析中 · ${d.pages_analyzed || 0}/${d.total_pages || "?"} 页`;
      } else if (st === "analyzing") {
        base = `分析 ${d.pages_analyzed || 0}/${d.total_pages || "?"}` +
          etaSuffixFor(li.dataset.jobId, d.total_pages);
      } else if (st === "partial_review") {
        // #127：不再以 error_message 为条件 —— "有几页没出结果"本身就是
        // 复核者最需要的信息；仅双后端差异等无 error_message 的情形同样要显示。
        base = `部分可复核 · ${d.pages_analyzed}/${d.total_pages || "?"} 页`;
      } else {
        base = `${d.total_pages || "?"} 页`;
      }
      const phase = d.phase || st;
      if (TERMINAL_STATUSES.includes(st)) {
        pages.textContent = base; // 终态：不再追加计时
        stageRows.delete(li.dataset.jobId);
      } else {
        pages.textContent = base + elapsedSuffixFor(li.dataset.jobId, phase);
        stageRows.set(li.dataset.jobId, { el: pages, base: base, phase: phase });
      }
    }
    // cr-19：错误行实时显示失败原因（旧实现只有红点"出错"，原因需点进复核页）
    // #127 扩展：partial_review 同样显示 —— 否则"有页失败"在界面上只剩一个
    // 色点，用户无从分辨"记录有问题"还是"这次根本没分析成功"。
    if (err) {
      if ((st === "error" || st === "partial_review") && d.error_message) {
        err.textContent = d.error_message;
        err.classList.remove("hidden");
      } else {
        err.classList.add("hidden");
      }
    }
    // cr-19：实际使用的 OCR 后端（failover/自愈后留痕，GMP 追溯可见）
    if (ocrTag) {
      if (d.ocr_backend_used) {
        ocrTag.textContent = `OCR: ${d.ocr_backend_display || d.ocr_backend_used}`;
        ocrTag.classList.remove("hidden");
      } else {
        ocrTag.classList.add("hidden");
      }
    }
    // #134(P0)：摘要行也要跟着实时快照更新 —— 旧实现只写 .job-pages，
    // 失败页数永远停在冷加载那一刻（通常还没有失败页）。
    // 只在**确实有 failed_pages 快照**时改写，避免覆盖掉
    // `buildRowFromSnapshot` 从旧行继承来的 created_at（SSE 快照不带该字段）。
    const metaEl = li.querySelector(".job-meta");
    if (metaEl && Array.isArray(d.failed_pages) && d.failed_pages.length) {
      metaEl.textContent = buildMetaLine({
        id: li.dataset.jobId || "",
        created_at: createdAtFromMeta(li),
        failed_pages: d.failed_pages,
      });
    }
  }

  // 用 SSE 快照重建终态行（filename/created_at 从旧行保留）
  function buildRowFromSnapshot(li, d) {
    const old = li;
    const job = {
      id: d.id || old.dataset.jobId,
      filename: old.dataset.filename || d.id,
      created_at: createdAtFromMeta(old),
      status: d.status,
      total_pages: d.total_pages || 0,
      ocr_progress: d.ocr_progress || {},
      self_heal_progress: d.self_heal_progress || null,
      cross_progress: d.cross_progress || null,
      phase: d.phase || "",
      pages_analyzed: d.pages_analyzed || 0,
      ocr_backend_used: d.ocr_backend_used || "",
      ocr_backend_display: d.ocr_backend_display || d.ocr_backend_used || "",
      error_message: d.error_message || "",
      // #134(P0)：failed_pages 此前**没被透传**（只在静态渲染路径里有），
      // 而 updateJobRowLive 走的是这条函数 ⇒ 实时阶段失败页数直接消失。
      // 接口契约是数组（`_parse_failed_pages` 已解过 JSON），这里按数组取用，
      // 非数组一律退化为空数组（**不**把字符串当数组用）。
      failed_pages: Array.isArray(d.failed_pages) ? d.failed_pages : [],
    };
    return renderJobRow(job, 0);
  }

  // 翻页前关闭全部实时连接（旧页 job 不在视口内，继续订阅无意义）
  function closeAllLiveSources() {
    if (liveHandle) {
      liveHandle.close();
      liveHandle = null;
    }
    pollTimers.forEach((t) => clearInterval(t));
    pollTimers.clear();
    stopStageTicker();
  }

  /* 摘要行文案（任务号 · 时间 · 失败页 N 页（…））。
   *
   * #134(P0)：抽成单一函数 —— 之前"静态渲染"与"实时更新"各写一份，
   * 而实时那份**压根没写 meta**（`updateJobRowLive` 只动 `.job-pages`），
   * 于是任务一进入实时更新，失败页数就消失，用户再也看不到哪几页失败。
   * 两条路径共用本函数后，这种漂移在结构上不可能再发生。
   */
  function buildMetaLine(job) {
    const failedPages = Array.isArray(job.failed_pages) ? job.failed_pages : [];
    return (
      `${job.id} · ${job.created_at}` +
      (failedPages.length
        ? ` · 失败页 ${failedPages.length} 页（${failedPages
            .slice(0, 12)
            .join(",")}${failedPages.length > 12 ? "…" : ""}）`
        : "")
    );
  }

  function renderJobRow(job, i) {
    const st = job.status;
    const stZh = statusZh(st);
    const canArchive = [
      "review",
      "partial_review",
      "error",
      "cancelled",
      "done",
    ].includes(st);
    // 与后端 _ACTIVE_STATUSES 对齐：运行中状态禁用删除（防孤儿 pipeline task）
    const isActive = ACTIVE_STATUSES.includes(st);
    // Security: build DOM via createElement + textContent to fully neutralize
    // XSS from job.id / job.filename.  Previous version used innerHTML with
    // inline onclick="archiveJob('${esc(...)}')" — esc() encoded ' as &#39;
    // but the HTML parser decodes entities BEFORE the JS engine parses the
    // attribute, so a filename like `x');alert(document.cookie);//.pdf`
    // broke out of the JS string literal.  In Electron this would be RCE.
    const li = document.createElement("li");
    li.className = "stagger-in group hover:bg-muted/20 transition-colors";
    li.style.setProperty("--i", String(i + 3));
    li.dataset.jobId = job.id;
    li.dataset.filename = job.filename;
    li.dataset.status = st;

    const row = document.createElement("div");
    row.className = "flex items-center gap-4 px-5 py-3.5";

    const link = document.createElement("a");
    link.href = `/jobs/${encodeURIComponent(job.id)}/review`;
    link.className = "flex-1 min-w-0 flex items-center gap-4";

    const info = document.createElement("div");
    info.className = "flex-1 min-w-0";
    const titleEl = document.createElement("div");
    titleEl.className = "text-[13px] font-medium truncate";
    titleEl.textContent = job.filename;
    const metaEl = document.createElement("div");
    metaEl.className =
      "text-[11px] text-muted-foreground mt-0.5 tabular-nums job-meta";
    // #127：失败页数直接进摘要行 —— 接口一直在返回 failed_pages
    // （api/jobs/status.py），但界面此前**零处**渲染，用户看不到是哪几页。
    // #134：抽成 buildMetaLine 供实时路径复用 —— 否则"静态行有失败页数、
    // 实时更新后消失"这种漂移会一直存在（本缺陷的成因就是两条路径各写各的）。
    metaEl.textContent = buildMetaLine(job);
    // cr-19：出错任务行直接显示失败原因（快照已透传 error_message，
    // 旧实现 renderJobRow 不渲染 — 用户看不到错误，只能点进复核页）
    // #127：partial_review 也显示 —— 这是"0 条 finding 到底是记录没问题、
    // 还是压根没分析成功"能否被正确解读的关键。
    const errEl = document.createElement("div");
    errEl.className =
      "job-error text-[11px] text-destructive mt-0.5 truncate";
    errEl.textContent = job.error_message || "";
    errEl.title = job.error_message || "";
    if (!["error", "partial_review"].includes(st) || !job.error_message)
      errEl.classList.add("hidden");
    info.appendChild(titleEl);
    info.appendChild(metaEl);
    info.appendChild(errEl);

    const statusWrap = document.createElement("div");
    statusWrap.className = "flex items-center gap-3 shrink-0";
    const statusEl = document.createElement("span");
    statusEl.className =
      "inline-flex items-center gap-1.5 text-[11px] text-muted-foreground";
    const dot = document.createElement("span");
    // status-dot / status-text / job-pages: live SSE updates locate these
    dot.className = statusDotClassName(st);
    statusEl.appendChild(dot);
    const stTextSpan = document.createElement("span");
    stTextSpan.className = "status-text";
    stTextSpan.textContent = stZh;
    statusEl.appendChild(stTextSpan);
    const pagesEl = document.createElement("span");
    pagesEl.className =
      "text-[11px] text-muted-foreground tabular-nums job-pages";
    // OCR 进行中且有实时进度时显示 "OCR 12/51"（后端 jobs.ocr_progress）
    const prog = job.ocr_progress || {};
    const sh = job.self_heal_progress;
    if (
      (st === "ocr_running" || st === "ocr_done") &&
      sh && sh.total > 0
    ) {
      pagesEl.textContent = `空页自愈 ${sh.done}/${sh.total}`;
    } else if ((st === "ocr_running" || st === "ocr_done") && prog.total > 0) {
      // 分片模式（MinerU + OCR_SLICES>1）下分析与 OCR 并行 — 同时显示两路进度
      pagesEl.textContent =
        (job.pages_analyzed || 0) > 0
          ? `OCR ${prog.done}/${prog.total} · 分析 ${job.pages_analyzed || 0}/${job.total_pages || "?"}`
          : `OCR ${prog.done}/${prog.total}`;
    } else if (st === "analyzing" && job.phase === "cross") {
      pagesEl.textContent = `跨页分析中 · ${job.pages_analyzed || 0}/${job.total_pages || "?"} 页`;
    } else {
      pagesEl.textContent = `${job.total_pages || "?"} 页`;
    }
    // cr-19：实际使用的 OCR 后端标签（failover 后与配置不同，GMP 追溯可见）
    const ocrTagEl = document.createElement("span");
    ocrTagEl.className =
      "job-ocr-backend text-[11px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground ml-2";
    ocrTagEl.textContent = job.ocr_backend_used
      ? `OCR: ${job.ocr_backend_display || job.ocr_backend_used}`
      : "";
    if (!job.ocr_backend_used) ocrTagEl.classList.add("hidden");
    statusWrap.appendChild(statusEl);
    statusWrap.appendChild(pagesEl);
    statusWrap.appendChild(ocrTagEl);

    link.appendChild(info);
    link.appendChild(statusWrap);

    const actions = document.createElement("div");
    actions.className =
      "flex items-center gap-1 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity";

    const archiveBtn = document.createElement("button");
    archiveBtn.type = "button";
    archiveBtn.textContent = "归档";
    archiveBtn.className = `btn-press text-[11px] px-2 py-1 rounded ${canArchive ? "text-muted-foreground hover:text-foreground hover:bg-muted" : "text-muted-foreground/30 cursor-not-allowed"}`;
    archiveBtn.title = canArchive
      ? "归档（保留数据，从列表移除）"
      : "任务处理中，无法归档";
    archiveBtn.disabled = !canArchive;
    archiveBtn.dataset.action = "archive";

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.textContent = "删除";
    // 运行中任务（pending/analyzing 等）：禁用删除，避免孤儿 pipeline task
    // 与归档按钮 canArchive 逻辑对齐，hover tooltip 说明原因和操作路径
    if (isActive) {
      deleteBtn.className =
        "btn-press text-[11px] px-2 py-1 rounded text-muted-foreground/30 cursor-not-allowed";
      deleteBtn.title = `任务${stZh}中，请先取消并等待进入终态后再删除`;
      deleteBtn.disabled = true;
    } else {
      deleteBtn.className =
        "btn-press text-[11px] px-2 py-1 rounded text-muted-foreground hover:text-destructive hover:bg-destructive/5 transition-colors";
      deleteBtn.title = "彻底删除（含 PDF 文件，不可恢复）";
    }
    deleteBtn.dataset.action = "delete";

    actions.appendChild(archiveBtn);
    actions.appendChild(deleteBtn);

    row.appendChild(link);
    row.appendChild(actions);
    li.appendChild(row);
    return li;
  }

  function renderPagination() {
    const pagEl = document.getElementById("history-pagination");
    if (!pagEl) return;
    if (totalPages <= 1) {
      pagEl.classList.add("hidden");
      return;
    }
    pagEl.classList.remove("hidden");
    const atFirst = currentPage <= 1;
    const atLast = currentPage >= totalPages;
    pagEl.innerHTML = `
      <div class="flex items-center justify-between px-5 py-3 border-t border-border text-[11px] text-muted-foreground">
        <span class="tabular-nums">第 ${currentPage} 页 / 共 ${totalPages} 页 · ${totalJobs} 份</span>
        <div class="flex items-center gap-1">
          <button onclick="goToPage(1)" ${atFirst ? "disabled" : ""}
            class="btn-press focus-ring px-2 py-1 rounded ${atFirst ? "text-muted-foreground/30 cursor-not-allowed" : "hover:text-foreground hover:bg-muted/50 transition-colors"}">首页</button>
          <button onclick="goToPage(${currentPage - 1})" ${atFirst ? "disabled" : ""}
            class="btn-press focus-ring px-2 py-1 rounded ${atFirst ? "text-muted-foreground/30 cursor-not-allowed" : "hover:text-foreground hover:bg-muted/50 transition-colors"}">← 上一页</button>
          <span class="px-2 py-1 tabular-nums text-foreground/70">${currentPage} / ${totalPages}</span>
          <button onclick="goToPage(${currentPage + 1})" ${atLast ? "disabled" : ""}
            class="btn-press focus-ring px-2 py-1 rounded ${atLast ? "text-muted-foreground/30 cursor-not-allowed" : "hover:text-foreground hover:bg-muted/50 transition-colors"}">下一页 →</button>
          <button onclick="goToPage(${totalPages})" ${atLast ? "disabled" : ""}
            class="btn-press focus-ring px-2 py-1 rounded ${atLast ? "text-muted-foreground/30 cursor-not-allowed" : "hover:text-foreground hover:bg-muted/50 transition-colors"}">末页</button>
        </div>
      </div>`;
  }

  window.goToPage = function (page) {
    if (page < 1 || page > totalPages || page === currentPage) return;
    loadHistory(page);
  };

  // === Toast + 确认弹窗（共享实现 confirm-dialog.js）===
  // P1-5: 薄别名（页面 API 名不变，调用点零改动）。函数声明（非 const）
  // 避免 TDZ，顶层/回调皆安全。
  //
  // P1-9: `window.PBC.*` 的**可用性**由 `pbc-fallback.js` 统一兜底（只补缺失
  // 方法、与加载顺序无关，见其头部注释），故此处不必再判存在 —— 降级语义
  // 只写一处，避免多份副本漂移。
  function showToast(msg, type) {
    return window.PBC.showToast(msg, type);
  }
  function confirmDialog(opts) {
    return window.PBC.confirmDialog(opts);
  }

  // === Job 归档/删除 (事件委托，DOM 淡出) ===
  // 用事件委托替代全局 window.archiveJob/deleteJob — 避免内联 onclick 字符串
  // 拼接导致的 XSS 风险，且对动态渲染的 DOM 自然生效。
  async function archiveJob(jobId, filename) {
    const ok = await confirmDialog({
      title: `归档 "${filename}"？`,
      message: `归档后将从列表移除，但数据保留，可在下方"已归档"区查看。`,
      confirmText: "归档",
    });
    if (!ok) return;
    log("archiveJob", { jobId, filename });
    try {
      const r = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/archive`, {
        method: "POST",
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      log("archiveJob — success", data);
      fadeOutJobRow(jobId);
      showToast(`已归档 ${filename}`, "ok");
      // Refresh archived list — only if already expanded (lazy load)
      if (archivedLoaded) loadArchivedList();
      else updateArchivedCount();
    } catch (err) {
      log.err("archiveJob failed", err);
      showToast(`归档失败: ${err.message}`, "err");
    }
  }

  async function deleteJob(jobId, filename, status) {
    const stZh = statusZh(status);
    const ok = await confirmDialog({
      title: `彻底删除 "${filename}"？`,
      message: `此操作不可恢复，将删除：\n• PDF 原文件\n• 所有 OCR 数据\n• 所有问题记录\n• 审计日志`,
      confirmText: "删除",
      danger: true,
      statusBadge: stZh
        ? { text: stZh, dotClass: statusDotClass(status) }
        : null,
    });
    if (!ok) return;
    log("deleteJob", { jobId, filename });
    try {
      const r = await fetch(
        `/api/jobs/${encodeURIComponent(jobId)}?keep_pdf=false`,
        { method: "DELETE" },
      );
      if (!r.ok) {
        // 解析后端 detail，409 时含可操作引导文案
        let detail = "HTTP " + r.status;
        try {
          const errBody = await r.json();
          detail = errBody.detail || detail;
        } catch {}
        throw new Error(detail);
      }
      const data = await r.json();
      log("deleteJob — success", data);
      fadeOutJobRow(jobId);
      showToast(`已删除 ${filename}`, "ok");
    } catch (err) {
      log.err("deleteJob failed", err);
      showToast(`删除失败: ${err.message}`, "err");
    }
  }

  function fadeOutJobRow(jobId) {
    const row = document.querySelector(
      `#history-list li[data-job-id="${CSS.escape(jobId)}"]`,
    );
    if (!row) return;
    row.style.transition = "opacity 0.3s, height 0.3s, padding 0.3s";
    row.style.opacity = "0";
    setTimeout(() => {
      row.style.height = "0";
      row.style.padding = "0";
      row.style.overflow = "hidden";
      setTimeout(() => {
        row.remove();
        // Update count
        totalJobs = Math.max(0, totalJobs - 1);
        const countEl = document.getElementById("history-count");
        if (countEl) countEl.textContent = `${totalJobs} 份`;
        // If current page is now empty, reload — go back if page > 1,
        // otherwise reload page 1 to show empty-state or new data.
        const remaining = document.querySelectorAll(
          "#history-list li[data-job-id]",
        ).length;
        if (remaining === 0) {
          const targetPage = currentPage > 1 ? currentPage - 1 : 1;
          loadHistory(targetPage);
        }
      }, 300);
    }, 300);
  }

  // 事件委托 — 一个监听器处理所有动态按钮（archive/delete/unarchive/delete-archived）
  function handleListClick(e) {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const li = btn.closest("li[data-job-id]");
    if (!li) return;
    const jobId = li.dataset.jobId;
    const filename = li.dataset.filename || "";
    const status = li.dataset.status || "";
    const action = btn.dataset.action;
    if (action === "archive") archiveJob(jobId, filename);
    else if (action === "delete") deleteJob(jobId, filename, status);
    else if (action === "unarchive") unarchiveJob(jobId, filename);
    else if (action === "delete-archived") deleteArchivedJob(jobId, filename);
  }

  const historyListEl = document.getElementById("history-list");
  if (historyListEl) historyListEl.addEventListener("click", handleListClick);
  const archivedListEl = document.getElementById("archived-list");
  if (archivedListEl) archivedListEl.addEventListener("click", handleListClick);

  // === 已归档列表 ===
  let archivedLoaded = false;
  async function loadArchivedList() {
    const listEl = document.getElementById("archived-list");
    const countEl = document.getElementById("archived-count");
    // 与 loadHistory 同款：归档区是可选区块，容器缺失时静默跳过，
    // 不能让它把调用方（归档/删除后的刷新）一起带崩。
    if (!listEl) {
      log.warn("loadArchivedList — 容器缺失，跳过渲染");
      return;
    }
    try {
      const r = await fetch("/api/jobs/archived/list");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      if (countEl) countEl.textContent = data.count > 0 ? `(${data.count})` : "";
      log("loadArchivedList", { count: data.count });
      listEl.innerHTML = "";
      if (data.archived.length === 0) {
        const empty = document.createElement("li");
        empty.className =
          "px-5 py-6 text-center text-[12px] text-muted-foreground";
        empty.textContent = "暂无归档记录";
        listEl.appendChild(empty);
        return;
      }
      const frag = document.createDocumentFragment();
      for (const job of data.archived) frag.appendChild(renderArchivedRow(job));
      listEl.appendChild(frag);
    } catch (err) {
      log.err("loadArchivedList failed", err);
      listEl.innerHTML = "";
      const errLi = document.createElement("li");
      errLi.className = "px-5 py-6 text-center text-[12px] text-destructive";
      errLi.textContent = "加载失败";
      listEl.appendChild(errLi);
    }
  }

  // 仅刷新计数（归档区未展开时使用，避免拉取完整列表）
  async function updateArchivedCount() {
    const countEl = document.getElementById("archived-count");
    if (!countEl) return;
    try {
      const r = await fetch("/api/jobs/archived/list");
      if (!r.ok) return;
      const data = await r.json();
      countEl.textContent = data.count > 0 ? `(${data.count})` : "";
    } catch (err) {
      // 静默失败 — count 是次要 UI
      log.warn("updateArchivedCount failed", err);
    }
  }

  function renderArchivedRow(job) {
    const li = document.createElement("li");
    li.className = "group hover:bg-muted/20 transition-colors";
    li.dataset.jobId = job.id;
    li.dataset.filename = job.filename;

    const row = document.createElement("div");
    row.className = "flex items-center gap-4 px-5 py-3";

    const link = document.createElement("a");
    link.href = `/jobs/${encodeURIComponent(job.id)}/review`;
    link.className = "flex-1 min-w-0 flex items-center gap-4";

    const info = document.createElement("div");
    info.className = "flex-1 min-w-0";
    const titleEl = document.createElement("div");
    titleEl.className =
      "text-[13px] font-medium truncate text-muted-foreground";
    titleEl.textContent = job.filename;
    const metaEl = document.createElement("div");
    metaEl.className =
      "text-[11px] text-muted-foreground mt-0.5 tabular-nums";
    metaEl.textContent = `${job.id} · ${job.created_at}`;
    info.appendChild(titleEl);
    info.appendChild(metaEl);

    const pagesEl = document.createElement("span");
    pagesEl.className = "text-[11px] text-muted-foreground";
    pagesEl.textContent = `${job.total_pages || "?"} 页`;

    link.appendChild(info);
    link.appendChild(pagesEl);

    const actions = document.createElement("div");
    actions.className =
      "flex items-center gap-1 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity";

    const unarchiveBtn = document.createElement("button");
    unarchiveBtn.type = "button";
    unarchiveBtn.textContent = "恢复";
    unarchiveBtn.className =
      "btn-press text-[11px] px-2 py-1 rounded text-muted-foreground hover:text-foreground hover:bg-muted transition-colors";
    unarchiveBtn.title = "取消归档，恢复到列表";
    unarchiveBtn.dataset.action = "unarchive";

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.textContent = "删除";
    deleteBtn.className =
      "btn-press text-[11px] px-2 py-1 rounded text-muted-foreground hover:text-destructive hover:bg-destructive/5 transition-colors";
    deleteBtn.title = "彻底删除（不可恢复）";
    deleteBtn.dataset.action = "delete-archived";

    actions.appendChild(unarchiveBtn);
    actions.appendChild(deleteBtn);

    row.appendChild(link);
    row.appendChild(actions);
    li.appendChild(row);
    return li;
  }

  async function unarchiveJob(jobId, filename) {
    const ok = await confirmDialog({
      title: `恢复 "${filename}" 到历史记录？`,
      message: "取消归档后，该记录将重新出现在历史记录列表中。",
      confirmText: "恢复",
    });
    if (!ok) return;
    log("unarchiveJob", { jobId, filename });
    try {
      const r = await fetch(
        `/api/jobs/${encodeURIComponent(jobId)}/unarchive`,
        { method: "POST" },
      );
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      log("unarchiveJob — success", data);
      showToast(`已恢复 ${filename}`, "ok");
      // Refresh both lists
      loadHistory(currentPage);
      loadArchivedList();
    } catch (err) {
      log.err("unarchiveJob failed", err);
      showToast(`恢复失败: ${err.message}`, "err");
    }
  }

  async function deleteArchivedJob(jobId, filename) {
    const ok = await confirmDialog({
      title: `彻底删除已归档的 "${filename}"？`,
      message: "⚠️ 此操作不可恢复，将永久删除该记录及其所有关联数据。",
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    log("deleteArchivedJob", { jobId, filename });
    try {
      const r = await fetch(
        `/api/jobs/${encodeURIComponent(jobId)}?keep_pdf=false`,
        { method: "DELETE" },
      );
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      log("deleteArchivedJob — success", data);
      showToast(`已删除 ${filename}`, "ok");
      // 直接 reload 列表 — 之前的 fade setTimeout(300) 会被 loadArchivedList
      // 的 innerHTML 替换打断，导致视觉跳跃。改为直接重新渲染。
      loadArchivedList();
    } catch (err) {
      log.err("deleteArchivedJob failed", err);
      showToast(`删除失败: ${err.message}`, "err");
    }
  }

  // === 归档区展开时加载 ===
  const archivedDetails = document.querySelector("#archived-section details");
  if (archivedDetails) {
    archivedDetails.addEventListener("toggle", () => {
      if (archivedDetails.open && !archivedLoaded) {
        archivedLoaded = true;
        loadArchivedList();
      }
    });
  }

  // === 上传成功后刷新列表（供 upload.js / e2e 调用） ===
  window.refreshHistory = function () {
    loadHistory(1);
  };

  // 页面卸载（跳转 review / 关闭窗口）时关闭实时订阅，避免 EventSource 泄漏
  // （SSE 连接本体由 PbcSse 注册 beforeunload 清理；这里额外清轮询定时器
  // 与 stage ticker，pagehide 在 bfcache 场景比 beforeunload 更可靠）
  window.addEventListener("pagehide", closeAllLiveSources);
  window.addEventListener("beforeunload", closeAllLiveSources);

  // === 初始化：加载第一页 ===
  loadHistory(1);
})();
