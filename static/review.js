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
  // total_pages=0 表示 OCR 尚未完成（真实页数未知），显示 "?" 而非 1
  let totalPages = ctx.total_pages || 0;
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
    page_parse_error: ctx.page_parse_error,
    page_confidence: ctx.page_confidence,
  });

  // DOM 就绪后探测 E2E-required 元素，方便快速排查模板渲染问题
  document.addEventListener("DOMContentLoaded", () => {
    // 初始渲染当前页 PNG（替代 iframe 原生 PDF viewer —
    // 无浏览器打印/下载/更多操作按钮，缩放 fit-width 可控）
    const pdfImg = document.getElementById("pdf-page-img");
    const pdfLoading = document.getElementById("pdf-loading");
    if (pdfImg && pdfLoading) {
      pdfImg.onload = () => pdfLoading.classList.add("is-loaded");
      pdfImg.onerror = () => {
        pdfLoading.classList.add("is-loaded");
        log.err("PDF initial render failed");
      };
      updatePdfDisplay(currentPage);
      // 兜底：6s 后强制隐藏（渲染失败/极慢时不永久遮挡）
      setTimeout(() => pdfLoading.classList.add("is-loaded"), 6000);
    }

    // === SSE 实时进度订阅 ===
    // 非终态时订阅 /api/jobs/{id}/stream，每 3s 收到进度更新
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

      let retryCount = 0;
      const MAX_RETRIES = 3;
      let pollTimer = null;
      let es = null;

      const connect = () => {
        const url = `/api/jobs/${jid}/stream`;
        log("SSE subscribe", url);
        es = new EventSource(url);

        // 流式输出：跟踪 pages_analyzed 变化，当当前页被分析完成时
        // 自动 AJAX 刷新该页 findings，让用户在 Stage 2 进行中就能看到
        // 已分析页的结果，无需等全部页完成。
        let lastPagesAnalyzed = -1;

        es.onmessage = (e) => {
          try {
            const d = JSON.parse(e.data);
            const total = d.total_pages || 0;
            // OCR 完成后 total_pages 从 0 → 51，同步标题栏（不重置 iframe）
            if (total > 0 && total !== totalPages) {
              totalPages = total;
              const label = String(total);
              const pageTotalEl = document.getElementById("page-total");
              if (pageTotalEl) pageTotalEl.textContent = label;
              const counterEl = document.getElementById("page-counter");
              if (counterEl)
                counterEl.textContent = `${currentPage} / ${label}`;
              const navTotalEl = document.getElementById("page-nav-total");
              if (navTotalEl) navTotalEl.textContent = label;
              // 同步翻页按钮状态（totalPages 已知后允许翻页）
              document
                .querySelectorAll('[onclick^="goPage"]')
                .forEach((btn) => {
                  const match = btn
                    .getAttribute("onclick")
                    .match(/goPage\((\d+)\)/);
                  if (match) {
                    const target = parseInt(match[1]);
                    btn.disabled =
                      target < 1 || target > totalPages;
                  }
                });
            }
            // 流式：OCR 完成后若仍在占位态（total_pages=0 时进入页面），
            // 重建页码导航；随后每页圆点随 findings 实时点亮
            if (d.page_finding_counts) {
              if (totalPages > 0 && !document.querySelector(".page-nav-item")) {
                buildPageNav();
              }
              updatePageNavDots(d.page_finding_counts);
            }
            let pct = 0;
            let label = d.status;

            // 流式输出（所有状态，含分片 OCR 阶段）：pages_analyzed 增长时
            // 若当前页已分析完成，静默刷新该页 findings。分片 OCR 下
            // status 仍是 ocr_running 但分析已在进行（_analyze_one 每页
            // 完成即写库），用户无需等全部页 OCR 完就看到结果。
            const analyzedCount = d.pages_analyzed || 0;
            if (
              analyzedCount > lastPagesAnalyzed &&
              analyzedCount > 0 &&
              currentPage <= analyzedCount
            ) {
              lastPagesAnalyzed = analyzedCount;
              log(
                "SSE stream — page analyzed, refreshing current page",
                { currentPage, pagesAnalyzed: analyzedCount, status: d.status },
              );
              // 静默刷新当前页 findings（不显示 loading overlay，避免干扰）
              refreshCurrentPageFindings();
            }

            // 计算进度百分比
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
                label = analyzedCount > 0
                  ? `OCR ${ocrDone}/${ocrTotal} · 分析 ${analyzedCount}/${total}`
                  : `OCR ${ocrDone}/${ocrTotal}`;
              } else if (total > 0) {
                pct = total > 0 ? Math.round((d.pages_ocr_done / total) * 33) : 0;
                label = analyzedCount > 0
                  ? `OCR ${d.pages_ocr_done}/${total} · 分析 ${analyzedCount}/${total}`
                  : `OCR ${d.pages_ocr_done}/${total}`;
              } else {
                label = "OCR 处理中…";
              }
            } else if (d.status === "analyzing") {
              pct =
                33 +
                (total > 0 ? Math.round((analyzedCount / total) * 60) : 0);
              label = `分析 ${analyzedCount}/${total}`;
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
          log.warn("SSE connection error", { retryCount });
          es.close();
          if (retryCount < MAX_RETRIES) {
            // 指数退避重试：2s / 4s / 8s
            const delay = 2000 * Math.pow(2, retryCount);
            txt.textContent = `连接断开，${delay / 1000}s 后重试…`;
            retryCount++;
            setTimeout(connect, delay);
          } else {
            // 重试耗尽：fallback 到 10s 轮询 /api/jobs/{id}
            log.warn("SSE retries exhausted, fallback to polling");
            txt.textContent = "SSE 不可用，切换轮询…";
            pollTimer = setInterval(async () => {
              try {
                const r = await fetch(`/api/jobs/${jid}`);
                if (!r.ok) return;
                const d = await r.json();
                if (terminalStatuses.includes(d.status)) {
                  clearInterval(pollTimer);
                  setTimeout(() => location.reload(), 500);
                }
              } catch (err) {
                log.warn("poll failed", err);
              }
            }, 10000);
          }
        };
      };

      connect();

      // 页面卸载时清理 SSE 连接 + 轮询定时器
      window.addEventListener("beforeunload", () => {
        if (es) es.close();
        if (pollTimer) clearInterval(pollTimer);
      });
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

  // 占位态重建页码导航 — OCR 完成前进入页面时 total_pages=0，
  // sidebar 只有占位提示；OCR 完成后 SSE 推送 total_pages 时调用，
  // 用 DOM 重建页码列表（与 Jinja 渲染结构一致），无整页刷新。
  function buildPageNav() {
    const nav = document.getElementById("page-nav");
    if (!nav || totalPages <= 0) return;
    log("buildPageNav", { totalPages });
    nav.innerHTML = "";
    for (let p = 1; p <= totalPages; p++) {
      const a = document.createElement("a");
      a.href = `/jobs/${jobId}/review?page=${p}`;
      a.dataset.page = String(p);
      a.className =
        "page-nav-item group relative flex items-center justify-between " +
        "px-3 py-1.5 text-[12px] transition-colors duration-150 rounded-sm " +
        "text-muted-foreground hover:text-foreground hover:bg-muted/50";
      a.addEventListener("click", (ev) => {
        ev.preventDefault();
        goPage(p);
      });
      const label = document.createElement("span");
      label.className = "font-mono tabular-nums";
      label.textContent = "P" + p;
      a.appendChild(label);
      nav.appendChild(a);
    }
    updatePageNavActive(currentPage);
  }

  // 流式更新页码导航圆点 — SSE 推送 page_finding_counts 时调用，
  // 每页 findings 生成后圆点立即点亮，无需等整批完成。
  // 所有文本走 textContent，无 innerHTML（XSS 防御）。
  function updatePageNavDots(counts) {
    if (!counts) return;
    document.querySelectorAll(".page-nav-item").forEach((el) => {
      const p = parseInt(el.dataset.page);
      const c = counts[p] || { critical: 0, warning: 0, info: 0, total: 0 };
      // 移除旧的圆点容器，重建当前值
      el.querySelectorAll("[data-dots]").forEach((n) => n.remove());
      const dotsEl = document.createElement("span");
      dotsEl.setAttribute("data-dots", "1");
      dotsEl.className = "flex items-center gap-1";
      if (c.total > 0) {
        if (c.critical > 0) {
          const dot = document.createElement("span");
          dot.className = "w-1 h-1 rounded-full bg-destructive";
          dot.title = c.critical + " 严重";
          dotsEl.appendChild(dot);
        }
        if (c.warning > 0) {
          const dot = document.createElement("span");
          dot.className = "w-1 h-1 rounded-full bg-warning";
          dot.title = c.warning + " 警告";
          dotsEl.appendChild(dot);
        }
        if (c.info > 0 && c.critical === 0 && c.warning === 0) {
          const n = document.createElement("span");
          n.className = "text-[10px] tabular-nums text-muted-foreground";
          n.textContent = String(c.total);
          dotsEl.appendChild(n);
        }
      }
      el.appendChild(dotsEl);
    });
  }

  // 更新页码导航选中态（无整页刷新）— 黑底白字（约束：选中页码必须黑底白字，非蓝/紫）
  function updatePageNavActive(targetPage) {
    document.querySelectorAll(".page-nav-item").forEach((el) => {
      const pageNum = parseInt(el.dataset.page);
      const isActive = pageNum === targetPage;
      // 移除所有选中态 class
      el.classList.remove("bg-foreground", "text-background", "font-medium");
      el.classList.remove(
        "text-muted-foreground",
        "hover:text-foreground",
        "hover:bg-muted/50",
      );
      // 添加对应 class
      if (isActive) {
        el.classList.add("bg-foreground", "text-background", "font-medium");
        // 滚动到可见
        el.scrollIntoView({ block: "nearest", behavior: "smooth" });
      } else {
        el.classList.add(
          "text-muted-foreground",
          "hover:text-foreground",
          "hover:bg-muted/50",
        );
      }
    });
  }

  // 更新 PDF 区域页码显示
  function updatePdfDisplay(targetPage) {
    const pageNumEl = document.getElementById("page-num");
    if (pageNumEl) pageNumEl.textContent = targetPage;
    // 更新标题栏 "第 N / M 页" 和计数器 "N / M"（totalPages=0 显示 "?"）
    const totalLabel = totalPages > 0 ? String(totalPages) : "?";
    const pageTotalEl = document.getElementById("page-total");
    if (pageTotalEl) pageTotalEl.textContent = totalLabel;
    const counterEl = document.getElementById("page-counter");
    if (counterEl) counterEl.textContent = `${targetPage} / ${totalLabel}`;
    // 渲染当前页 PNG（替代 iframe 原生 viewer — 无打印/下载/更多操作按钮，
    // 缩放由 CSS width:100% 控制，页码与渲染页严格对应）
    const img = document.getElementById("pdf-page-img");
    const loading = document.getElementById("pdf-loading");
    if (img) {
      const render = () => {
        img.src = `/api/jobs/${jobId}/page/${targetPage}`;
        if (loading) {
          loading.classList.remove("is-loaded");
          loading.querySelector("p").textContent = `正在渲染第 ${targetPage} 页 ...`;
        }
      };
      // 已缓存同一 URL 时不重复触发 loading（浏览器会从缓存加载）
      if (img.src.endsWith(`/page/${targetPage}`)) {
        img.onload = null;
        return;
      }
      img.onload = () => {
        if (loading) loading.classList.add("is-loaded");
      };
      img.onerror = () => {
        if (loading) loading.classList.add("is-loaded");
        log.err("PDF page render failed", targetPage);
      };
      render();
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

  // AJAX 加载页面数据（findings + OCR + measurements + banners）
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

      // 加载该页的 measurements 矩阵
      const mr = await fetch(
        `/api/jobs/${jobId}/pages/${targetPage}/measurements`,
      );
      const measurementsData = mr.ok ? await mr.json() : { measurements: [] };

      // 更新 URL（不刷新页面）
      history.pushState(
        { page: targetPage },
        "",
        `/jobs/${jobId}/review?page=${targetPage}`,
      );

      // 更新页码导航 + PDF + 翻页按钮
      updatePageNavActive(targetPage);
      updatePdfDisplay(targetPage);

      // 更新 OCR 文本 — htmlToText 保留表格结构（行/列分隔），
      // 纯字符串处理 + textContent，无 XSS 面
      const ocrEl = document.getElementById("ocr-text");
      if (ocrEl && pageData.raw_html) {
        ocrEl.textContent = htmlToText(pageData.raw_html);
      }

      // 更新 findings 列表（重新渲染）
      renderFindings(findingsData.findings || []);

      // 更新页面级 UI 元素：置信度 / parse-error / critical banner / measurements
      updatePageLevelUI(
        pageData,
        findingsData.findings || [],
        measurementsData,
      );

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

  // 流式输出：静默刷新当前页 findings（不显示 loading overlay）
  // 在 SSE 收到 pages_analyzed 变化时调用，让用户在 Stage 2 进行中
  // 就能看到已分析页的 findings 实时更新。
  async function refreshCurrentPageFindings() {
    try {
      const [pageRes, findingsRes] = await Promise.all([
        fetch(`/api/jobs/${jobId}/pages/${currentPage}`),
        fetch(`/api/jobs/${jobId}/findings?page=${currentPage}`),
      ]);
      if (!pageRes.ok || !findingsRes.ok) return;
      const pageData = await pageRes.json();
      const findingsData = await findingsRes.json();
      const findings = findingsData.findings || [];

      // 重新渲染 findings 列表
      renderFindings(findings);
      // 更新页面级 UI（置信度/critical banner 等）
      const mr = await fetch(
        `/api/jobs/${jobId}/pages/${currentPage}/measurements`,
      );
      const measurementsData = mr.ok ? await mr.json() : { measurements: [] };
      updatePageLevelUI(pageData, findings, measurementsData);
      log("refreshCurrentPageFindings — updated", {
        page: currentPage,
        findings: findings.length,
      });
    } catch (err) {
      log.warn("refreshCurrentPageFindings failed", err);
    }
  }

  // 翻页时更新页面级 UI：置信度徽章 / parse-error 横幅 / critical 横幅 / 参数矩阵
  // 之前 AJAX 翻页只更新 OCR + findings，导致用户看到的是上一页的置信度、
  // critical 计数和参数矩阵，对 GMP 复核构成误导。
  function updatePageLevelUI(pageData, findings, measurementsData) {
    const structured = pageData.structured || {};
    const pageConfidence = structured.overall_confidence || "";
    const pageParseError = bool(structured._parse_error);

    // 1. 置信度徽章
    const confEl = document.getElementById("page-confidence-badge");
    if (confEl) {
      if (pageConfidence && !pageParseError) {
        const confZh = { high: "高", medium: "中", low: "低" };
        confEl.textContent = `置信度 ${confZh[pageConfidence] || pageConfidence}`;
        confEl.classList.remove("hidden");
      } else {
        confEl.classList.add("hidden");
      }
    }

    // 2. parse-error 横幅
    const parseBanner = document.getElementById("parse-error-banner");
    if (parseBanner) {
      parseBanner.classList.toggle("hidden", !pageParseError);
    }

    // 3. critical 横幅 — 按当前页 findings 重新计算 critical 数量
    const criticalBanner = document.getElementById("critical-banner");
    const criticalCount = findings.filter(
      (f) => f.severity === "critical",
    ).length;
    if (criticalBanner) {
      if (criticalCount > 0) {
        const strong = criticalBanner.querySelector("strong");
        if (strong) strong.textContent = String(criticalCount);
        criticalBanner.classList.remove("hidden");
      } else {
        criticalBanner.classList.add("hidden");
      }
    }

    // 4. 参数矩阵 — 重新渲染表格
    const matrixSection = document.getElementById("measurements-section");
    const matrixBody = document.getElementById("measurements-body");
    const matrixHeader = document.getElementById("measurements-header-row");
    const matrixShape = document.getElementById("measurements-shape");
    const measurements = measurementsData.measurements || [];
    const columns = measurementsData.columns || [];

    if (matrixSection) {
      if (measurements.length > 0 && columns.length > 0) {
        // 渲染表头
        if (matrixHeader) {
          matrixHeader.innerHTML = "";
          const timeTh = document.createElement("th");
          timeTh.className =
            "text-left px-3 py-1.5 font-medium text-muted-foreground";
          timeTh.textContent = "时间";
          matrixHeader.appendChild(timeTh);
          for (const col of columns) {
            const th = document.createElement("th");
            th.className =
              "px-3 py-1.5 font-medium text-muted-foreground text-center whitespace-nowrap";
            th.textContent = col;
            matrixHeader.appendChild(th);
          }
        }
        // 渲染表体
        if (matrixBody) {
          matrixBody.innerHTML = "";
          measurements.forEach((m, i) => {
            const tr = document.createElement("tr");
            tr.className =
              "stagger-in border-b border-border/50 hover:bg-muted/50";
            tr.style.setProperty("--i", String(i));
            const timeTd = document.createElement("td");
            timeTd.className = "px-3 py-1.5 font-mono text-foreground";
            timeTd.textContent = m.time || "-";
            tr.appendChild(timeTd);
            for (const col of columns) {
              const cell = (m.values || {})[col] || {};
              const inSpec = cell.in_spec;
              const cellClass =
                inSpec === true
                  ? "cell-ok"
                  : inSpec === false
                    ? "cell-bad"
                    : "cell-unknown";
              const td = document.createElement("td");
              td.className = `px-3 py-1.5 text-center tabular-nums ${cellClass}`;
              td.title = `规格: ${cell.spec || ""} | 实测: ${cell.actual || ""} | 单位: ${cell.unit || ""}`;
              td.textContent = cell.actual || "-";
              tr.appendChild(td);
            }
            matrixBody.appendChild(tr);
          });
        }
        if (matrixShape) {
          matrixShape.textContent = `${measurements.length} × ${columns.length}`;
        }
        matrixSection.classList.remove("hidden");
      } else {
        matrixSection.classList.add("hidden");
      }
    }
  }

  // 安全的布尔转换（structured._parse_error 可能是 true/false/"true"/1 等）
  function bool(v) {
    return v === true || v === "true" || v === 1;
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
      user_rule: "用户规则",
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
                    <button onclick="updateFinding(event, ${fid}, 'confirmed')" class="btn-press text-[11px] font-medium text-foreground hover:text-muted-foreground">确认</button>
                    <span class="text-muted-foreground/30">·</span>
                    <button onclick="updateFinding(event, ${fid}, 'rejected')" class="btn-press text-[11px] font-medium text-muted-foreground hover:text-foreground">拒绝</button>
                    <span class="text-muted-foreground/30">·</span>
                    <button onclick="correctFinding(event, ${fid})" class="btn-press text-[11px] font-medium text-muted-foreground hover:text-foreground">修正</button>
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
  async function cancelJob(e) {
    const ok = await window.PBC.confirmDialog({
      title: "确定取消此任务？",
      message: "处理中的数据会保留，可稍后重试。",
      confirmText: "确认取消",
      cancelText: "保留",
      danger: true,
    });
    if (!ok) return;
    log("cancelJob");
    const btn = e && e.currentTarget ? e.currentTarget : null;
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
      window.PBC.showToast("取消失败: " + err.message, "err");
    }
  }

  async function retryJob(e) {
    const ok = await window.PBC.confirmDialog({
      title: "确定重试此任务？",
      message: "将从中断处继续处理。",
      confirmText: "确认重试",
      cancelText: "取消",
    });
    if (!ok) return;
    log("retryJob");
    const btn = e && e.currentTarget ? e.currentTarget : null;
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
      window.PBC.showToast("重试失败: " + err.message, "err");
    }
  }

  // OCRraw HTML → 可读文本：保留表格结构（行/单元格分隔），剥离标签与
  // MinerU 样式噪音（style= 属性、字面 "\n" 转义、img 长路径）。
  // 纯字符串处理 + textContent 赋值，无 innerHTML，无 XSS 面。
  function htmlToText(html) {
    if (!html) return "";
    return String(html)
      .replace(/\\n/g, "\n") // MinerU 表格单元格分隔的字面 \n
      .replace(/\\t/g, "\t")
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<img[^>]*>/gi, "[图]")
      .replace(/<\/tr>/gi, "\n")
      .replace(/<\/t[dh]>/gi, " | ")
      .replace(/<\/p>/gi, "\n")
      .replace(/<div[^>]*>/gi, "\n")
      .replace(/<[^>]+>/g, "") // 剩余标签
      .replace(/&nbsp;/gi, " ")
      .replace(/&amp;/gi, "&")
      .replace(/&lt;/gi, "<")
      .replace(/&gt;/gi, ">")
      .replace(/&quot;/gi, '"')
      .replace(/[ \t]+/g, " ") // 折叠行内空白
      .replace(/ *\| */g, " | ") // 统一单元格分隔符
      .replace(/[ \t]+\n/g, "\n") // 行尾空白
      .replace(/\n{3,}/g, "\n\n")
      .trim();
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

  function updateFinding(e, findingId, status) {
    log("updateFinding() called", { findingId, status });
    const btn = e && e.currentTarget ? e.currentTarget : null;
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
        window.PBC.showToast(
          "更新失败: " + err.message + "\n请查看控制台排查",
          "err",
        );
      });
  }

  async function correctFinding(e, findingId) {
    log("correctFinding() called", { findingId });
    const text = await window.PBC.promptDialog({
      title: "输入修正后的文本：",
      confirmText: "确认修正",
      cancelText: "取消",
    });
    log("correctFinding — prompt result", {
      text: text ? text.slice(0, 80) + (text.length > 80 ? "…" : "") : null,
    });
    if (!text) {
      log("correctFinding — user cancelled (empty input)");
      return;
    }
    const btn = e && e.currentTarget ? e.currentTarget : null;
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
        window.PBC.showToast(
          "修正失败: " + err.message + "\n请查看控制台排查",
          "err",
        );
      });
  }

  // 暴露到全局（onclick 处理器需要）
  window.goPage = goPage;
  window.cancelJob = cancelJob;
  window.retryJob = retryJob;
  window.toggleOcr = toggleOcr;
  window.updateFinding = updateFinding;
  window.correctFinding = correctFinding;

  // 初始化：OCR 文本 raw → htmlToText 可读化（data-raw 为服务端注入原文）
  window.addEventListener("DOMContentLoaded", () => {
    const el = document.getElementById("ocr-text");
    const gradient = document.getElementById("ocr-gradient");
    if (el) {
      const raw = el.getAttribute("data-raw") || "";
      el.textContent = htmlToText(raw) || "无 OCR 数据";
    }
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
