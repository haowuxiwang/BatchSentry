/* ============================================================
   Upload page — file upload + job archive/delete interactions
   依赖：window.__PBC__.jobs_count（可选，仅用于日志）
   ============================================================ */
(function () {
  "use strict";

  // === PBC Upload Page Logger ===
  const log = (...args) =>
    console.log("%c[PBC]", "color:#0ea5e9;font-weight:bold", ...args);
  log.warn = (...args) =>
    console.warn("%c[PBC]", "color:#f59e0b;font-weight:bold", ...args);
  log.err = (...args) =>
    console.error("%c[PBC]", "color:#ef4444;font-weight:bold", ...args);

  const ctx = window.__PBC__ || {};
  const input = document.getElementById("file-input");
  const area = document.getElementById("upload-area");
  const status = document.getElementById("status");

  log("upload.html loaded", {
    has_input: !!input,
    has_area: !!area,
    has_status: !!status,
    jobs_count: ctx.jobs_count || 0,
  });

  if (!input || !area || !status) {
    log.err("Critical DOM elements missing — interactions will fail", {
      input,
      area,
      status,
    });
  }

  function setStatus(text, kind) {
    const colors = {
      info: "text-foreground",
      ok: "text-emerald-600",
      err: "text-destructive",
    };
    log("setStatus()", {
      kind,
      text: typeof text === "string" ? text.slice(0, 100) : text,
    });
    // Security: use textContent to neutralize any HTML in the text.
    // Previously used innerHTML with file.name / err.message interpolated,
    // which allowed a malicious filename like '<img src=x onerror=alert(1)>.pdf'
    // to execute arbitrary script.
    status.innerHTML = "";
    const p = document.createElement("p");
    p.className = colors[kind] || colors.info;
    p.textContent = String(text);
    status.appendChild(p);
  }

  if (input) {
    input.addEventListener("change", (e) => {
      const file = e.target.files[0];
      log("file-input change event", {
        hasFile: !!file,
        fileName: file?.name,
        fileSize: file?.size,
      });
      if (file) uploadFile(file);
    });
  }

  if (area) {
    area.addEventListener("dragover", (e) => {
      e.preventDefault();
      log("dragover on upload-area");
      area.classList.add("border-primary", "bg-accent/40");
      area.classList.remove("border-border");
    });
    area.addEventListener("dragleave", () => {
      log("dragleave on upload-area");
      area.classList.remove("border-primary", "bg-accent/40");
      area.classList.add("border-border");
    });
    area.addEventListener("drop", (e) => {
      e.preventDefault();
      const file = e.dataTransfer.files[0];
      log("drop on upload-area", {
        hasFile: !!file,
        fileName: file?.name,
        fileSize: file?.size,
      });
      area.classList.remove("border-primary", "bg-accent/40");
      area.classList.add("border-border");
      if (file) uploadFile(file);
    });
  }

  function uploadFile(file) {
    log("uploadFile() called", {
      name: file.name,
      size: file.size,
      sizeMB: (file.size / 1024 / 1024).toFixed(2),
      type: file.type,
      lastModified: new Date(file.lastModified).toISOString(),
    });
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      log.warn("uploadFile — rejected (not .pdf)", file.name);
      setStatus("仅支持 PDF 文件", "err");
      return;
    }
    if (file.size > 200 * 1024 * 1024) {
      log.warn("uploadFile — rejected (too large)", {
        sizeMB: (file.size / 1024 / 1024).toFixed(1),
        limitMB: 200,
      });
      setStatus("文件超过 200MB 上限", "err");
      return;
    }
    // 前端预检：未配置 LLM 时引导用户先去设置（与后端 400 拦截双保险）
    if (ctx.needs_setup) {
      log.warn("uploadFile — blocked (needs_setup)");
      confirmDialog({
        title: "未配置 LLM 服务商",
        message:
          "上传后无法进行结构化分析，请先前往「设置」完成 API Key 配置。",
        confirmText: "前往设置",
        cancelText: "取消",
      }).then((ok) => {
        if (ok) window.location.href = "/settings";
      });
      return;
    }
    const mb = (file.size / 1024 / 1024).toFixed(1);
    setStatus(`上传中 ${file.name} (${mb} MB)...`, "info");
    const fd = new FormData();
    fd.append("file", file);
    log("uploadFile — XHR POST /api/jobs", {
      formDataEntries: [...fd.entries()].map(([k, v]) => [
        k,
        v instanceof File ? v.name : v,
      ]),
    });

    // 禁用上传区域，防止重复提交
    const dropZone = document.getElementById("upload-area");
    if (dropZone) dropZone.style.pointerEvents = "none";

    // 使用 XHR 替代 fetch，以获取 upload progress 事件
    const progressBar = document.getElementById("upload-progress");
    const progressText = document.getElementById("upload-progress-text");
    const progressPct = document.getElementById("upload-progress-pct");
    const progressFill = document.getElementById("upload-progress-fill");
    if (progressBar) {
      progressBar.classList.remove("hidden");
      if (progressFill) progressFill.style.width = "0%";
      if (progressPct) progressPct.textContent = "0%";
    }

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");

    // 上传进度（大文件反馈关键）
    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable && progressBar) {
        const pct = Math.round((e.loaded / e.total) * 100);
        if (progressFill) progressFill.style.width = pct + "%";
        if (progressPct) progressPct.textContent = pct + "%";
        if (progressText) {
          const loadedMB = (e.loaded / 1024 / 1024).toFixed(1);
          const totalMB = (e.total / 1024 / 1024).toFixed(1);
          progressText.textContent = `上传中 ${loadedMB}/${totalMB} MB`;
        }
        log("upload progress", { pct, loaded: e.loaded, total: e.total });
      }
    });

    xhr.onload = () => {
      log("uploadFile — response", {
        status: xhr.status,
        ok: xhr.status >= 200 && xhr.status < 300,
      });
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const data = JSON.parse(xhr.responseText);
          log("uploadFile — success response", data);
          if (data.job_id) {
            if (progressBar) progressBar.classList.add("hidden");
            setStatus(`Job 已创建 ${data.job_id}，1.5s 后跳转复核页...`, "ok");
            const target = `/jobs/${data.job_id}/review`;
            log("uploadFile — scheduling redirect", { target, delayMs: 1500 });
            setTimeout(() => {
              log("uploadFile — redirecting now", target);
              window.location.href = target;
            }, 1500);
          } else {
            log.err("uploadFile — response missing job_id", data);
            setStatus(`上传失败: ${JSON.stringify(data)}`, "err");
            if (progressBar) progressBar.classList.add("hidden");
            if (dropZone) dropZone.style.pointerEvents = "";
          }
        } catch (err) {
          log.err("uploadFile — JSON parse failed", err);
          setStatus(`解析响应失败: ${err}`, "err");
          if (progressBar) progressBar.classList.add("hidden");
          if (dropZone) dropZone.style.pointerEvents = "";
        }
      } else {
        log.err("uploadFile — HTTP error", xhr.status, xhr.responseText);
        // robustness-C7: 展示后端返回的 detail（如"未配置 LLM/OCR"等中文
        // 友好提示），此前仅显示状态码，用户无法得知具体原因。
        let detail = "";
        try {
          const body = JSON.parse(xhr.responseText || "{}");
          if (body && body.detail) detail = `: ${String(body.detail)}`;
        } catch (_) { /* 非 JSON 响应（网关/代理错误），忽略 */ }
        setStatus(`上传失败: HTTP ${xhr.status}${detail}`, "err");
        if (progressBar) progressBar.classList.add("hidden");
        if (dropZone) dropZone.style.pointerEvents = "";
      }
    };

    xhr.onerror = () => {
      log.err("uploadFile — XHR network error");
      setStatus(`网络错误: 上传失败`, "err");
      if (progressBar) progressBar.classList.add("hidden");
      if (dropZone) dropZone.style.pointerEvents = "";
    };

    xhr.send(fd);
  }

  // === Job 历史记录 AJAX 加载 ===

  const STATUS_ZH = {
    pending: "待处理",
    confirmed: "已确认",
    rejected: "已拒绝",
    corrected: "已修正",
    queued: "排队中",
    processing: "处理中",
    review: "可复核",
    partial_review: "部分可复核",
    error: "出错",
    cancelled: "已取消",
    cancelling: "取消中",
    ocr_running: "识别中",
    ocr_done: "识别完成",
    analyzing: "分析中",
    done: "已完成",
    archived: "已归档",
  };

  function statusDotClass(st) {
    if (["review", "partial_review", "done"].includes(st)) return "bg-success";
    if (st === "error") return "bg-destructive";
    if (["cancelled", "cancelling", "archived"].includes(st))
      return "bg-muted-foreground/40";
    return "bg-info";
  }

  // 与后端 _ACTIVE_STATUSES 对齐：运行中状态禁用删除（防孤儿 pipeline task）
  const ACTIVE_STATUSES = [
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
    listEl.innerHTML =
      '<li class="px-5 py-8 text-center text-[12px] text-muted-foreground">加载中…</li>';
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
      countEl.textContent = `${totalJobs} 份`;
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

  let liveSource = null; // single aggregated EventSource
  const pollTimers = new Map(); // jobId -> interval (fallback polling after SSE loss)

  // === Job 行实时状态（SSE 聚合） ===
  // 单条 /api/jobs/live 连接推送所有活跃任务快照，按 job_id 分发到行内。
  // 对比逐任务 EventSource：HTTP/1.1 每域 6 连接上限下，多任务并行 +
  // 多标签页不会饿死普通请求。断线 3 次后降级为逐 job 10s 轮询。
  function startLiveTracking() {
    if (liveSource) return;
    const es = new EventSource("/api/jobs/live");
    liveSource = es;
    es.onmessage = (e) => {
      let d;
      try {
        d = JSON.parse(e.data);
      } catch (_) {
        return;
      }
      const jobs = Array.isArray(d) ? d : d.jobs || [];
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
    };
    let errCount = 0;
    es.onerror = () => {
      errCount += 1;
      if (errCount >= 3) {
        es.close();
        liveSource = null;
        log.warn("SSE aggregated stream lost, falling back to polling");
        startFallbackPolling();
      }
    };
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
            if (!row) return;
            const r = await fetch(`/api/jobs/${encodeURIComponent(jid)}`);
            if (!r.ok) return;
            const d = await r.json();
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
    const dot = li.querySelector(".status-dot");
    const stText = li.querySelector(".status-text");
    const pages = li.querySelector(".job-pages");
    if (dot) dot.className = `w-1.5 h-1.5 rounded-full ${statusDotClass(st)}`;
    if (stText) stText.textContent = STATUS_ZH[st] || st;
    if (pages) {
      const prog = d.ocr_progress || {};
      if ((st === "ocr_running" || st === "ocr_done") && prog.total > 0) {
        pages.textContent = `OCR ${prog.done}/${prog.total}`;
      } else if (st === "analyzing") {
        pages.textContent = `分析 ${d.pages_analyzed || 0}/${d.total_pages || "?"}`;
      } else if (st === "partial_review" && d.error_message) {
        pages.textContent = `部分可复核 · ${d.pages_analyzed}/${d.total_pages || "?"} 页`;
      } else {
        pages.textContent = `${d.total_pages || "?"} 页`;
      }
    }
  }

  // 用 SSE 快照重建终态行（filename/created_at 从旧行保留）
  function buildRowFromSnapshot(li, d) {
    const old = li;
    const job = {
      id: d.id || old.dataset.jobId,
      filename: old.dataset.filename || d.id,
      created_at: old.querySelector(".job-meta")?.textContent.split("· ")[1] || "",
      status: d.status,
      total_pages: d.total_pages || 0,
      ocr_progress: d.ocr_progress || {},
      error_message: d.error_message || "",
    };
    return renderJobRow(job, 0);
  }

  // 翻页前关闭全部实时连接（旧页 job 不在视口内，继续订阅无意义）
  function closeAllLiveSources() {
    if (liveSource) {
      liveSource.close();
      liveSource = null;
    }
    pollTimers.forEach((t) => clearInterval(t));
    pollTimers.clear();
  }

  function renderJobRow(job, i) {
    const st = job.status;
    const stZh = STATUS_ZH[st] || st;
    const dotCls = statusDotClass(st);
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
    metaEl.textContent = `${job.id} · ${job.created_at}`;
    info.appendChild(titleEl);
    info.appendChild(metaEl);

    const statusWrap = document.createElement("div");
    statusWrap.className = "flex items-center gap-3 shrink-0";
    const statusEl = document.createElement("span");
    statusEl.className =
      "inline-flex items-center gap-1.5 text-[11px] text-muted-foreground";
    const dot = document.createElement("span");
    // status-dot / status-text / job-pages: live SSE updates locate these
    dot.className = `w-1.5 h-1.5 rounded-full ${dotCls} status-dot`;
    statusEl.appendChild(dot);
    const stTextSpan = document.createElement("span");
    stTextSpan.className = "status-text";
    stTextSpan.textContent = stZh;
    statusEl.appendChild(stTextSpan);
    const pagesEl = document.createElement("span");
    pagesEl.className =
      "text-[11px] text-muted-foreground/70 tabular-nums job-pages";
    // OCR 进行中且有实时进度时显示 "OCR 12/51"（后端 jobs.ocr_progress）
    const prog = job.ocr_progress || {};
    if ((st === "ocr_running" || st === "ocr_done") && prog.total > 0) {
      pagesEl.textContent = `OCR ${prog.done}/${prog.total}`;
    } else {
      pagesEl.textContent = `${job.total_pages || "?"} 页`;
    }
    statusWrap.appendChild(statusEl);
    statusWrap.appendChild(pagesEl);

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
        "btn-press text-[11px] px-2 py-1 rounded text-muted-foreground/60 hover:text-destructive hover:bg-destructive/5 transition-colors";
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

  // === Toast 提示 ===
  function showToast(msg, type = "info") {
    let container = document.getElementById("toast-container");
    if (!container) {
      container = document.createElement("div");
      container.id = "toast-container";
      container.className = "fixed bottom-4 right-4 z-50 flex flex-col gap-2";
      document.body.appendChild(container);
    }
    const color =
      type === "ok"
        ? "text-success"
        : type === "err"
          ? "text-destructive"
          : "text-info";
    const toast = document.createElement("div");
    toast.className = `bg-card border border-border rounded-md px-4 py-2.5 text-[12px] ${color} shadow-md`;
    toast.style.cssText = "animation: fade-in-up 0.2s ease-out both;";
    toast.textContent = msg;
    container.appendChild(toast);
    setTimeout(() => {
      toast.style.transition = "opacity 0.3s, transform 0.3s";
      toast.style.opacity = "0";
      toast.style.transform = "translateY(4px)";
      setTimeout(() => toast.remove(), 300);
    }, 3000);
  }

  // === Notion 风格确认弹窗（替代原生 confirm()）===
  // 返回 Promise<boolean>，支持 Esc 取消 / Enter 确认 / 点击遮罩取消
  // danger=true 时确认按钮用 destructive 色调（用于删除等不可恢复操作）
  // statusBadge 可选：{ text, dotClass } — 在标题下方展示任务状态徽章
  function confirmDialog({
    title,
    message = "",
    confirmText = "确认",
    cancelText = "取消",
    danger = false,
    statusBadge = null,
  }) {
    return new Promise((resolve) => {
      let settled = false;
      const settle = (val) => {
        if (settled) return;
        settled = true;
        overlay.style.transition = "opacity 0.15s ease-out";
        modal.style.transition =
          "opacity 0.15s ease-out, transform 0.15s ease-out";
        overlay.style.opacity = "0";
        modal.style.transform = "scale(0.96)";
        modal.style.opacity = "0";
        setTimeout(() => {
          overlay.remove();
          document.removeEventListener("keydown", onKey);
          document.body.style.overflow = "";
          resolve(val);
        }, 150);
      };

      const overlay = document.createElement("div");
      overlay.className =
        "fixed inset-0 z-[60] flex items-center justify-center px-4";
      overlay.style.cssText =
        "background: hsl(222 47% 11% / 0.32); backdrop-filter: blur(2px); animation: fade-in-up 0.15s ease-out both;";

      const modal = document.createElement("div");
      modal.className =
        "bg-card border border-border rounded-lg w-full max-w-sm overflow-hidden";
      modal.style.cssText =
        "box-shadow: var(--shadow-strong); animation: pbc-modal-in 0.2s cubic-bezier(0.2, 0.9, 0.1, 1) both;";

      const body = document.createElement("div");
      body.className = "px-5 py-4";
      const titleEl = document.createElement("div");
      titleEl.className = "text-[14px] font-medium";
      titleEl.textContent = title;
      body.appendChild(titleEl);
      // 状态徽章 — 展示任务当前状态，让用户预判操作后果
      if (statusBadge) {
        const badge = document.createElement("div");
        badge.className =
          "inline-flex items-center gap-1.5 mt-2 text-[11px] text-muted-foreground bg-muted/50 px-2 py-0.5 rounded";
        const dot = document.createElement("span");
        dot.className = `w-1.5 h-1.5 rounded-full ${statusBadge.dotClass}`;
        badge.appendChild(dot);
        badge.appendChild(
          document.createTextNode(`当前状态：${statusBadge.text}`),
        );
        body.appendChild(badge);
      }
      if (message) {
        const msgEl = document.createElement("div");
        msgEl.className =
          "text-[12px] text-muted-foreground mt-1.5 whitespace-pre-line leading-relaxed";
        msgEl.textContent = message;
        body.appendChild(msgEl);
      }
      modal.appendChild(body);

      const footer = document.createElement("div");
      footer.className =
        "flex items-center justify-end gap-2 px-5 py-3 border-t border-border bg-muted/30";
      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.textContent = cancelText;
      cancelBtn.className =
        "btn-press focus-ring text-[12px] px-3 py-1.5 rounded text-muted-foreground hover:text-foreground";
      const confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.textContent = confirmText;
      confirmBtn.className = danger
        ? "btn-press focus-ring text-[12px] px-3 py-1.5 rounded bg-destructive text-destructive-foreground hover:bg-destructive/90 font-medium"
        : "btn-press focus-ring text-[12px] px-3 py-1.5 rounded bg-primary text-primary-foreground hover:bg-primary/90 font-medium";
      cancelBtn.addEventListener("click", () => settle(false));
      confirmBtn.addEventListener("click", () => settle(true));
      footer.appendChild(cancelBtn);
      footer.appendChild(confirmBtn);
      modal.appendChild(footer);

      overlay.appendChild(modal);
      // 点击遮罩空白处取消（点击 modal 本身不取消）
      overlay.addEventListener("click", (e) => {
        if (e.target === overlay) settle(false);
      });
      document.body.appendChild(overlay);
      document.body.style.overflow = "hidden";

      const onKey = (e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          settle(false);
        } else if (e.key === "Enter") {
          // 安全规范：Enter 行为跟随当前焦点。
          // 焦点在按钮上时，让浏览器默认行为处理（button Enter → click），
          // 这样危险操作默认聚焦 cancel，Enter 会触发 cancel 而非 confirm。
          // 焦点不在按钮时（如刚打开未 Tab），非危险操作默认确认（用户期望），
          // 危险操作默认取消（安全兜底）。
          if (
            document.activeElement === cancelBtn ||
            document.activeElement === confirmBtn
          ) {
            return; // 不 preventDefault，让浏览器触发 button click
          }
          e.preventDefault();
          settle(danger ? false : true);
        }
      };
      document.addEventListener("keydown", onKey);

      // 安全规范：危险操作默认聚焦"取消"按钮，防止回车误触删除
      // （APG/Polaris/Material 3 一致规范）。用户需主动 Tab 到确认按钮
      // 并按 Enter 才会确认，或鼠标点击确认按钮。
      setTimeout(() => cancelBtn.focus(), 50);
    });
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
    const stZh = STATUS_ZH[status] || status || "";
    const ok = await confirmDialog({
      title: `彻底删除 "${filename}"？`,
      message: `此操作不可恢复，将删除：\n• PDF 原文件\n• 所有 OCR 数据\n• 所有 findings\n• 审计日志`,
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
        document.getElementById("history-count").textContent =
          `${totalJobs} 份`;
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
    try {
      const r = await fetch("/api/jobs/archived/list");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      countEl.textContent = data.count > 0 ? `(${data.count})` : "";
      log("loadArchivedList", { count: data.count });
      listEl.innerHTML = "";
      if (data.archived.length === 0) {
        const empty = document.createElement("li");
        empty.className =
          "px-5 py-6 text-center text-[12px] text-muted-foreground/60";
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
      "text-[11px] text-muted-foreground/70 mt-0.5 tabular-nums";
    metaEl.textContent = `${job.id} · ${job.created_at}`;
    info.appendChild(titleEl);
    info.appendChild(metaEl);

    const pagesEl = document.createElement("span");
    pagesEl.className = "text-[11px] text-muted-foreground/50";
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
      "btn-press text-[11px] px-2 py-1 rounded text-muted-foreground/60 hover:text-destructive hover:bg-destructive/5 transition-colors";
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

  // === 上传成功后刷新列表 ===
  window.refreshHistory = function () {
    loadHistory(1);
  };

  // 页面卸载（跳转 review / 关闭窗口）时关闭实时订阅，避免 EventSource 泄漏
  window.addEventListener("pagehide", closeAllLiveSources);
  window.addEventListener("beforeunload", closeAllLiveSources);

  // === 初始化：加载第一页 ===
  loadHistory(1);

  // 捕获全局错误
  window.addEventListener("error", (e) => {
    log.err("window.error", {
      message: e.message,
      filename: e.filename,
      lineno: e.lineno,
      colno: e.colno,
      error: e.error,
    });
  });
  window.addEventListener("unhandledrejection", (e) => {
    log.err("unhandledrejection", e.reason);
  });

  // === 暴露共享原语到 window.PBC 命名空间 ===
  // review.js / settings.js 通过 <script src="/static/confirm-dialog.js"> 加载
  // 独立副本；upload.html 不引入该文件，故在此导出本页定义的 confirmDialog，
  // 供未来跨页面复用（showToast 为本页私有，未导出）。
  window.PBC = window.PBC || {};
  window.PBC.confirmDialog = confirmDialog;
})();
