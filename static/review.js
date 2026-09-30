/* ============================================================
   Review 页 — 入口编排与数据层（R63 P1-A 拆分后瘦身为单一职责）

   原文件 2037 行混杂 DOM 初始化/PDF 缩放/SSE 订阅/findings 渲染等
   全部职责，现已按模块拆分（加载顺序见 templates/review.html）：
     review-state.js        共享状态与工具（最先加载）
     review-locate.js       OCR 面板文本定位 + htmlToText
     review-pageview.js     PDF 页图/缩放/页码导航/区域锚
     review-pageinfo.js     页面级 UI（置信度/横幅/参数矩阵/豁免区）
     review-findings.js     findings 渲染与裁决
     review-suppressions.js 抑制留痕面板
     review-progress.js     SSE 实时进度（基于共享件 sse.js）
   本文件只保留：页面编排（DOMContentLoaded）、翻页数据层
   （loadPageData / refreshCurrentPageFindings）、上下文操作
   （取消/重试）、复核反馈统计、全局导出。
   ============================================================ */
(function () {
  "use strict";

  const R = window.PbcReview;
  const log = R.log;

  // 终态集合：非终态才订阅 SSE
  const TERMINAL_STATUSES = [
    "review",
    "partial_review",
    "error",
    "cancelled",
    "archived",
  ];

  // 本页 findings 的请求上限（单一真值：两处 fetch 共用，避免只改一处漂移）。
  // 后端 `order=confidence` 分支先把该页全部 findings（≤2000 行）取回、按置信度
  // 排序，再 `[offset:offset+limit]` 切片；**前端从不传 offset** ⇒ limit 就是
  // 本页可见条数的硬上限。默认 50 会让"单页 >50 条"的后 50+ 条在 UI 里**完全
  // 不可达**（且已裁决的 finding 只是变淡、不会移出列表，"处理后刷新"也救不回
  // 来）—— GMP 漏检。取后端上限 200（`api/review.py` 内 `min(limit, 200)`）。
  const FINDINGS_PAGE_LIMIT = 200;

  // ============================================================
  // 数据层：AJAX 翻页 / 静默刷新
  // ============================================================

  // AJAX 加载页面数据（findings + OCR + measurements + banners）
  async function loadPageData(targetPage) {
    const state = R.state;
    const jobId = state.jobId;
    const token = ++state.pageLoadToken;
    log("loadPageData", { target: targetPage });
    R.showPageLoading();
    // 翻页期间禁用所有翻页按钮，防止重复点击
    document
      .querySelectorAll('[onclick^="goPage"], [onclick^="navPage"]')
      .forEach((b) => (b.disabled = true));
    try {
      const r = await fetch(`/api/jobs/${jobId}/pages/${targetPage}`);
      if (!r.ok) throw new Error("HTTP " + r.status);
      const pageData = await r.json();
      if (token !== state.pageLoadToken) return; // 已翻到新页，丢弃过期响应

      // 加载该页的 findings（按置信度排序：低置信度排前便于人工优先复核）
      // limit 必须显式传：不传则后端默认 50，单页 >50 条时其余条目在 UI 里
      // 不可达（详见 FINDINGS_PAGE_LIMIT 处注释）。
      const fr = await fetch(
        `/api/jobs/${jobId}/findings?page=${targetPage}&order=confidence&limit=${FINDINGS_PAGE_LIMIT}`,
      );
      if (!fr.ok) throw new Error("HTTP " + fr.status);
      const findingsData = await fr.json();

      // 加载该页的 measurements 矩阵
      const mr = await fetch(
        `/api/jobs/${jobId}/pages/${targetPage}/measurements`,
      );
      const measurementsData = mr.ok ? await mr.json() : { measurements: [] };
      if (token !== state.pageLoadToken) return; // 再校验一次（measurements 慢响应）

      // 更新 URL（不刷新页面）
      history.pushState(
        { page: targetPage },
        "",
        `/jobs/${jobId}/review?page=${targetPage}`,
      );

      // 更新页码导航 + PDF + 翻页按钮
      // 对抗审查：先落全局 currentPage 再刷 UI —— updatePdfDisplay 内的
      // syncNavButtons 读取全局值，后置赋值会让中间态按旧页码计算
      // （从末页回跳时 next 被错误禁用）。
      state.currentPage = targetPage;
      R.pageview.updatePageNavActive(targetPage);
      R.pageview.updatePdfDisplay(targetPage);

      // 更新 OCR 文本 — htmlToText 保留表格结构（行/列分隔），
      // 纯字符串处理 + textContent，无 XSS 面
      // P1 修复（cr-19）：无条件更新 — 空页 raw_html 为 ""（Paddle 空页）时
      // 原条件 `if (ocrEl && pageData.raw_html)` 跳过赋值，OCR 面板残留
      // 上一页文本，GMP 复核会误读；SSR 初次加载路径显示"无 OCR 数据"，
      // 两条路径行为需一致。
      const ocrEl = document.getElementById("ocr-text");
      if (ocrEl) {
        ocrEl.textContent = pageData.raw_html
          ? R.locate.htmlToText(pageData.raw_html)
          : "此页无 OCR 内容（空白页或扫描质量过低），未执行分析，请以 PDF 原图为准";
      }

      // 更新页面级 UI 元素：置信度 / parse-error / critical banner / measurements。
      // ⚠️ 顺序不可颠倒：本调用是 `state.currentPageFlags` 的**唯一写点**，
      // 而紧随其后的 renderFindings 在"清单为空"时经 emptyFindingsNote()
      // **读**它来区分"分析失败 / 空页 / 确实无问题"。若先渲染后写标记，
      // 翻页时会拿**上一页**的标记渲染本页空清单 —— 解析失败页被显示成
      // "本页无问题"（GMP 假阴性，正是 #136 要消灭的形态）。
      R.pageinfo.updatePageLevelUI(
        pageData,
        findingsData.findings || [],
        measurementsData,
      );

      // 更新 findings 列表（重新渲染）— P2-3: has_more 提示（后端默认
      // limit=50，超出部分静默截断会让复核者误以为全部问题就这些）。
      // total 一并传入：截断提示要报出"共 N 条 / 还有 M 条未显示"的真实数字，
      // 只给 hasMore 布尔值时文案只能含糊其辞。
      R.findings.renderFindings(
        findingsData.findings || [],
        findingsData.has_more,
        findingsData.total,
      );

      // P0-2：抑制台账随页切换（与 findings 同源同页，避免"问题清单已换页、
      // 抑制清单还是上一页"的错位误导）
      await R.suppressions.loadSuppressions(targetPage);

      // currentPage 已在 UI 刷新前更新（见上）
      log("loadPageData — success", {
        page: targetPage,
        findings: findingsData.count,
      });
    } catch (err) {
      log.err("loadPageData failed", err);
      // 降级：整页刷新
      window.location.href = `/jobs/${state.jobId}/review?page=${targetPage}`;
    } finally {
      R.hidePageLoading();
      // 恢复翻页按钮状态（仅当前代际，避免旧请求恢复已禁用状态）
      if (token === state.pageLoadToken) {
        R.pageview.updatePdfDisplay(state.currentPage);
      }
    }
  }

  // 流式输出：静默刷新当前页 findings（不显示 loading overlay）
  // 在 SSE 收到 pages_analyzed 变化时调用，让用户在 Stage 2 进行中
  // 就能看到已分析页的 findings 实时更新。
  async function refreshCurrentPageFindings() {
    const state = R.state;
    const jobId = state.jobId;
    const token = state.pageLoadToken;
    const page = state.currentPage;
    try {
      const [pageRes, findingsRes] = await Promise.all([
        fetch(`/api/jobs/${jobId}/pages/${page}`),
        fetch(
          `/api/jobs/${jobId}/findings?page=${page}&order=confidence&limit=${FINDINGS_PAGE_LIMIT}`,
        ),
      ]);
      if (!pageRes.ok || !findingsRes.ok) return;
      const pageData = await pageRes.json();
      const findingsData = await findingsRes.json();
      // 代际守卫：期间用户已翻页则丢弃（旧页数据渲染到新页会误导复核）
      if (token !== state.pageLoadToken || page !== state.currentPage) return;
      const findings = findingsData.findings || [];

      // 先取 measurements 再渲染：updatePageLevelUI 必须在 renderFindings
      // **之前**（它是 currentPageFlags 的唯一写点，空清单文案读它）——
      // 与 loadPageData 同一条不变式，详见该函数内的顺序注释。
      const mr = await fetch(
        `/api/jobs/${jobId}/pages/${page}/measurements`,
      );
      const measurementsData = mr.ok ? await mr.json() : { measurements: [] };
      if (token !== state.pageLoadToken || page !== state.currentPage) return;
      R.pageinfo.updatePageLevelUI(pageData, findings, measurementsData);
      // 重新渲染 findings 列表（total 用于截断提示的真实数字）
      R.findings.renderFindings(findings, findingsData.has_more, findingsData.total);
      log("refreshCurrentPageFindings — updated", {
        page,
        findings: findings.length,
      });
    } catch (err) {
      log.warn("refreshCurrentPageFindings failed", err);
    }
  }

  // ============================================================
  // 翻页控制
  // ============================================================

  function goPage(p) {
    const { currentPage, totalPages } = R.state;
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

  // 箭头翻页：onclick 只携带固定 delta（-1/+1），目标页永远从 currentPage
  // 实时计算 — 避免服务端渲染的 {{ page ± 1 }} 死值在 AJAX 翻页后失效/错跳
  function navPage(delta) {
    goPage(R.state.currentPage + delta);
  }

  // prev/next 箭头按钮 disabled 状态：与 currentPage/totalPages 实时同步
  function syncNavButtons() {
    const { currentPage, totalPages } = R.state;
    const prev = document.getElementById("btn-prev-page");
    const next = document.getElementById("btn-next-page");
    if (prev) prev.disabled = currentPage <= 1;
    if (next) next.disabled = currentPage >= totalPages;
  }

  // === Toast + 确认弹窗（共享实现 confirm-dialog.js）===
  // P1-9: 薄别名（页面 API 名不变，调用点零改动）。`window.PBC.*` 的**可用性**
  // 由 `pbc-fallback.js` 统一兜底（只补缺失方法，与加载顺序无关，见其头部注释），
  // 因此这里不必再判存在 —— 降级语义只写一处，避免多份副本漂移。
  function showToast(msg, type) {
    return window.PBC.showToast(msg, type);
  }
  function confirmDialog(opts) {
    return window.PBC.confirmDialog(opts);
  }

  // ============================================================
  // 上下文操作: 取消 / 重试
  // ============================================================

  async function cancelJob(e) {
    const ok = await confirmDialog({
      title: "确定取消此任务？",
      message: "处理中的数据会保留，可稍后重试。",
      confirmText: "确认取消",
      cancelText: "保留",
      danger: true,
    });
    if (!ok) return;
    log("cancelJob");
    const btn = e && e.currentTarget ? e.currentTarget : null;
    R.setButtonLoading(btn, true);
    try {
      const r = await fetch(`/api/jobs/${R.state.jobId}/cancel`, { method: "POST" });
      const data = await r.json();
      if (data.ok) {
        log("cancelJob — success", data);
        setTimeout(() => location.reload(), 800);
      } else {
        throw new Error(data.message || "取消失败");
      }
    } catch (err) {
      log.err("cancelJob failed", err);
      R.setButtonLoading(btn, false, "取消任务");
      showToast("取消失败: " + err.message, "err");
    }
  }

  async function retryJob(e) {
    // P1-6: review 状态 = 全量重新分析（清空结果），其余状态 = 断点续跑
    const fullRetry = e && e.currentTarget && e.currentTarget.dataset.fullRetry === "1";
    const ok = await confirmDialog({
      title: fullRetry ? "确定重新分析此任务？" : "确定重试此任务？",
      message: fullRetry
        ? "将清空现有分析结果并完整重新分析（OCR 结果复用，不重复消耗）。"
        : "将从中断处继续处理。",
      confirmText: fullRetry ? "确认重新分析" : "确认重试",
      cancelText: "取消",
    });
    if (!ok) return;
    log("retryJob");
    const btn = e && e.currentTarget ? e.currentTarget : null;
    R.setButtonLoading(btn, true);
    try {
      const r = await fetch(`/api/jobs/${R.state.jobId}/retry`, { method: "POST" });
      const data = await r.json();
      if (data.ok) {
        log("retryJob — success", data);
        setTimeout(() => location.reload(), 800);
      } else {
        throw new Error(data.detail || data.message || "重试失败");
      }
    } catch (err) {
      log.err("retryJob failed", err);
      R.setButtonLoading(btn, false, "重试");
      showToast("重试失败: " + err.message, "err");
    }
  }

  // ============================================================
  // 复核反馈统计（round-23 C）
  // ============================================================

  // 确认/驳回率、高频驳回类型、按来源驳回率 — DOMContentLoaded 内调用；
  // 载入失败静默（统计是增强项，不阻断主链路）。
  async function loadReviewStats(jid) {
    const brief = document.getElementById("review-stats-brief");
    const body = document.getElementById("review-stats-body");
    if (!brief && !body) return;
    try {
      const r = await fetch(`/api/jobs/${encodeURIComponent(jid)}/review-stats`);
      if (!r.ok) return;
      const s = await r.json();
      if (brief) {
        brief.textContent = s.adjudicated
          ? `已裁决 ${s.adjudicated}/${s.total} · 确认率 ${(s.confirm_rate * 100).toFixed(0)}% · 驳回率 ${(s.reject_rate * 100).toFixed(0)}%`
          : `待裁决 ${s.pending} 条`;
      }
      if (!body) return;
      const parts = [];
      parts.push(
        `已裁决 ${s.adjudicated}/${s.total} 条（确认 ${s.by_status.confirmed} · 驳回 ${s.by_status.rejected} · 修正 ${s.by_status.corrected} · 待复核 ${s.pending}）`,
      );
      if (s.adjudicated) {
        parts.push(
          `确认率 ${(s.confirm_rate * 100).toFixed(1)}% · 驳回率 ${(s.reject_rate * 100).toFixed(1)}%`,
        );
      }
      if (Array.isArray(s.top_rejected_types) && s.top_rejected_types.length) {
        const tops = s.top_rejected_types
          .map((t) => `${t.type_zh}（${t.count} 条，${(t.share * 100).toFixed(0)}%）`)
          .join("、");
        parts.push(`高频驳回类型: ${tops}`);
      }
      if (Array.isArray(s.by_source) && s.by_source.length) {
        const srcs = s.by_source
          .map((x) => {
            const rate = (x.reject_rate * 100).toFixed(0);
            const flag = x.reject_rate > 0.5 && x.total >= 4 ? "!" : "";
            return `${x.source}: ${x.rejected}/${x.total}（${rate}%）${flag}`;
          })
          .join(" · ");
        parts.push(`按来源驳回率: ${srcs}`);
      }
      body.textContent = parts.join("\n");
      body.style.whiteSpace = "pre-line";
    } catch (err) {
      log.warn("review-stats fetch failed", err);
    }
  }

  // ============================================================
  // 挂载跨模块入口 + DOMContentLoaded 编排
  // ============================================================

  R.loadPageData = loadPageData;
  R.refreshCurrentPageFindings = refreshCurrentPageFindings;
  R.goPage = goPage;
  R.syncNavButtons = syncNavButtons;
  // progress 模块的 safeAutoReload（fallback 轮询/终态刷新共用）
  R.safeAutoReload = R.progress.safeAutoReload;

  // 暴露到全局（onclick 处理器需要）
  window.goPage = goPage;
  window.navPage = navPage;
  window.cancelJob = cancelJob;
  window.retryJob = retryJob;

  // DOM 就绪后探测 E2E-required 元素，方便快速排查模板渲染问题
  document.addEventListener("DOMContentLoaded", () => {
    const state = R.state;
    const ctx = state.ctx;
    const jobId = state.jobId;

    // 跨页总览的按钮监听（P1）。防御式：review-findings.js 缺席时不该连带
    // 废掉整个 DOMContentLoaded（后面的 PDF 初始化 / SSE 订阅都要跑）。
    if (R.findings && R.findings.initOverview) R.findings.initOverview();

    // #135：首屏也应用一次页面级 UI（此前只有 AJAX/SSE 路径会调
    // updatePageLevelUI，DOMContentLoaded 不触发）⇒ 直接打开或刷新终态
    // 任务的第一页时，parse-error 横幅只有模板里的**通用文案**，
    // 复核者看不到"401 凭据失效"这类**全局性**失败原因 ——
    // 与 #127（后端原因不透传）是同一缺陷的界面侧。
    // 数据来自 SSR 注入的 ctx（structured_json 已在服务端解析）。
    R.pageinfo.applyInitialPageLevelUI();

    // 初始渲染当前页 PNG（替代 iframe 原生 PDF viewer —
    // 无浏览器打印/下载/更多操作按钮，缩放 fit-width 可控）
    const pdfImg = document.getElementById("pdf-page-img");
    const pdfLoading = document.getElementById("pdf-loading");
    if (pdfImg && pdfLoading) {
      // onload/onerror 只注册一次 — 反复赋值会互相覆盖（翻页时第二次
      // updatePdfDisplay 命中缓存分支曾把 onload 置 null，图片加载完成后
      // 无回调 → "正在渲染第 N 页" 永久显示）。加载完成/失败统一隐藏遮挡。
      pdfImg.onload = () => {
        pdfLoading.classList.add("is-loaded");
        // P0-3：区域框以图片实际像素尺寸定位，必须在图片加载完成后重算
        // （加载前 offsetWidth/offsetHeight 还是旧页/占位尺寸 → 框偏移）
        R.pageview.positionRegionOverlay();
      };
      pdfImg.onerror = () => {
        pdfLoading.classList.add("is-loaded");
        log.err("PDF page render failed");
      };
      R.pageview.updatePdfDisplay(state.currentPage);
      // 兜底：6s 后强制隐藏（渲染失败/极慢时不永久遮挡）
      setTimeout(() => pdfLoading.classList.add("is-loaded"), 6000);
      // 窗口尺寸变化 → 图片 CT 尺寸变化 → 区域框需重算
      window.addEventListener("resize", R.pageview.positionRegionOverlay);
    }

    // === SSE 实时进度订阅 ===
    // 非终态时订阅 /api/jobs/{id}/stream，每 3s 收到进度更新
    // 终态时服务端推送 done 事件并关闭流
    if (jobId && !TERMINAL_STATUSES.includes(ctx.status)) {
      R.progress.subscribe(jobId);
    }

    // round-23 C：复核反馈统计面板（确认/驳回率、高频驳回类型、按来源
    // 驳回率）— 裁决操作后页面 reload 自动刷新；载入失败静默（统计是
    // 增强项，不阻断复核主链路）。
    if (jobId) {
      loadReviewStats(jobId);
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

    // 验证 critical-pulse 静态红晕规则是否被浏览器解析（P1-8: 3s 呼吸已移除）
    try {
      const sheets = [...document.styleSheets];
      let found = false;
      for (const s of sheets) {
        try {
          for (const rule of s.cssRules || []) {
            if (
              rule.cssText &&
              rule.cssText.includes(".critical-pulse") &&
              rule.cssText.includes("box-shadow")
            ) {
              found = true;
              log(
                "CSS rule matched: .critical-pulse static",
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
          "critical-pulse 静态规则未在可访问样式表中找到（可能是跨域 CSS）",
        );
    } catch (e) {
      log.warn("styleSheet 探测失败", e);
    }
  });

  // 初始化：OCR 文本 raw → htmlToText 可读化（data-raw 为服务端注入原文）
  window.addEventListener("DOMContentLoaded", () => {
    const el = document.getElementById("ocr-text");
    if (el) {
      const raw = el.getAttribute("data-raw") || "";
      el.textContent = R.locate.htmlToText(raw) || "无 OCR 数据";
    }
    // P0-2 抑制台账：首屏也走同一渲染函数（SSR 只放空容器）
    R.suppressions.loadSuppressions(R.state.currentPage);
  });

  // finding 定位：findings-list 事件委托（AJAX 重渲染后监听仍生效）。
  // 点击卡片（非操作按钮）→ OCR 面板高亮定位对应原文。
  // ⚠️ R63 修复：拆分前这里调用 locateFinding(card)，但同名函数声明
  // 提升使区域锚定版遮蔽了文本定位版 → 此路径一直静默失效（详见
  // review-locate.js 头注）。现指向 locateInOcrPanel。
  document.addEventListener("DOMContentLoaded", () => {
    const listEl = document.getElementById("findings-list");
    if (listEl) {
      listEl.addEventListener("click", (e) => {
        if (e.target.closest("button")) return; // 确认/拒绝/修正按钮不触发
        const card = e.target.closest(".finding-card");
        if (card) R.locate.locateInOcrPanel(card);
      });
    }
  });

  // 键盘快捷键: ← → 翻页
  // ⚠️ 必须排除"有弹窗打开"的情形：弹窗的 onKey 只处理 Esc/Enter/Tab，方向键
  // 会继续冒泡到这里 —— 于是在"取消任务"确认框开着时按 ← → 会**翻动背后的
  // 页面**：弹窗还在，上下文却已经换了（正要确认的那条 finding 已不在屏幕上，
  // 翻页还会触发 loadPageData 重渲染清单）。
  // 两个监听器都挂在 document 上、且本监听器注册更早，事件按注册序触发 ⇒
  // 弹窗侧 stopPropagation 救不了，只能在这里主动查询。
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
    // 防御式取值：本处理器在**每次按键**上运行，一旦抛错会连带废掉全部键盘
    // 导航（比"方向键穿透"更糟）。故宁可 fail-open，不做无保护解引用。
    if (window.PBC && window.PBC.isDialogOpen && window.PBC.isDialogOpen()) {
      return;
    }
    if (e.key === "ArrowLeft") goPage(R.state.currentPage - 1);
    else if (e.key === "ArrowRight") goPage(R.state.currentPage + 1);
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
