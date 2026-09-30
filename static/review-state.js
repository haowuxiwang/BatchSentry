/* ============================================================
   Review 页共享状态与工具（拆分自 review.js，R63 P1-A）

   review.js 原为 2037 行的单体（DOM 初始化/PDF 缩放/SSE 订阅/统计
   加载/findings 渲染混杂），按职责拆为多个模块文件。本文件最先加载，
   提供两样东西：

   1. PbcReview.state —— 跨模块共享的可变状态（当前页码/总页数/页面级
      标记/翻页代际 token）。拆分前它们是 review.js IIFE 的闭包变量；
      拆分后必须有一个所有模块都能访问的宿主，且**只能有一个**（否则
      两份状态必然漂移 —— 与状态映射多副本是同一类缺陷）。
   2. PbcReview 工具 —— log / bool / setButtonLoading / showPageLoading /
      hidePageLoading（各模块高频使用的基础设施）。

   依赖：服务端通过 Jinja2 注入 window.__PBC__（review.html 底部内联
   script，先于本文件执行 —— defer 脚本在 DOMContentLoaded 前按序执行）。
   ============================================================ */
(function (global) {
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
  const ctx = global.__PBC__ || {};

  const R = (global.PbcReview = global.PbcReview || {});

  // ── 共享可变状态（原 review.js IIFE 闭包变量） ──
  const state = {
    ctx: ctx,
    jobId: ctx.job_id || "",
    currentPage: ctx.page || 1,
    // 代际守卫：loadPageData / refreshCurrentPageFindings 的并发守卫_token。
    // 快速翻页时旧请求晚返回会覆盖新页内容（对抗审查发现）——响应落地前
    // 校验 token 与当前页，不匹配则丢弃。
    pageLoadToken: 0,
    // total_pages=0 表示 OCR 尚未完成（真实页数未知），显示 "?" 而非 1
    totalPages: ctx.total_pages || 0,
    // #136：**当前页**的页面级标记（由 updatePageLevelUI 每次刷新时写入）。
    // 空 findings 的文案依赖它来区分"分析失败 / 空页 / 确实无问题"。
    // 不能用 ctx：那是首屏 SSR 注入的，翻页后即过期（会把上一页的失败
    // 标记套到新页上，反之亦然）。
    currentPageFlags: {
      parseError: bool(ctx.page_parse_error),
      ocrEmpty: bool(ctx.page_ocr_empty),
    },
    // 注：曾在此镜像 `ctx.page_finding_counts`（SSR 首屏每页 finding 计数），
    // 但**无任何读取方** —— 页码圆点的真值始终来自 SSE 帧的
    // `d.page_finding_counts`（review-progress.js → updatePageNavDots）。
    // 镜像副本只会与实时帧漂移（"同一数据两处存"是状态不一致的温床），
    // 已删除。SSR 侧仍用 Jinja 变量直接渲染首屏圆点（templates/review.html），
    // 不经由本对象。
    // finding id → region_ref（渲染时收集；onclick 是内联的，无法携带对象）
    regionRefs: Object.create(null),
  };

  // ── 工具函数 ──

  // 安全的布尔转换（structured._parse_error 可能是 true/false/"true"/1 等）
  function bool(v) {
    return v === true || v === "true" || v === 1;
  }

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
    // P0-2: 原 selector "section.flex-1.border-t" 与模板中列 class
    // （flex flex-col gap-3 min-w-0）不匹配 → overlay 永不创建、翻页
    // 无反馈且旧数据残留。改用 ID 定位，消除对 class 组合的脆弱依赖。
    const center = document.getElementById("center-panel");
    if (!center) return;
    pageLoadingOverlay = document.createElement("div");
    pageLoadingOverlay.className =
      "absolute inset-0 flex items-center justify-center bg-background/60 z-20 transition-opacity";
    // P3-2: 页面形骨架（形态 = 最终 PDF 页面形状）替代 spinner
    pageLoadingOverlay.innerHTML =
      '<div class="w-56 h-80 rounded-md bg-muted/70 animate-pulse"></div>';
    center.style.position = "relative";
    center.appendChild(pageLoadingOverlay);
  }
  function hidePageLoading() {
    if (pageLoadingOverlay) {
      pageLoadingOverlay.remove();
      pageLoadingOverlay = null;
    }
  }

  // SSR 首屏的锚点随 ctx 注入（首屏不经过 AJAX 渲染函数）
  if (ctx.region_refs && typeof ctx.region_refs === "object") {
    Object.assign(state.regionRefs, ctx.region_refs);
  }

  R.state = state;
  R.log = log;
  R.bool = bool;
  R.setButtonLoading = setButtonLoading;
  R.showPageLoading = showPageLoading;
  R.hidePageLoading = hidePageLoading;

  // 初始化信息（一次性 dump 上下文）
  log("review.html loaded", {
    job_id: state.jobId,
    filename: ctx.filename,
    status: ctx.status,
    page: state.currentPage,
    total_pages: state.totalPages,
    findings_count: ctx.findings_count,
    severity_counts: ctx.severity_counts,
    has_measurements: ctx.has_measurements,
    matrix_shape: ctx.matrix_shape,
    page_parse_error: ctx.page_parse_error,
    page_confidence: ctx.page_confidence,
  });
})(typeof window !== "undefined" ? window : globalThis);
