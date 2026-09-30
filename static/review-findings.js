/* ============================================================
   Review 页 — findings 列表渲染与裁决操作（拆分自 review.js，R63 P1-A）

   中文映射（severity/source/type/finding-status）走共享件
   static/findings-map.js（R63 P2 收敛：此前内建在本渲染函数里，
   与 SSR 模板各持一份，新增枚举漏改一处即首屏/翻页文案不同源）。

   Security: escape all attacker-controlled text before injecting as
   HTML. f.type / f.description / f.ocr_text come from LLM or rule
   output and could contain <script> tags otherwise（esc 走共享件）。
   ============================================================ */
(function (global) {
  "use strict";

  const R = global.PbcReview;
  const log = R.log;
  const FM = global.PbcFindingsMap;

  // 三色复核分级（M6/T6.4）计数条刷新 —— 红/蓝为本页口径，与下方清单一致；
  // 绿色（系统校验通过）是全批次口径，由 SSR 渲染，翻页不重算。
  function updateTierCounts(findings) {
    const tally = { rule: 0, llm: 0 };
    for (const f of findings || []) {
      const t = f && f.tier === "llm" ? "llm" : "rule"; // 未知/缺省 → 规则（保守同后端）
      tally[t] += 1;
    }
    const rEl = document.getElementById("tier-count-rule");
    const lEl = document.getElementById("tier-count-llm");
    if (rEl) rEl.textContent = String(tally.rule);
    if (lEl) lEl.textContent = String(tally.llm);
  }

  // 空 findings 的文案（#136）。**单一副本**：SSR 首屏（模板）与 AJAX 翻页
  // 走同一判据（分析失败 / 空页 / 确实无问题），否则两套标记必然漂移。
  // 依据的是**当前页**的结构化标记（`currentPageFlags`，由 updatePageLevelUI
  // 每次刷新时写入）—— 不能用 ctx（那是首屏注入的，翻页后已过期）。
  function emptyFindingsNote() {
    const flags = R.state.currentPageFlags || {};
    const base = "py-8 text-center text-[13px] text-muted-foreground";
    if (flags.parseError) {
      return (
        `<div class="${base}" id="findings-empty-note">本页未能分析（LLM 解析失败），` +
        `<span class="text-foreground">问题清单为空不代表本页无问题</span>，` +
        `请以 PDF 原图人工核对</div>`
      );
    }
    if (flags.ocrEmpty) {
      return (
        `<div class="${base}" id="findings-empty-note">` +
        `本页无 OCR 内容（空白页或扫描质量过低），未执行分析</div>`
      );
    }
    return `<div class="${base}" id="findings-empty-note">本页无问题</div>`;
  }

  // 渲染 findings 列表
  // total：后端返回的**当前过滤集**总数（本页问题总数）。截断提示要报真实
  // 缺口就离不开它，故为可选第三参（旧调用点缺省时降级为不报数字）。
  function renderFindings(findings, hasMore, total) {
    const list = document.getElementById("findings-list");
    if (!list) return;
    // 映射走共享件（R63 P2）：severity/source/type/status 的中文与
    // SSR 模板由机检锁定逐值一致（tests/unit/test_findings_map_js.py）。
    const severityZh = FM.SEVERITY_ZH;
    const sourceZh = FM.SOURCE_ZH;
    const typeZh = FM.TYPE_ZH;
    const statusZh = FM.FINDING_STATUS_ZH;
    const zhOrUnknown = FM.zhOrUnknown;
    const esc = FM.esc;

    if (findings.length === 0) {
      // #136：空 findings 的**原因**必须区分，三种情况视觉上不能相同：
      //   - 分析失败（_parse_error）→ "未能分析，清单为空不代表无问题"
      //   - 空页（_ocr_empty）      → "无 OCR 内容，未执行分析"
      //   - 其余                     → "本页无问题"（确实检查过）
      // 只按 `findings.length === 0` 判断会把失败页伪装成合规页（GMP 假阴性）。
      list.innerHTML = emptyFindingsNote();
      updateTierCounts(findings);
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
        // 三色分级（M6/T6.4）：tier 由后端 core.finding_quality 单一来源计算，
        // 前端只读不做映射 —— 避免 SSR/AJAX 两套颜色漂移。
        const tier = f.tier === "llm" ? "llm" : "rule";
        const statusTag =
          f.status !== "pending"
            ? `<span class="text-[11px] text-muted-foreground">· ${esc(zhOrUnknown(statusZh, f.status))}</span>`
            : "";
        const sourceTag =
          f.source && f.source !== "rule"
            ? `<span class="text-[11px] text-muted-foreground">· ${esc(zhOrUnknown(sourceZh, f.source))}</span>`
            : "";
        // #8 字段级置信度：低置信条目显式标记（LLM 生成 + 所在页带
        // 完整性警告 → 评分下降），提示复核员优先对照原图
        const lowConfTag =
          typeof f.confidence === "number" && f.confidence < 0.6
            ? `<span class="text-[11px] px-1 py-0.5 rounded bg-warning/10 text-warning" title="置信度 ${f.confidence}（来源可靠性/页面完整性加权）">低置信</span>`
            : "";
        const ocrSnippet = f.ocr_text
          ? `<p class="text-[11px] text-muted-foreground font-mono mt-1 truncate">OCR：${esc(f.ocr_text.slice(0, 100))}</p>`
          : "";
        // GMP 依据引用（v7）：法规知识库映射（gmp_basis.py），复核员可
        // 直接引用到复核记录；无映射（ocr_noise/user_rule）不显示
        const basisInfo = f.gmp_basis
          ? `<p class="text-[11px] text-muted-foreground mt-1 border-l-2 border-border pl-1.5">依据：${esc(f.gmp_basis)}</p>`
          : "";
        // 知识库条文引用（v8）：kb_refs 为 JSON 字符串，坏数据安全退化
        let kbRefsInfo = "";
        if (f.kb_refs) {
          let refs = [];
          try {
            const parsed = JSON.parse(f.kb_refs);
            if (Array.isArray(parsed)) refs = parsed.filter((x) => x && x.label);
          } catch {
            /* ignore malformed */
          }
          if (refs.length) {
            const rowsHtml = refs
              .map(
                (r) =>
                  `<p class="text-[11px] text-muted-foreground leading-relaxed">` +
                  `<span class="font-medium text-foreground">${esc(r.label)}</span>：${esc(r.excerpt || "")}</p>`,
              )
              .join("");
            kbRefsInfo =
              `<details class="mt-1 group"><summary class="text-[11px] text-muted-foreground ` +
              `cursor-pointer select-none hover:text-foreground">依据条文（${refs.length}）</summary>` +
              `<div class="mt-1 space-y-1 border-l-2 border-border pl-1.5">${rowsHtml}</div></details>`;
          }
        }
        // UX P1-2: AJAX 渲染补齐人工复核信息 — SSR 模板有 corrected_text /
        // reviewer_note（review.html:359-364），JS 渲染此前缺失：用户修正
        // 或备注后详情从视图中消失，复核记录审计不可见。
        const correctedInfo =
          f.status === "corrected" && f.corrected_text
            ? `<p class="text-[11px] text-foreground mt-1">修正：${esc(f.corrected_text)}</p>`
            : "";
        const noteInfo = f.reviewer_note
          ? `<p class="text-[11px] text-muted-foreground mt-1">备注：${esc(f.reviewer_note)}</p>`
          : "";
        // f.id is INTEGER from DB; coerce to Number to prevent string injection
        const fid = Number(f.id);
        // P0-3：区域级证据锚（后端读时推导；锚不上则无此入口）。
        // 内联 onclick 传不了对象，先把锚存进 regionRefs 再按 id 取。
        // 画的是 page_bbox（后端已按服务端上报的 angle 做旋转逆映射的
        // **页面空间**坐标）；bbox 是 OCR 空间原始框，仅留痕不回显。
        if (f.region_ref && Array.isArray(f.region_ref.page_bbox)) {
          R.state.regionRefs[fid] = f.region_ref;
        } else {
          delete R.state.regionRefs[fid];
        }
        const locateBtn = f.region_ref && Array.isArray(f.region_ref.page_bbox)
          ? `<button onclick="locateRegion(event, ${fid})" class="btn-press text-[11px] font-medium text-muted-foreground hover:text-foreground" title="在左侧页面上高亮该问题所在的 OCR 版面区域（区域级定位，非单元格级）">定位原图</button>`
          : "";
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
                <div id="finding-${fid}" class="finding-card tier-${tier} stagger-in hover-lift border-b border-border last:border-b-0 ${statusOpacity} py-2.5 px-1" data-tier="${tier}" data-ocr="${esc(f.ocr_text || "")}" style="--i: ${i}">
                    <div class="flex items-start gap-2">
                        <span class="w-1.5 h-1.5 rounded-full ${sevDot} mt-[7px] shrink-0"></span>
                        <div class="flex-1 min-w-0">
                            <div class="flex items-center gap-2 mb-0.5">
                                <span class="text-[13px] font-medium text-foreground">${esc(zhOrUnknown(typeZh, f.type))}</span>
                                ${statusTag}
                                ${sourceTag}
                                ${lowConfTag}
                                <span class="text-[11px] text-muted-foreground uppercase tracking-wider ml-auto">${esc(zhOrUnknown(severityZh, f.severity))}</span>
                            </div>
                            <p class="text-[13px] text-muted-foreground leading-relaxed">${esc(f.description)}</p>
                            ${ocrSnippet}
                            ${basisInfo}
                            ${kbRefsInfo}
                            ${correctedInfo}
                            ${noteInfo}
                            ${locateBtn
                              ? `<div class="mt-1.5 flex items-center gap-2">${locateBtn}</div>`
                              : ""}
                            ${actionBtns}
                        </div>
                    </div>
                </div>`;
      })
      .join("")
      // P2-3: has_more 时追加提示 — 后端 limit（复核页现固定传 200）之外
      // 的部分被截断；静默截断会让复核者误以为本页全部问题就是这些（GMP 漏检）。
      // 对抗审查：has_more 按当前过滤集（页/状态）统计，不再被全局总数误触发。
      //
      // ⚠️ 旧文案「请逐页翻页或处理后刷新」**两句都是无效指引**，已删除：
      //   · findings 是**页内**集合，翻页离开再回来仍是同一批前 N 条；
      //   · 已裁决的 finding 只在渲染时变淡（opacity-50），**不会移出列表**
      //     （见上方 statusOpacity），"处理后刷新"也换不出被截断的条目。
      // 真正含**全部** findings 的出口是报告导出（api/report.py 的查询无
      // LIMIT），故改指向它 —— 指路必须指向真能到的地方。
      .concat(
        hasMore
          ? (() => {
              const shown = findings.length;
              const missing =
                typeof total === "number" && total > shown ? total - shown : null;
              const detail =
                missing === null
                  ? `本页已显示 ${shown} 条，仍有多条未显示`
                  : `本页共 ${total} 条问题，按置信度仅显示前 ${shown} 条，尚有 ${missing} 条未显示`;
              return (
                `<div class="py-2 px-1 text-[11px] text-muted-foreground text-center">` +
                `${detail}（超出单页显示上限；全部问题可在报告导出中查看）</div>`
              );
            })()
          : "",
      );
    // 三色计数条随本页清单同步（AJAX 翻页后红/蓝数字必须跟上）
    updateTierCounts(findings);
  }

  function updateFinding(e, findingId, status) {
    const jobId = R.state.jobId;
    log("updateFinding() called", { findingId, status });
    const btn = e && e.currentTarget ? e.currentTarget : null;
    R.setButtonLoading(btn, true);
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
        R.setButtonLoading(btn, false, status === "confirmed" ? "确认" : "拒绝");
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
    const jobId = R.state.jobId;
    const btn = e && e.currentTarget ? e.currentTarget : null;
    R.setButtonLoading(btn, true);
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
        R.setButtonLoading(btn, false, "修正");
        window.PBC.showToast(
          "修正失败: " + err.message + "\n请查看控制台排查",
          "err",
        );
      });
  }

  // ============================================================
  // 跨页问题总览（对抗审查 P1）
  // ============================================================
  //
  // 为什么是**独立视图**而不是给 `#findings-list` 加"全部"模式：
  //   · `#findings-list` 是**页内**语义的宿主 —— `loadPageData` 与
  //     `refreshCurrentPageFindings`（SSE 每次进度帧）都会重渲染它；给它加模式
  //     就要在每个刷新点插分支，漏一个就会"翻页把总览冲掉"。
  //   · `#tier-bar` 的图例文案明确写着"本页"（`title` 属性亦然）。总览若复用
  //     它，就会出现"图例标着本页、数字却是全批"的误导。
  //   · 独立容器是**纯增量**：翻页与 SSE 照旧刷新页内清单，两者互不干扰。
  //
  // 为什么必须用 offset 分页、而不是"把 limit 提到 200"：
  //   后端 `order=confidence` 分支先取回（≤2000 行）再 `[offset:offset+limit]`
  //   切片，**前端此前从不传 offset** ⇒ limit 就是硬天花板。只抬高 limit 只是
  //   把天花板从 50 挪到 200；`offset` 续读才是**彻底**解法（排序键
  //   `(confidence, id)` 确定，故续读安全）。
  //
  // 作用域标记是**模块局部**变量，不进 `PbcReview.state`：它没有任何跨模块读取方
  // （页内渲染路径不需要知道总览是否展开），而 `state` 的键面由
  // `tests/unit/test_review_state_js.py` 快照锁定 —— 为纯呈现状态去动那份契约不划算。
  const OVERVIEW_LIMIT = 200;
  let overviewOpen = false;
  let overviewOffset = 0;
  let overviewTotal = 0;
  let overviewBusy = false;

  /** 共享映射别名包。**调用期**解析（不是模块加载期）：
   *
   * `findings-map.js` 若缺席，`renderFindings` / `overviewRow` 会在**函数内**抛错，
   * 被各自的调用方兜住（页内渲染由 review.js 的 try/catch，总览由 loadOverviewPage
   * 的 try/catch ⇒ 界面上报"加载失败"）。
   *
   * ⚠️ 绝不能"FM 缺席时降级成不转义"—— 那会把 LLM/规则产出当 HTML 注入（XSS）。
   * 失败必须**可见**，不能静默放行。
   */
  function fm() {
    return {
      esc: FM.esc,
      zhOrUnknown: FM.zhOrUnknown,
      severityZh: FM.SEVERITY_ZH,
      sourceZh: FM.SOURCE_ZH,
      typeZh: FM.TYPE_ZH,
      statusZh: FM.FINDING_STATUS_ZH,
    };
  }

  function overviewEl(id) {
    return document.getElementById(id);
  }

  /** 总览行 —— **只读**视图，故意比页内行简单。
   *
   * 页内行带裁决/修正/定位按钮，那是**权威操作面**；总览的作用是"看见 + 跳过去"，
   * 若在这里复制一套操作按钮，就会多出一个必须与页内保持同步的操作面（漂移源）。
   * 中文映射仍走共享件 `findings-map.js`（与页内同源），不自带映射表。
   */
  function overviewRow(f) {
    const { esc, zhOrUnknown, severityZh, sourceZh, typeZh, statusZh } = fm();
    const sevDot =
      f.severity === "critical"
        ? "bg-destructive"
        : f.severity === "warning"
          ? "bg-warning"
          : "bg-info";
    const tierZh = f.tier === "llm" ? "LLM 辅助" : "规则命中";
    const statusTag =
      f.status && f.status !== "pending"
        ? ` · ${esc(zhOrUnknown(statusZh, f.status))}`
        : "";
    // 页码必须归一成整数后再插进 onclick 属性 —— 属性里绝不放未净化的值。
    const page = Number(f.page) || 0;
    return (
      `<div class="border-b border-border last:border-b-0 py-2 px-1 flex items-start gap-2">` +
      `<span class="mt-1.5 w-1.5 h-1.5 rounded-full ${sevDot} shrink-0"></span>` +
      `<div class="min-w-0 flex-1">` +
      `<div class="flex items-center gap-2 flex-wrap">` +
      `<span class="text-[11px] font-medium text-foreground">${esc(zhOrUnknown(typeZh, f.type))}</span>` +
      `<span class="text-[11px] text-muted-foreground">${esc(zhOrUnknown(severityZh, f.severity))}</span>` +
      `<span class="text-[11px] text-muted-foreground">${tierZh}</span>` +
      `<span class="text-[11px] text-muted-foreground">${esc(zhOrUnknown(sourceZh, f.source))}${statusTag}</span>` +
      `</div>` +
      `<div class="text-[13px] text-foreground leading-relaxed mt-0.5">${esc(f.description || "")}</div>` +
      `</div>` +
      (page > 0
        ? `<button type="button" class="shrink-0 text-[11px] text-muted-foreground hover:text-foreground underline decoration-dotted underline-offset-2" onclick="goPage(${page})">第 ${page} 页</button>`
        : "") +
      `</div>`
    );
  }

  function overviewSummaryText() {
    const shown = overviewOffset;
    if (overviewTotal > shown) {
      return `已显示 ${shown} / 共 ${overviewTotal} 条 · 按置信度升序（最需要人工关注的排在最前）`;
    }
    return `共 ${shown} 条 · 按置信度升序（最需要人工关注的排在最前）`;
  }

  function syncOverviewChrome() {
    const list = overviewEl("all-findings-list");
    const summary = overviewEl("all-findings-summary");
    const more = overviewEl("all-findings-more");
    if (summary) summary.textContent = overviewSummaryText();
    if (more) {
      // has_more 才显示"加载更多"；否则按钮会点了没反应（假承诺）
      more.classList.toggle("hidden", !(overviewTotal > overviewOffset));
    }
    return list;
  }

  /** 拉一页总览并**追加**渲染（append，不是 replace）。 */
  async function loadOverviewPage() {
    const state = R.state;
    const jobId = state.jobId;
    if (overviewBusy) return;
    overviewBusy = true;
    // 映射在 try **内**解析：FM 缺席时抛出并被 catch 兜住（显性报错）。
    // ⚠️ 不要在 catch 里直接调 `fm().esc` —— 那会让"错误处理"本身抛错，
    // 失败随之**完全静默**（本文件首版即踩此坑，被
    // `test_failure_is_visible_never_silent` 抓住）。
    let esc = null;
    try {
      esc = fm().esc;
      const url =
        `/api/jobs/${jobId}/findings?order=confidence` +
        `&limit=${OVERVIEW_LIMIT}&offset=${overviewOffset}`;
      const r = await fetch(url);
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      const rows = data.findings || [];
      const list = overviewEl("all-findings-list");
      if (list) {
        const html = rows.map(overviewRow).join("");
        // 首页 replace、续读 append —— 否则"加载更多"会把已读的冲掉
        if (overviewOffset === 0) list.innerHTML = html;
        else list.innerHTML = list.innerHTML + html;
      }
      overviewOffset += rows.length;
      // total 由后端给（当前过滤集总数）；缺失时退回"已读条数"避免报假数字
      overviewTotal =
        typeof data.total === "number" ? data.total : overviewOffset;
      if (rows.length === 0 && overviewOffset === 0) {
        if (list) {
          list.innerHTML =
            `<div class="py-8 text-center text-[13px] text-muted-foreground">本任务没有问题记录</div>`;
        }
      }
      syncOverviewChrome();
      log("loadOverviewPage — loaded", {
        got: rows.length,
        offset: overviewOffset,
        total: overviewTotal,
      });
    } catch (err) {
      log.err("loadOverviewPage failed", err);
      const list = overviewEl("all-findings-list");
      // 失败必须**可见**：静默失败会让复核者以为"全批就这些问题"（GMP 假阴性）。
      // esc 可能因 FM 缺席而拿不到 —— 那时**不渲染**动态文本（fail-closed），
      // 绝不为了显示一句错误而注入未转义内容。
      if (list && overviewOffset === 0) {
        const detail = esc
          ? esc(String(err && err.message ? err.message : err))
          : "";
        list.innerHTML = detail
          ? `<div class="py-8 text-center text-[13px] text-destructive">` +
            `跨页总览加载失败（${detail}），请重试或改用逐页复核</div>`
          : `<div class="py-8 text-center text-[13px] text-destructive">` +
            `跨页总览加载失败，请重试或改用逐页复核</div>`;
      }
      if (window.PBC && window.PBC.showToast) {
        window.PBC.showToast("跨页总览加载失败，请重试", "err");
      }
    } finally {
      overviewBusy = false;
    }
  }

  /** 展开/收起总览。展开时**隐藏**页内清单与 tier 图例 —— 图例文案是"本页"口径，
   *  与总览并排显示会自相矛盾。收起时原样恢复（不重算页内清单）。 */
  function setOverviewOpen(open) {
    overviewOpen = !!open;
    const panel = overviewEl("all-findings-panel");
    const toggle = overviewEl("all-findings-toggle");
    const pageList = overviewEl("findings-list");
    const tierBar = overviewEl("tier-bar");
    if (panel) panel.classList.toggle("hidden", !overviewOpen);
    if (pageList) pageList.classList.toggle("hidden", overviewOpen);
    if (tierBar) tierBar.classList.toggle("hidden", overviewOpen);
    if (toggle) {
      toggle.setAttribute("aria-expanded", overviewOpen ? "true" : "false");
      toggle.textContent = overviewOpen ? "返回本页" : "全部页";
    }
    if (overviewOpen && overviewOffset === 0) {
      loadOverviewPage();
    }
  }

  function initOverview() {
    const toggle = overviewEl("all-findings-toggle");
    if (toggle) {
      toggle.addEventListener("click", () => setOverviewOpen(!overviewOpen));
    }
    const more = overviewEl("all-findings-more");
    if (more) {
      more.addEventListener("click", () => loadOverviewPage());
    }
  }

  const findings = (global.PbcReview = global.PbcReview || {});
  findings.findings = {
    renderFindings: renderFindings,
    updateTierCounts: updateTierCounts,
    emptyFindingsNote: emptyFindingsNote,
    updateFinding: updateFinding,
    correctFinding: correctFinding,
    // 跨页总览（P1）
    overviewRow: overviewRow,
    overviewSummaryText: overviewSummaryText,
    setOverviewOpen: setOverviewOpen,
    loadOverviewPage: loadOverviewPage,
    initOverview: initOverview,
  };

  // 暴露到全局（onclick 处理器需要）
  global.updateFinding = updateFinding;
  global.correctFinding = correctFinding;
})(typeof window !== "undefined" ? window : globalThis);
