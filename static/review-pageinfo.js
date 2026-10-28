/* ============================================================
   Review 页 — 页面级 UI：置信度徽章 / 各类横幅 / 参数矩阵 /
   OCR 完整性豁免区（拆分自 review.js，R63 P1-A）

   翻页时这些元素若不同步更新，用户看到的就是上一页的置信度、
   critical 计数和参数矩阵，对 GMP 复核构成误导（#135/#136 的由来）。
   映射（置信度等）走共享件 PbcFindingsMap（R63 P2 收敛）。
   ============================================================ */
(function (global) {
  "use strict";

  const R = global.PbcReview;
  const log = R.log;
  const FM = global.PbcFindingsMap;

  // 首屏页面级 UI（#135）：只喂模板已经知道的那几项（parse-error 横幅 +
  // 置信度），不伪造结构化数据。`findings` / `measurements` 传首屏已有的
  // SSR 值（模板已渲染好，这里只是把"原因文案"补上）。
  //
  // 为什么不干脆在模板里直接渲染原因：模板文案与 JS 文案必须有**唯一副本**，
  // 否则两处必然漂移（updatePageLevelUI 里已缓存 dataset.fallback 正是为此）。
  // 所以这里复用同一个 updatePageLevelUI，让首屏与翻页走**同一条**代码路径。
  function applyInitialPageLevelUI() {
    const ctx = R.state.ctx;
    if (ctx.page_parse_error || ctx.page_confidence || ctx.page_ocr_empty) {
      updatePageLevelUI(
        {
          structured: {
            _parse_error: R.bool(ctx.page_parse_error),
            _error: ctx.page_parse_error_reason || "",
            overall_confidence: ctx.page_confidence || "",
          },
        },
        [],
        {},
      );
    }
    // 首屏 findings 是 SSR 渲染的：若为空，按同一判据补上正确文案
    // （#136 —— 模板已按 page_parse_error 分支渲染，此处只在"清单为空"
    // 时兜底，避免依赖模板分支是否被正确渲染）。
    const list = document.getElementById("findings-list");
    if (list && !list.querySelector('[id^="finding-"]')) {
      const note = document.getElementById("findings-empty-note");
      if (note) note.outerHTML = R.findings.emptyFindingsNote();
    }
  }

  // 翻页时更新页面级 UI：置信度徽章 / parse-error 横幅 / critical 横幅 / 参数矩阵
  // 之前 AJAX 翻页只更新 OCR + findings，导致用户看到的是上一页的置信度、
  function updatePageLevelUI(pageData, findings, measurementsData) {
    const state = R.state;
    const structured = pageData.structured || {};
    const pageConfidence = structured.overall_confidence || "";
    const pageParseError = R.bool(structured._parse_error);

    // #136：刷新"当前页标记" —— 空 findings 文案（emptyFindingsNote）依赖它。
    // 必须在任何提前 return 之前写入，保证翻页后标记与页面同步。
    state.currentPageFlags = {
      parseError: pageParseError,
      ocrEmpty: R.bool(structured._ocr_empty),
    };

    // 1. 置信度徽章
    const confEl = document.getElementById("page-confidence-badge");
    if (confEl) {
      if (pageConfidence && !pageParseError) {
        confEl.textContent = `置信度 ${FM.confidenceZh(pageConfidence)}`;
        confEl.classList.remove("hidden");
      } else {
        confEl.classList.add("hidden");
      }
    }

    // 2. parse-error 横幅
    // #127：把**实际失败原因**写进横幅。此前只有一句通用文案 —— 复核者知道
    // "这页没解析出来"，却不知道是 PDF 本身损坏、网关超时，还是模型凭据失效。
    // 后者是**全局性**故障（每页都一样），必须能一眼认出：否则整份 0 条 finding
    // 会被当成"记录无异常"（GMP 假阴性）。
    const parseBanner = document.getElementById("parse-error-banner");
    if (parseBanner) {
      parseBanner.classList.toggle("hidden", !pageParseError);
      const parseText = document.getElementById("parse-error-text");
      if (parseText) {
        // 首帧模板文案即"通用兜底"的**唯一副本**：缓存后复用，
        // 不在 JS 里再抄一份（两处文案必然漂移）。
        if (parseText.dataset.fallback === undefined)
          parseText.dataset.fallback = parseText.textContent.trim();
        const reason = String(structured._error || "").trim();
        parseText.textContent = reason
          ? `此页 LLM 解析失败：${reason}`
          : parseText.dataset.fallback;
      }
    }

    // 2b. 幻觉防护横幅 — LLM 提取数值未在 OCR 原文找到（疑似臆造）
    const groundingWarn = structured._grounding_warn || [];
    const groundingBanner = document.getElementById("grounding-warn-banner");
    if (groundingBanner) {
      groundingBanner.classList.toggle("hidden", groundingWarn.length === 0);
      const textEl = groundingBanner.querySelector(".grounding-warn-text");
      if (textEl && groundingWarn.length > 0) {
        textEl.textContent = groundingWarn.join("；");
      }
    }

    // 2c. 截断透出横幅 — HTML 超上限被截，分析基于不完整输入
    const truncatedBanner = document.getElementById("ocr-truncated-banner");
    if (truncatedBanner) {
      truncatedBanner.classList.toggle("hidden", !R.bool(structured._ocr_truncated));
    }

    // 2c-b. LLM 完整性横幅（对抗审查 P1）— 输出截断已恢复 / schema 校验
    // 未通过，此前标记无消费终端，复核者看不到"数据可能静默缺失"
    const llmIntegrityBanner = document.getElementById("llm-integrity-banner");
    if (llmIntegrityBanner) {
      const schemaWarn = structured._schema_warn || [];
      const llmTruncated = R.bool(structured._truncated_warn);
      const showLlmIntegrity = llmTruncated || schemaWarn.length > 0;
      llmIntegrityBanner.classList.toggle("hidden", !showLlmIntegrity);
      const mainText = llmIntegrityBanner.querySelector(".llm-integrity-text") ||
        llmIntegrityBanner.querySelector("p.text-xs.font-medium");
      if (mainText) {
        let msg = "此页 LLM 分析结果可能不完整";
        if (llmTruncated) msg += "（输出过长被截断后自动恢复，尾部数据可能丢失）";
        if (llmTruncated && schemaWarn.length > 0) msg += "；";
        if (schemaWarn.length > 0) msg += "（结构校验未完全通过）";
        msg += "，请以 PDF 原图核对此页全部内容";
        mainText.textContent = msg;
      }
      const schemaText = llmIntegrityBanner.querySelector(".llm-schema-warn-text");
      if (schemaText) {
        if (schemaWarn.length > 0) {
          schemaText.textContent = schemaWarn.join("；");
          schemaText.classList.remove("hidden");
        } else {
          schemaText.textContent = "";
          schemaText.classList.add("hidden");
        }
      }
    }

    // 2d. OCR 状态横幅（空页/稀疏/不完整警告）— AJAX 翻页时同步更新，
    // 否则上一页的横幅残留到当前页，对 GMP 复核构成误导
    const emptyBanner = document.getElementById("ocr-empty-banner");
    if (emptyBanner) {
      emptyBanner.classList.toggle("hidden", !R.bool(structured._ocr_empty));
    }
    const sparseBanner = document.getElementById("ocr-sparse-banner");
    if (sparseBanner) {
      sparseBanner.classList.toggle("hidden", !R.bool(structured._ocr_sparse));
    }
    const warningBanner = document.getElementById("ocr-warning-banner");
    if (warningBanner) {
      warningBanner.classList.toggle("hidden", !structured._ocr_warning);
      if (structured._ocr_warning) {
        const warnText = warningBanner.querySelector("span.text-xs");
        if (warnText) {
          warnText.textContent = `此页 OCR 不完整：${structured._ocr_warning} — 分析已降级，请以 PDF 原图核对`;
        }
      }
    }
    // OCR 原始完整性证据独立于 LLM 返回。LLM 超时/JSON 失败时仍须让
    // 复核者看到“此页不可信”，不能因异步翻页而沿用上一页的横幅。
    const integrityBanner = document.getElementById("ocr-integrity-banner");
    if (integrityBanner) {
      const diag = pageData.ocr_diagnostics || {};
      const showIntegrity = diag.integrity === "incomplete" && !structured._ocr_warning;
      integrityBanner.classList.toggle("hidden", !showIntegrity);
      if (showIntegrity) {
        const integrityText = document.getElementById("ocr-integrity-text");
        if (integrityText) {
          integrityText.textContent = `此页 OCR 完整性校验未通过：${(diag.reasons || []).join("；")}。请以 PDF 原图为准。`;
        }
        // 门禁 1：有效 DPI / 页面盒 / 旋转 / 长宽比 / 文本量诊断详情
        const detailEl = document.getElementById("ocr-detail-text");
        if (detailEl) {
          const parts = [];
          if (diag.effective_dpi != null) {
            parts.push(`有效 DPI=${diag.effective_dpi}${diag.low_dpi ? "（低）" : ""}`);
          }
          if (Array.isArray(diag.media_box_pt) && diag.media_box_pt.length === 2) {
            parts.push(`页面盒=${Math.round(diag.media_box_pt[0])}×${Math.round(diag.media_box_pt[1])}pt`);
          }
          if (diag.rotation) {
            parts.push(`旋转=${diag.rotation}°`);
          }
          // round-23 A：自愈信息（切片重跑 / 横置页旋转重渲染）— 复核者
          // 须知道"此页文本是恢复产物"而非原始识别，必要时以 PDF 原图核对。
          if (diag.self_healed) {
            parts.push(
              diag.rotation_deg != null
                ? `已自愈（横置页按 ${diag.rotation_deg}° 重渲染后识别）`
                : "已自愈（切片重跑识别）",
            );
          }
          // round-23 A2：旋转探测未果 — 提示复核者系统已尽力（90/270/180°
          // 均试过），此页需人工核对原图，不要再怀疑横置可能性。
          if (diag.rotation_probed) {
            parts.push("已尝试 90/270/180° 旋转恢复未果，请人工核对原图");
          }
          if (diag.aspect_ratio != null) {
            parts.push(`长宽比=${diag.aspect_ratio}`);
          }
          if (diag.text_chars != null) {
            parts.push(`文本=${diag.text_chars} 字符`);
          }
          detailEl.textContent = parts.join("；");
        }
      }
      renderOcrExemptionZone(diag, pageData.page);
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
            timeTd.className = "px-3 py-1.5 font-mono tabular-nums text-foreground";
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
          matrixShape.className = (matrixShape.className || "") + " tabular-nums";
        }
        matrixSection.classList.remove("hidden");
      } else {
        matrixSection.classList.add("hidden");
      }
    }
  }

  // 门禁 2（docs/OCR_GOLDEN_CORPUS.md）：OCR 完整性不达标的页面
  // 记录"人工确认豁免 + 原因"。
  // 已豁免 → 绿徽章 + 原因 + 时间 + 撤销；未豁免 → "已人工核对原图，
  // 确认豁免"按钮。操作走 POST /api/jobs/{job_id}/pages/{page}/exemption。
  function renderOcrExemptionZone(diag, pageNum) {
    const zone = document.getElementById("ocr-exemption-zone");
    const jobId = R.state.jobId;
    if (!zone) {
      return;
    }
    // 与 renderFindings 同款转义：reason 是人工输入，可能含 HTML。
    const esc = FM.esc;
    zone.replaceChildren();
    const exempt = diag && diag.exemption;
    if (exempt) {
      const badge = document.createElement("div");
      badge.className =
        "inline-flex items-center gap-2 rounded border-l-2 border-success bg-card px-2 py-1.5 text-xs";
      badge.innerHTML =
        '<span class="w-1.5 h-1.5 rounded-full bg-success shrink-0"></span>' +
        `<span>已确认豁免（人工已核对原图）：${esc(exempt.reason || "")}</span>` +
        `<span class="text-muted-foreground tabular-nums">${esc(exempt.created_at || "")}</span>` +
        '<button type="button" class="text-muted-foreground underline underline-offset-2 hover:text-foreground" data-exempt-revoke="1">撤销</button>';
      zone.appendChild(badge);
      const revokeBtn = badge.querySelector("[data-exempt-revoke]");
      if (revokeBtn) {
        revokeBtn.addEventListener("click", async (e) => {
          e.stopPropagation();
          const ok = await window.PBC.confirmDialog({
            title: "撤销该页 OCR 豁免？",
            message: "撤销后该页恢复为“完整性未通过”状态，请确认。",
            confirmText: "撤销豁免",
            cancelText: "取消",
          });
          if (!ok) {
            return;
          }
          try {
            const r = await fetch(`/api/jobs/${jobId}/pages/${pageNum}/exemption`, {
              method: "POST",
              headers: { "Content-Type": "application/x-www-form-urlencoded" },
              body: "revoke=1",
            });
            if (!r.ok) {
              const err = await r.json().catch(() => ({}));
              throw new Error(err.detail || `HTTP ${r.status}`);
            }
            window.PBC.showToast("已撤销豁免", "ok");
            R.loadPageData(pageNum);
          } catch (err) {
            window.PBC.showToast("撤销豁免失败: " + err.message, "err");
          }
        });
      }
    } else if (diag && diag.integrity === "incomplete") {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className =
        "rounded-md border border-border bg-card px-3 py-1 text-xs text-foreground hover:bg-muted";
      btn.textContent = "已人工核对原图，确认豁免";
      btn.addEventListener("click", async () => {
        const reason = await window.PBC.promptDialog({
          title: "确认本页 OCR 完整性豁免",
          message:
            "你已对照 PDF 原图核对，确认本页 OCR 不完整但内容可接受。请填写豁免原因（GMP 审计将记录）：",
          confirmText: "记录豁免",
          cancelText: "取消",
        });
        if (!reason) {
          return;
        }
        try {
          const r = await fetch(`/api/jobs/${jobId}/pages/${pageNum}/exemption`, {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: "reason=" + encodeURIComponent(reason),
          });
          if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${r.status}`);
          }
          window.PBC.showToast("已记录豁免", "ok");
          R.loadPageData(pageNum);
        } catch (err) {
          window.PBC.showToast("记录豁免失败: " + err.message, "err");
        }
      });
      zone.appendChild(btn);
    }
  }

  const pageinfo = (global.PbcReview = global.PbcReview || {});
  pageinfo.pageinfo = {
    applyInitialPageLevelUI: applyInitialPageLevelUI,
    updatePageLevelUI: updatePageLevelUI,
    renderOcrExemptionZone: renderOcrExemptionZone,
  };
})(typeof window !== "undefined" ? window : globalThis);
