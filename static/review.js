/* ============================================================
   Review page — interactions
   依赖：服务端通过 Jinja2 注入全局变量到 window.__PBC__
   ============================================================ */
(function () {
  "use strict";

  // === PBC Review Page Logger ===
  // 统一前缀 [PBC] 便于控制台过滤；level 用颜色区分便于视觉定位
  const log = (...args) =>
    console.log("%c[PBC]", "color:#0ea5e9;font-weight:bold", ...args);
  log.warn = (...args) =>
    console.warn("%c[PBC]", "color:#f59e0b;font-weight:bold", ...args);
  log.err = (...args) =>
    console.error("%c[PBC]", "color:#ef4444;font-weight:bold", ...args);

  // 服务端注入的上下文（避免在 JS 中混写 Jinja2 语法）
  const ctx = window.__PBC__ || {};
  const jobId = ctx.job_id || "";
  let currentPage = ctx.page || 1;
  const totalPages = ctx.total_pages || 1;
  const pdfUrl = ctx.pdf_url || "";
  const pageFindingCounts = ctx.page_finding_counts || {};

  // 页面初始化信息（一次性 dump 上下文）
  log("review.html loaded", {
    job_id: jobId,
    filename: ctx.filename,
    status: ctx.status,
    page: currentPage,
    total_pages: totalPages,
    findings_count: ctx.findings_count,
    severity_counts: ctx.severity_counts,
    has_measurements: ctx.has_measurements,
    matrix_shape: ctx.matrix_shape,
    pdf_url: pdfUrl,
    page_parse_error: ctx.page_parse_error,
    page_confidence: ctx.page_confidence,
  });

  // DOM 就绪后探测 E2E-required 元素，方便快速排查模板渲染问题
  document.addEventListener("DOMContentLoaded", () => {
    // PDF iframe 加载完成后淡出 loading 指示器
    const pdfFrame = document.getElementById("pdf-frame");
    const pdfLoading = document.getElementById("pdf-loading");
    if (pdfFrame && pdfLoading) {
      let hidden = false;
      const hideLoading = () => {
        if (hidden) return;
        hidden = true;
        pdfLoading.classList.add("is-loaded");
        log("PDF iframe loaded — hiding spinner");
      };
      pdfFrame.addEventListener("load", hideLoading);
      // 兜底：6s 后强制隐藏（部分 PDF 插件不触发 load）
      setTimeout(hideLoading, 6000);
    }

    // === SSE 实时进度订阅 ===
    // 非终态时订阅 /api/jobs/{id}/stream，每 2s 收到进度更新
    // 终态时服务端推送 done 事件并关闭流
    const initialStatus = ctx.status;
    const terminalStatuses = [
      "review",
      "partial_review",
      "error",
      "cancelled",
      "archived",
    ];

    if (jobId && !terminalStatuses.includes(initialStatus)) {
      subscribeProgress(jobId);
    }

    function subscribeProgress(jid) {
      const bar = document.getElementById("progress-bar-container");
      const txt = document.getElementById("progress-text");
      const fill = document.getElementById("progress-fill");
      if (!bar || !txt || !fill) return;

      bar.classList.remove("hidden");
      bar.classList.add("inline-flex");
      txt.textContent = "连接中...";

      const url = `/api/jobs/${jid}/stream`;
      log("SSE subscribe", url);
      const es = new EventSource(url);

      es.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data);
          const total = d.total_pages || 0;
          let pct = 0;
          let label = d.status;

          // 计算进度百分比
          if (d.status === "pending") {
            pct = 0;
            label = "排队中";
          } else if (d.status === "ocr_running" || d.status === "ocr_done") {
            pct = total > 0 ? Math.round((d.pages_ocr_done / total) * 33) : 0;
            label = `OCR ${d.pages_ocr_done}/${total}`;
          } else if (d.status === "analyzing") {
            pct =
              33 +
              (total > 0 ? Math.round((d.pages_analyzed / total) * 60) : 0);
            label = `分析 ${d.pages_analyzed}/${total}`;
          } else if (d.status === "review" || d.status === "partial_review") {
            pct = 100;
            label = "完成";
          }

          fill.style.width = pct + "%";
          txt.textContent = label;
          log("SSE progress", { status: d.status, pct, label });
        } catch (err) {
          log.warn("SSE parse error", err);
        }
      };

      es.addEventListener("done", (e) => {
        log("SSE done — closing stream, reloading page");
        es.close();
        // 终态：1.5s 后自动刷新页面，加载最终 findings
        setTimeout(() => location.reload(), 1500);
      });

      es.onerror = () => {
        log.warn("SSE connection error — will retry on next reload");
        es.close();
      };

      // 页面卸载时清理 SSE 连接
      window.addEventListener("beforeunload", () => es.close());
    }

    const probes = {
      "critical-banner": document.querySelectorAll(".critical-banner").length,
      "findings-list-critical": document.querySelectorAll(
        ".findings-list-critical",
      ).length,
      "source-badge": document.querySelectorAll(".source-badge").length,
      "page-link": document.querySelectorAll(".page-link").length,
      "severity-summary": document.querySelectorAll(".severity-summary").length,
      "finding-card": document.querySelectorAll(".finding-card").length,
      "parse-error-banner": document.querySelectorAll(".parse-error-banner")
        .length,
      "confidence-badge-row": document.querySelectorAll(".confidence-badge-row")
        .length,
    };
    log("DOMContentLoaded — DOM probe", probes);

    // 验证 critical-pulse 3s 动画规则是否被浏览器解析
    try {
      const sheets = [...document.styleSheets];
      let found = false;
      for (const s of sheets) {
        try {
          for (const rule of s.cssRules || []) {
            if (rule.cssText && rule.cssText.includes("critical-pulse 3s")) {
              found = true;
              log(
                "CSS rule matched: critical-pulse 3s",
                rule.cssText.slice(0, 120),
              );
            }
          }
        } catch (e) {
          // 跨域 stylesheet 无法读取 cssRules，忽略
        }
      }
      if (!found)
        log.warn(
          "critical-pulse 3s 规则未在可访问样式表中找到（可能是跨域 CSS）",
        );
    } catch (e) {
      log.warn("styleSheet 探测失败", e);
    }
  });

  // 按钮加载状态管理 — 防止重复点击
  function setButtonLoading(btn, loading, originalText) {
    if (!btn) return;
    if (loading) {
      btn.dataset.originalText = btn.textContent;
      btn.disabled = true;
      btn.classList.add(
        "opacity-50",
        "cursor-not-allowed",
        "pointer-events-none",
      );
      btn.textContent = "处理中…";
    } else {
      btn.disabled = false;
      btn.classList.remove(
        "opacity-50",
        "cursor-not-allowed",
        "pointer-events-none",
      );
      btn.textContent =
        originalText || btn.dataset.originalText || btn.textContent;
      delete btn.dataset.originalText;
    }
  }

  // 翻页期间全局加载指示器
  let pageLoadingOverlay = null;
  function showPageLoading() {
    if (pageLoadingOverlay) return;
    const center = document.querySelector("section.flex-1.border-t");
    if (!center) return;
    pageLoadingOverlay = document.createElement("div");
    pageLoadingOverlay.className =
      "absolute inset-0 flex items-center justify-center bg-background/60 z-20 transition-opacity";
    pageLoadingOverlay.innerHTML = '<div class="pdf-spinner"></div>';
    center.style.position = "relative";
    center.appendChild(pageLoadingOverlay);
  }
  function hidePageLoading() {
    if (pageLoadingOverlay) {
      pageLoadingOverlay.remove();
      pageLoadingOverlay = null;
    }
  }

  // 更新页码导航选中态（无整页刷新）— 左侧指示条 + 文字加粗
  function updatePageNavActive(targetPage) {
    document.querySelectorAll(".page-nav-item").forEach((el) => {
      const pageNum = parseInt(el.dataset.page);
      const isActive = pageNum === targetPage;
      // 移除所有选中态 class
      el.classList.remove("text-foreground", "font-medium");
      el.classList.remove("text-muted-foreground", "hover:text-foreground");
      // 添加对应 class
      if (isActive) {
        el.classList.add("text-foreground", "font-medium");
        // 添加左侧指示条
        let indicator = el.querySelector(".nav-indicator");
        if (!indicator) {
          indicator = document.createElement("span");
          indicator.className =
            "nav-indicator absolute left-0 top-1 bottom-1 w-0.5 bg-foreground";
          el.appendChild(indicator);
        }
        // 滚动到可见
        el.scrollIntoView({ block: "nearest", behavior: "smooth" });
      } else {
        el.classList.add("text-muted-foreground", "hover:text-foreground");
        // 移除指示条
        const indicator = el.querySelector(".nav-indicator");
        if (indicator) indicator.remove();
      }
    });
  }

  // 更新 PDF 区域页码显示
  function updatePdfDisplay(targetPage) {
    const pageNumEl = document.getElementById("page-num");
    if (pageNumEl) pageNumEl.textContent = targetPage;
    // 更新 "X / Y" 显示
    const counterEl = document.querySelector(
      ".text-sm.text-muted-foreground.font-mono.tabular-nums.w-16",
    );
    if (counterEl) counterEl.textContent = `${targetPage} / ${totalPages}`;
    // 更新 iframe src（PDF 内部跳转，不重新加载）
    const iframe = document.getElementById("pdf-frame");
    if (iframe) {
      iframe.src = pdfUrl + "#page=" + targetPage;
    }
    // 更新翻页按钮 disabled 状态
    document.querySelectorAll('[onclick^="goPage"]').forEach((btn) => {
      const match = btn.getAttribute("onclick").match(/goPage\((\d+)\)/);
      if (match) {
        const target = parseInt(match[1]);
        btn.disabled = target < 1 || target > totalPages;
      }
    });
  }

  // AJAX 加载页面数据（findings + OCR + measurements）
  async function loadPageData(targetPage) {
    log("loadPageData", { target: targetPage });
    showPageLoading();
    // 翻页期间禁用所有翻页按钮，防止重复点击
    document
      .querySelectorAll('[onclick^="goPage"]')
      .forEach((b) => (b.disabled = true));
    try {
      const r = await fetch(`/api/jobs/${jobId}/pages/${targetPage}`);
      if (!r.ok) throw new Error("HTTP " + r.status);
      const pageData = await r.json();

      // 加载该页的 findings
      const fr = await fetch(`/api/jobs/${jobId}/findings?page=${targetPage}`);
      if (!fr.ok) throw new Error("HTTP " + fr.status);
      const findingsData = await fr.json();

      // 更新 URL（不刷新页面）
      history.pushState(
        { page: targetPage },
        "",
        `/jobs/${jobId}/review?page=${targetPage}`,
      );

      // 更新页码导航 + PDF + 翻页按钮
      updatePageNavActive(targetPage);
      updatePdfDisplay(targetPage);

      // 更新 OCR 文本
      const ocrEl = document.getElementById("ocr-text");
      if (ocrEl && pageData.raw_html) {
        // 去除 HTML 标签 — 用 DOMParser 避免设置 innerHTML 时
        // 触发 <img onerror=...> 等事件处理器（XSS 防御）
        const doc = new DOMParser().parseFromString(
          pageData.raw_html,
          "text/html",
        );
        ocrEl.textContent = (doc.body.textContent || "")
          .replace(/\s+/g, " ")
          .trim()
          .slice(0, 5000);
      }

      // 更新 findings 列表（重新渲染）
      renderFindings(findingsData.findings || []);

      currentPage = targetPage;
      log("loadPageData — success", {
        page: targetPage,
        findings: findingsData.count,
      });
    } catch (err) {
      log.err("loadPageData failed", err);
      // 降级：整页刷新
      window.location.href = `/jobs/${jobId}/review?page=${targetPage}`;
    } finally {
      hidePageLoading();
      // 恢复翻页按钮状态
      updatePdfDisplay(currentPage);
    }
  }

  // 渲染 findings 列表
  function renderFindings(findings) {
    const list = document.getElementById("findings-list");
    if (!list) return;
    const severityZh = { critical: "严重", warning: "警告", info: "信息" };
    const sourceZh = {
      rule: "规则",
      llm_page: "LLM单页",
      llm_fallback: "LLM兜底",
      llm_cross: "LLM跨页",
    };
    const statusZh = {
      pending: "待复核",
      confirmed: "已确认",
      rejected: "已拒绝",
      corrected: "已修正",
    };

    // Security: escape all attacker-controlled text before injecting as HTML.
    // f.type / f.description / f.ocr_text come from LLM or rule output and
    // could contain <script> tags otherwise.
    const esc = (s) =>
      String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");

    if (findings.length === 0) {
      list.innerHTML =
        '<div class="py-8 text-center text-[13px] text-muted-foreground">本页无问题</div>';
      return;
    }

    list.innerHTML = findings
      .map((f, i) => {
        const sevDot =
          f.severity === "critical"
            ? "bg-destructive"
            : f.severity === "warning"
              ? "bg-warning"
              : "bg-info";
        const statusOpacity =
          f.status === "confirmed"
            ? "opacity-50"
            : f.status === "rejected"
              ? "opacity-40"
              : "";
        const statusTag =
          f.status !== "pending"
            ? `<span class="text-[10px] text-muted-foreground">· ${esc(statusZh[f.status] || f.status)}</span>`
            : "";
        const sourceTag =
          f.source && f.source !== "rule"
            ? `<span class="text-[10px] text-muted-foreground">· ${esc(sourceZh[f.source] || f.source)}</span>`
            : "";
        const ocrSnippet = f.ocr_text
          ? `<p class="text-[11px] text-muted-foreground/70 font-mono mt-1 truncate">OCR：${esc(f.ocr_text.slice(0, 100))}</p>`
          : "";
        // f.id is INTEGER from DB; coerce to Number to prevent string injection
        const fid = Number(f.id);
        const actionBtns =
          f.status === "pending"
            ? `
                <div class="action-btns mt-1.5 flex items-center gap-2">
                    <button onclick="updateFinding(${fid}, 'confirmed')" class="btn-press text-[11px] font-medium text-foreground hover:text-muted-foreground">确认</button>
                    <span class="text-muted-foreground/30">·</span>
                    <button onclick="updateFinding(${fid}, 'rejected')" class="btn-press text-[11px] font-medium text-muted-foreground hover:text-foreground">拒绝</button>
                    <span class="text-muted-foreground/30">·</span>
                    <button onclick="correctFinding(${fid})" class="btn-press text-[11px] font-medium text-muted-foreground hover:text-foreground">修正</button>
                </div>`
            : "";
        return `
                <div id="finding-${fid}" class="finding-card stagger-in hover-lift border-b border-border last:border-b-0 ${statusOpacity} py-2.5 px-1" style="--i: ${i}">
                    <div class="flex items-start gap-2">
                        <span class="w-1.5 h-1.5 rounded-full ${sevDot} mt-[7px] shrink-0"></span>
                        <div class="flex-1 min-w-0">
                            <div class="flex items-center gap-2 mb-0.5">
                                <span class="text-[13px] font-medium text-foreground">${esc(f.type)}</span>
                                ${statusTag}
                                ${sourceTag}
                                <span class="text-[10px] text-muted-foreground uppercase tracking-wider ml-auto">${esc(severityZh[f.severity] || f.severity)}</span>
                            </div>
                            <p class="text-[13px] text-muted-foreground leading-relaxed">${esc(f.description)}</p>
                            ${ocrSnippet}
                            ${actionBtns}
                        </div>
                    </div>
                </div>`;
      })
      .join("");
  }

  function goPage(p) {
    log("goPage() called", {
      target: p,
      current: currentPage,
      max: totalPages,
    });
    if (p < 1 || p > totalPages) {
      log.warn("goPage — out of range, aborted", { p, max: totalPages });
      return;
    }
    if (p === currentPage) {
      log("goPage — same page, aborted");
      return;
    }
    loadPageData(p);
  }

  // === 上下文操作: 取消 / 重试 ===
  async function cancelJob() {
    if (!confirm("确定取消此任务？处理中的数据会保留，可稍后重试。")) return;
    log("cancelJob");
    const btn = event && event.currentTarget ? event.currentTarget : null;
    setButtonLoading(btn, true);
    try {
      const r = await fetch(`/api/jobs/${jobId}/cancel`, { method: "POST" });
      const data = await r.json();
      if (data.ok) {
        log("cancelJob — success", data);
        setTimeout(() => location.reload(), 800);
      } else {
        throw new Error(data.message || "取消失败");
      }
    } catch (err) {
      log.err("cancelJob failed", err);
      setButtonLoading(btn, false, "取消任务");
      alert("取消失败: " + err.message);
    }
  }

  async function retryJob() {
    if (!confirm("确定重试此任务？将从中断处继续处理。")) return;
    log("retryJob");
    const btn = event && event.currentTarget ? event.currentTarget : null;
    setButtonLoading(btn, true);
    try {
      const r = await fetch(`/api/jobs/${jobId}/retry`, { method: "POST" });
      const data = await r.json();
      if (data.ok) {
        log("retryJob — success", data);
        setTimeout(() => location.reload(), 800);
      } else {
        throw new Error(data.detail || data.message || "重试失败");
      }
    } catch (err) {
      log.err("retryJob failed", err);
      setButtonLoading(btn, false, "重试");
      alert("重试失败: " + err.message);
    }
  }

  function toggleOcr() {
    const el = document.getElementById("ocr-text");
    const btn = document.getElementById("ocr-toggle-btn");
    const gradient = document.getElementById("ocr-gradient");
    if (!el) return;
    const collapsed = el.style.maxHeight === "200px" || !el.style.maxHeight;
    if (collapsed) {
      el.style.maxHeight = "none";
      el.style.overflow = "auto";
      if (btn) btn.textContent = "收起";
      if (gradient) gradient.style.display = "none";
    } else {
      el.style.maxHeight = "200px";
      el.style.overflow = "hidden";
      if (btn) btn.textContent = "展开更多";
      if (gradient) gradient.style.display = "flex";
    }
    log("toggleOcr —", collapsed ? "expanded" : "collapsed");
  }

  function updateFinding(findingId, status) {
    log("updateFinding() called", { findingId, status });
    const btn = event && event.currentTarget ? event.currentTarget : null;
    setButtonLoading(btn, true);
    // 同时禁用同行其他操作按钮，防止交叉操作
    const row = document.getElementById("finding-" + findingId);
    if (row) {
      row
        .querySelectorAll(".action-btns button")
        .forEach((b) => (b.disabled = true));
    }
    const url = "/api/jobs/" + jobId + "/findings/" + findingId;
    const body = "status=" + status;
    log("updateFinding — fetch", { url, method: "POST", body });
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
    })
      .then((r) => {
        log("updateFinding — response", { status: r.status, ok: r.ok });
        if (!r.ok) {
          return r.text().then((txt) => {
            log.err("updateFinding — HTTP error body", txt);
            throw new Error("HTTP " + r.status);
          });
        }
        return r.json();
      })
      .then((data) => {
        log("updateFinding — success", data);
        const el = document.getElementById("finding-" + findingId);
        if (el) {
          el.classList.add(status);
          log("updateFinding — class applied", {
            id: "finding-" + findingId,
            classAdded: status,
          });
        } else {
          log.warn("updateFinding — element not found", "finding-" + findingId);
        }
        log("updateFinding — reloading page");
        location.reload();
      })
      .catch((err) => {
        log.err("updateFinding — fetch failed", err);
        // 恢复按钮
        if (row) {
          row
            .querySelectorAll(".action-btns button")
            .forEach((b) => (b.disabled = false));
        }
        setButtonLoading(btn, false, status === "confirmed" ? "确认" : "拒绝");
        alert("更新失败: " + err.message + "\n请查看控制台排查");
      });
  }

  function correctFinding(findingId) {
    log("correctFinding() called", { findingId });
    const text = prompt("输入修正后的文本:");
    log("correctFinding — prompt result", {
      text: text ? text.slice(0, 80) + (text.length > 80 ? "…" : "") : null,
    });
    if (!text) {
      log("correctFinding — user cancelled (empty input)");
      return;
    }
    const btn = event && event.currentTarget ? event.currentTarget : null;
    setButtonLoading(btn, true);
    const row = document.getElementById("finding-" + findingId);
    if (row) {
      row
        .querySelectorAll(".action-btns button")
        .forEach((b) => (b.disabled = true));
    }
    const url = "/api/jobs/" + jobId + "/findings/" + findingId;
    const body = "status=corrected&corrected_text=" + encodeURIComponent(text);
    log("correctFinding — fetch", {
      url,
      method: "POST",
      bodyLength: body.length,
    });
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
    })
      .then((r) => {
        log("correctFinding — response", { status: r.status, ok: r.ok });
        if (!r.ok) {
          return r.text().then((txt) => {
            log.err("correctFinding — HTTP error body", txt);
            throw new Error("HTTP " + r.status);
          });
        }
        return r.json();
      })
      .then((data) => {
        log("correctFinding — success", data);
        log("correctFinding — reloading page");
        location.reload();
      })
      .catch((err) => {
        log.err("correctFinding — fetch failed", err);
        if (row) {
          row
            .querySelectorAll(".action-btns button")
            .forEach((b) => (b.disabled = false));
        }
        setButtonLoading(btn, false, "修正");
        alert("修正失败: " + err.message + "\n请查看控制台排查");
      });
  }

  // 暴露到全局（onclick 处理器需要）
  window.goPage = goPage;
  window.cancelJob = cancelJob;
  window.retryJob = retryJob;
  window.toggleOcr = toggleOcr;
  window.updateFinding = updateFinding;
  window.correctFinding = correctFinding;

  // 初始化：检查 OCR 内容是否溢出，不溢出则隐藏展开按钮
  window.addEventListener("DOMContentLoaded", () => {
    const el = document.getElementById("ocr-text");
    const gradient = document.getElementById("ocr-gradient");
    if (el && gradient) {
      el.style.maxHeight = "200px";
      el.style.overflow = "hidden";
      // 内容未溢出则隐藏渐变和按钮
      if (el.scrollHeight <= 200) {
        gradient.style.display = "none";
      }
    }
  });

  // 键盘快捷键: ← → 翻页
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
    if (e.key === "ArrowLeft") goPage(currentPage - 1);
    else if (e.key === "ArrowRight") goPage(currentPage + 1);
  });

  // 捕获全局错误，便于发现模板/Jinja 渲染或异步异常
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
})();
