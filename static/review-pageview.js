/* ============================================================
   Review 页 — 左侧面板：PDF 页图 / 缩放 / 页码导航 / 区域锚
   （拆分自 review.js，R63 P1-A）

   区域级证据锚（P0-3）：把 finding 锚回 OCR 原始版面区域并画到当前页
   图上。只到区域级 —— Paddle/MinerU 都不回传单元格级 bbox（实测见
   docs/NOISE_REDUCTION_SPIKE.md），自称单元格级等于给复核员一个错位的框。
   ============================================================ */
(function (global) {
  "use strict";

  const R = global.PbcReview;
  const log = R.log;

  // UX P1-4: PDF 预览缩放状态（1.0 = fit-width 原样）。渲染用 CSS width
  // 百分比实现，滚动容器 #pdf-scroll 已有 overflow-auto 承接放大溢出。
  let pdfZoom = 1.0;
  const PDF_ZOOM_MIN = 0.5;
  const PDF_ZOOM_MAX = 2.0;

  let activeRegionFid = null;
  const REGION_ASPECT_TOL = 0.02;

  function applyZoom() {
    const img = document.getElementById("pdf-page-img");
    if (img) img.style.width = Math.round(pdfZoom * 100) + "%";
    const label = document.getElementById("pdf-zoom-label");
    if (label) label.textContent = Math.round(pdfZoom * 100) + "%";
    // P0-3：区域框以图片像素尺寸定位，缩放后必须重算（否则框跑偏）
    positionRegionOverlay();
  }

  function zoomPdf(delta) {
    pdfZoom = Math.min(
      PDF_ZOOM_MAX,
      Math.max(PDF_ZOOM_MIN, Math.round((pdfZoom + delta) * 100) / 100),
    );
    applyZoom();
    log("zoomPdf", { zoom: pdfZoom });
  }

  function resetZoom() {
    pdfZoom = 1.0;
    applyZoom();
  }

  // 占位态重建页码导航 — OCR 完成前进入页面时 total_pages=0，
  // sidebar 只有占位提示；OCR 完成后 SSE 推送 total_pages 时调用，
  // 用 DOM 重建页码列表（与 Jinja 渲染结构一致），无整页刷新。
  function buildPageNav() {
    const nav = document.getElementById("page-nav");
    const { jobId, totalPages, currentPage } = R.state;
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
        R.goPage(p);
      });
      const label = document.createElement("span");
      label.className = "tabular-nums";
      label.textContent = "第" + p + "页";
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
      // 对抗审查 P2：counts 无变化时跳过重建（SSE 每 3s 全量 tick，
      // 51 页 × 每页删建 DOM 纯属浪费）；以 data-dots-sig 记录上次签名。
      // ⚠️ 签名必须覆盖**渲染输出依赖的全部输入**，包括 total：info 分支
      // 显示的是 `c.total` 而非 `c.info`，且 total 可以 ≠ critical+warning
      // +info（后端 `_page_finding_counts` 对未知 severity 只累加 total，
      // severity 列无 CHECK 约束）。漏掉 total 会让数字永远停在首帧。
      const sig = `${c.critical}-${c.warning}-${c.info}-${c.total}`;
      if (el.dataset.dotsSig === sig) return;
      el.dataset.dotsSig = sig;
      // 移除旧的圆点容器，重建当前值。选择器与 SSR 模板共用同一标记
      // （templates/review.html 的容器同样带 data-dots="1"）—— 标记不一致
      // 会让服务端渲染的容器删不掉，叠加出陈旧的严重度圆点。
      el.querySelectorAll("[data-dots]").forEach((n) => n.remove());
      const dotsEl = document.createElement("span");
      dotsEl.setAttribute("data-dots", "1");
      dotsEl.className = "flex items-center gap-1";
      if (c.total > 0) {
        if (c.critical > 0) {
          const dot = document.createElement("span");
          dot.setAttribute("data-dot", "critical");
          dot.className = "w-1 h-1 rounded-full bg-destructive";
          dot.title = c.critical + " 严重";
          dotsEl.appendChild(dot);
        }
        if (c.warning > 0) {
          const dot = document.createElement("span");
          dot.setAttribute("data-dot", "warning");
          dot.className = "w-1 h-1 rounded-full bg-warning";
          dot.title = c.warning + " 警告";
          dotsEl.appendChild(dot);
        }
        if (c.info > 0 && c.critical === 0 && c.warning === 0) {
          const n = document.createElement("span");
          n.className = "text-[11px] tabular-nums text-muted-foreground";
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
    const { jobId, totalPages } = R.state;
    // 对抗审查（分发实证）：syncNavButtons 必须无条件执行 —— 此前放在
    // 函数末尾，而图片缓存命中（src 已是目标页）会提前 return 跳过它；
    // 叠加 loadPageData 中间态用旧 currentPage 计算过一次禁用位，导致
    // "从末页跳回任意页后 next 永久卡死"。箭头状态只依赖
    // currentPage/totalPages，与图片是否重新渲染无关。
    R.syncNavButtons();
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
      // 已缓存同一 URL（浏览器缓存/当前已加载）时不重复请求、不重置 loading
      if (img.src.endsWith(`/page/${targetPage}`)) {
        return;
      }
      // P0-3：翻页后旧的高亮框属于上一页，必须清掉（否则框会落在新页上）
      clearRegionAnchor();
      img.src = `/api/jobs/${jobId}/page/${targetPage}`;
      if (loading) {
        loading.classList.remove("is-loaded");
        loading.querySelector("p").textContent = `正在渲染第 ${targetPage} 页 …`;
      }
    }
  }

  // ── P0-3 区域级证据锚 ──

  function clearRegionAnchor() {
    activeRegionFid = null;
    const ov = document.getElementById("region-overlay");
    if (ov) ov.classList.add("hidden");
  }

  function positionRegionOverlay() {
    const ov = document.getElementById("region-overlay");
    const img = document.getElementById("pdf-page-img");
    if (!ov || !img || ov.classList.contains("hidden")) return;
    const bbox = (ov.dataset.bbox || "").split(",").map(Number);
    if (bbox.length !== 4 || bbox.some((v) => !Number.isFinite(v))) return;
    // 以图片自身为基准计算：放大时 img.style.width 会超过包装层 100%，
    // 用百分比定位会与实际渲染位置脱钩（缩放后框跑偏）。
    const [x0, y0, x1, y1] = bbox;
    ov.style.left = img.offsetLeft + x0 * img.offsetWidth + "px";
    ov.style.top = img.offsetTop + y0 * img.offsetHeight + "px";
    ov.style.width = (x1 - x0) * img.offsetWidth + "px";
    ov.style.height = (y1 - y0) * img.offsetHeight + "px";
  }

  // 区域锚定入口（内联 onclick 经 window.locateRegion 调入）。
  // ⚠️ 命名史：拆分前与"OCR 面板文本定位"函数同名（locateFinding），
  // 函数声明提升导致**本函数遮蔽了文本定位函数**，点击 finding 卡片
  // 的高亮定位自引入以来静默失效（R63 拆分时发现并修复，见
  // review-locate.js 头注）。改名 locateRegion 消除遮蔽。
  function locateRegion(e, findingId) {
    if (e) e.stopPropagation();
    const fid = Number(findingId);
    const ref = R.state.regionRefs[fid];
    const img = document.getElementById("pdf-page-img");
    const ov = document.getElementById("region-overlay");
    if (!ref || !img || !ov) return;
    if (activeRegionFid === fid) {
      clearRegionAnchor(); // 再点一次收起
      return;
    }
    // 宽高比闸门（兜底）：page_bbox 已是页面空间坐标，正常情况下必与渲染图
    // 同向。仍比对一次 —— 后端上报的坐标空间与页面**不同源**时（服务端转了
    // 页但没上报 angle、或上游改了行为），归一化映射必然失真。此时明示"无法
    // 定位"远好过画一个错位的框：后者会把复核员的注意力引到错误的区域，
    // 比不显示更危险。page_aspect 是后端按 rotation 推出的"页面应有宽高比"。
    const imgAspect = img.naturalWidth / img.naturalHeight;
    const pgAspect = Number(ref.page_aspect);
    if (
      pgAspect > 0 &&
      imgAspect > 0 &&
      Math.abs(pgAspect - imgAspect) / imgAspect > REGION_ASPECT_TOL
    ) {
      window.PBC.showToast(
        "该页坐标系与页面方向不一致（上游未上报旋转角），无法自动定位，请人工核对原图",
        "err",
      );
      return;
    }
    ov.dataset.bbox = (ref.page_bbox || []).join(",");
    ov.classList.remove("hidden");
    activeRegionFid = fid;
    positionRegionOverlay();
    // 区域可能在容器可视区之外（页图高于容器），滚动到它附近
    const scroll = document.getElementById("pdf-scroll");
    if (scroll && ov.offsetTop > scroll.scrollTop + scroll.clientHeight - 40) {
      scroll.scrollTop = Math.max(0, ov.offsetTop - scroll.clientHeight / 3);
    }
  }

  // P0-3：窗口尺寸变化时区域框按图片像素重算 —— overlay 用像素定位，
  // 容器宽度变了不重算会跑偏（原 review.js 拆分时随本模块迁移）
  global.addEventListener("resize", positionRegionOverlay);

  const pageview = (global.PbcReview = global.PbcReview || {});
  pageview.pageview = {
    applyZoom: applyZoom,
    zoomPdf: zoomPdf,
    resetZoom: resetZoom,
    buildPageNav: buildPageNav,
    updatePageNavDots: updatePageNavDots,
    updatePageNavActive: updatePageNavActive,
    updatePdfDisplay: updatePdfDisplay,
    clearRegionAnchor: clearRegionAnchor,
    positionRegionOverlay: positionRegionOverlay,
    locateRegion: locateRegion,
  };

  // 暴露到全局（模板内联 onclick 需要）
  global.zoomPdf = zoomPdf;
  global.resetZoom = resetZoom;
  global.locateRegion = locateRegion;
})(typeof window !== "undefined" ? window : globalThis);
