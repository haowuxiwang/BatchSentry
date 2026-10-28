/* ============================================================
   Review 页 — 审计面板（TODO 0-53 / 0-54，Round 88 第三十七批）

   ## 为什么需要这个面板

   后端两个端点 `GET /api/jobs/{id}/audit`（变更历史）与
   `GET /api/jobs/{id}/llm_audit`（模型调用）**早就存在**并返回
   `{entries, count}`，但引入本文件之前 `grep -rn audit static/ templates/`
   是 **0 命中** —— 数据在库里、接口活着、界面全黑。GMP 追溯要回答的三个
   问题（谁在什么时候改了什么 / 这条 finding 是哪个模型哪个版本给的 /
   上一次"重新分析"之前的复核结论去哪了）在 UI 上**没有任何出口**。

   `findings_archive` 更极端：只有写入点（`api/jobs/actions.py` 的
   review→retry 归档、`core/pipeline/stage3.py` 的 LLM 层重算），**没有读接口**
   ⇒ 本文件配套新增 `GET /api/jobs/{id}/archived_findings`。

   ## 三条纪律（都对应一次具体取舍）

   1. **只用 DOM API 渲染**（createElement + textContent），不用 innerHTML
      拼数据。审计内容含 OCR 原文与上游 `error` 文本 —— 那是不可信输入；
      拼字符串就是 XSS 面，也躲不开 `test_innerhtml_escaping` 的登记。
   2. **`action` 不翻译**。它是 GMP 追溯用的机器标识（审计员按 code 对表），
      而 `detail` 本来就是中文。再维护一张 action→中文 的表，就是**第四份
      中文真值**（status.js / findings-map.js / zh_map.py 之外）—— 必然漂移。
   3. **失败必须说出来**。取不到数据时在面板里显示状态码，**不**静默成空表：
      空表会被读成"没有变更"，那是一个错误的合规结论。
   ============================================================ */
(function (global) {
  "use strict";

  const FM = global.PbcFindingsMap || {};
  const typeZh = FM.typeZh || ((k) => (k == null ? "" : String(k)));
  const statusZh = FM.findingStatusZh || ((k) => (k == null ? "" : String(k)));

  // 一次取 200 条：复核页看的是"这次任务发生了什么"，不是全库检索。
  const LIMIT = 200;

  const TABS = [
    {
      key: "audit",
      label: "变更历史",
      columns: ["时间", "动作", "详情"],
      // R116：每个 tab 顶部一句"为什么会有这张表" —— 用户打开审计面板时
      // 最缺的不是数据，而是"我该在这里找什么"。
      hint: "这个任务从上传到现在发生过什么。审计追溯时按时间倒序看。",
      url: (id) => `/api/jobs/${encodeURIComponent(id)}/audit?limit=${LIMIT}`,
    },
    {
      key: "llm",
      label: "模型调用",
      columns: ["时间", "阶段", "模型", "版本", "耗时", "Token", "结果"],
      hint:
        "系统每次调用大模型的时间与消耗。某条结论看着不对时，用时间和阶段"
        + "对回是哪一次调用给出的。",
      url: (id) => `/api/jobs/${encodeURIComponent(id)}/llm_audit?limit=${LIMIT}`,
    },
    {
      key: "archived",
      label: "已归档",
      columns: ["归档时间", "页", "类型", "状态", "问题描述", "复核意见"],
      hint:
        "「重新分析」会覆盖上一次的复核结论，被覆盖的那份就留在这里 —— "
        + "想找回上一轮判断时看这张表。",
      url: (id) =>
        `/api/jobs/${encodeURIComponent(id)}/archived_findings?limit=${LIMIT}`,
    },
  ];

  // 空态文案：三句都必须回答"为什么会空"，否则用户会以为功能坏了。
  const EMPTY = {
    audit: "没有变更记录。任务尚未跑过流水线时这里会是空的。",
    llm: "没有模型调用记录。未启用 LLM 辅助（纯规则模式）或任务还没进入分析阶段时为空。",
    archived:
      "没有归档记录。只有对「已完成复核」的任务点「重新分析」时，上一次的复核结论才会被归档到这里。",
  };

  function dash(v) {
    if (v === null || v === undefined || v === "") return "—";
    return String(v);
  }

  // ── R116：把机器串翻成人话 ──────────────────────────────────────────
  //
  // 用户 2026-10-26 的反馈：「审计记录部分，前端显示太过工程化」。实测
  // 原始 `detail` 长这样：
  //     pipeline_complete      status=review findings=204 failed=0 total=1054199ms
  //     stage1_complete        pages=51 duration=57091ms backend=cached
  //     stage2_complete        duration=775027ms failed=[]
  //     ocr_input_normalized   pages=[1, 2, 3, ... 51]
  //     ocr_input_normalized_diag  original_pdf_diags={"1": {"media_box_pt": ...}}
  //
  // 三条纪律不变：
  //   ① **不删信息** —— 原始串永远进 `title`（悬停可见），人类化只是默认视图；
  //   ② **不认识就原样显示** —— 新增 action 不会因为没进映射表就变成空白；
  //   ③ 翻译只在**这一处**（`ACTION_ZH`）。`action` 本身仍是 GMP 追溯用的
  //      机器码，界面上以「中文名 + 机器码（悬停）」同时存在，审计员照样能
  //      按 code 对表，而普通复核员看得懂。

  //: 已知 action → 短中文名。**新增 action 不必急着补这里** —— 缺了就走原始码。
  const ACTION_ZH = {
    pipeline_start: "开始处理",
    pipeline_complete: "处理完成",
    status_transition: "状态变更",
    stage1_complete: "OCR 完成",
    stage1_skipped: "复用已有 OCR",
    stage2_complete: "逐页分析完成",
    spec_guard_dropped: "丢弃误报",
    findings_overlap_suppressed: "合并重复结论",
    llm_guard_suppressed: "否定 LLM 结论",
    feishu_notify: "飞书通知",
    user_rules_update: "用户规则变更",
    settings_update: "设置变更",
    job_deleted: "删除任务",
    analysis_reset: "重新分析",
    report_export: "导出报告",
    stuck_recovery: "中断恢复",
    llm_test_ok: "LLM 连通测试成功",
    llm_test_failed: "LLM 连通测试失败",
    ocr_input_normalized: "OCR 输入归一",
    ocr_input_normalized_diag: "OCR 输入诊断",
  };

  /** 毫秒 → "57 秒" / "12 分 55 秒" / "1 小时 3 分"。 */
  function fmtMs(ms) {
    // `Number(null)` 是 0 —— 不显式挡掉的话"耗时未知"会被显示成"<1 秒"，
    // 那是把"没记录"说成了"很快"（审计场景下属于假结论）。
    if (ms === null || ms === undefined || ms === "") return "—";
    const n = Number(ms);
    if (!isFinite(n) || n < 0) return "—";
    if (n < 1000) return "<1 秒";
    const s = Math.round(n / 1000);
    if (s < 60) return `${s} 秒`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m} 分${s % 60 ? ` ${s % 60} 秒` : ""}`;
    return `${Math.floor(m / 60)} 小时${m % 60 ? ` ${m % 60} 分` : ""}`;
  }

  /** 从 `k=v` 串里取值（`detail` 的通用形状）。 */
  function kv(detail, key) {
    const m = new RegExp(`(?:^|\\s)${key}=([^\\s]*)`).exec(detail || "");
    return m ? m[1] : null;
  }

  /** `pages=[1, 2, 3]` → 元素个数；取不到返回 null。 */
  function listLen(detail, key) {
    const m = new RegExp(`${key}=\\[([^\\]]*)\\]`).exec(detail || "");
    if (!m) return null;
    const body = m[1].trim();
    return body ? body.split(",").length : 0;
  }

  /**
   * `detail` → 人话。**不认识的形态原样返回**（fail-open）。
   *
   * 各分支只做"把已有事实换个说法"，不推断任何新结论 —— 审计记录的价值在于
   * 忠实，不在于好看。
   */
  function detailZh(action, detail) {
    const d = String(detail == null ? "" : detail);
    if (!d) return "—";
    const a = String(action || "");

    if (a === "pipeline_complete") {
      const findings = kv(d, "findings");
      const failed = kv(d, "failed");
      const total = kv(d, "total");
      const parts = [];
      if (findings != null) parts.push(`共 ${findings} 条问题`);
      if (failed != null) parts.push(`${failed === "0" ? "无" : failed + " "}页失败`);
      if (total != null) parts.push(`总耗时 ${fmtMs(total.replace(/ms$/, ""))}`);
      return parts.length ? parts.join("，") : d;
    }

    if (a === "stage1_complete" || a === "stage2_complete") {
      const pages = kv(d, "pages");
      const dur = kv(d, "duration");
      const backend = kv(d, "backend");
      const failed = listLen(d, "failed");
      const parts = [];
      if (pages != null) parts.push(`${pages} 页`);
      if (dur != null) parts.push(`耗时 ${fmtMs(dur.replace(/ms$/, ""))}`);
      if (backend != null) {
        parts.push(`OCR 后端：${backend === "cached" ? "复用缓存" : backend}`);
      }
      if (failed != null) {
        parts.push(failed === 0 ? "无失败页" : `${failed} 页失败`);
      }
      return parts.length ? parts.join("，") : d;
    }

    if (a === "stage1_skipped") {
      const m = /reuse\s+(\d+)/.exec(d);
      return m
        ? `复用 ${m[1]} 页已有 OCR 结果（未重新识别，不消耗额度）`
        : d;
    }

    if (a === "ocr_input_normalized" || a === "ocr_input_normalized_diag") {
      const n = listLen(d, "pages");
      if (n != null) return `已归一 ${n} 页的 OCR 输入参数`;
      return "记录了 PDF 原始尺寸/旋转信息（排查用，展开可看原值）";
    }

    if (a === "findings_overlap_suppressed" || a === "llm_guard_suppressed") {
      const count = kv(d, "count");
      if (count == null) return d;
      return a === "findings_overlap_suppressed"
        ? `合并 ${count} 条重复结论（同一页同一类型已由规则层给出）`
        : `否定 ${count} 条 LLM 结论（经确定性复核判定不成立），明细见「被抑制的结论」`;
    }

    if (a === "spec_guard_dropped") {
      const page = kv(d, "page");
      const n = kv(d, "suppressed");
      const first = d.split(": ").slice(1).join(": ");
      const head = page != null
        ? `第 ${page} 页：丢弃 ${n == null ? "" : n + " "}条误报`
        : `丢弃 ${n == null ? "" : n + " "}条误报`;
      return first ? `${head}（如「${first.slice(0, 60)}」）` : head;
    }

    if (a === "feishu_notify") {
      const ok = kv(d, "ok");
      if (ok === "True" || ok === "true") return "已发送失败通知";
      const code = kv(d, "code");
      const msg = d.split("msg=").slice(1).join("msg=");
      return code != null
        ? `通知发送失败（飞书返回 ${code}${msg ? " " + msg : ""}）`
        : "通知发送失败";
    }

    if (a === "stuck_recovery") {
      const m = /recovered on startup:\s*(\S+)\s*→\s*(\S+)/.exec(d);
      return m
        ? `启动时发现任务卡在「${m[1]}」，已标记为失败供重试`
        : d;
    }

    if (a === "pipeline_start") {
      const pdf = kv(d, "pdf");
      const backend = kv(d, "ocr_backend");
      if (pdf || backend) {
        const parts = [];
        if (pdf) parts.push(`文件：${pdf}`);
        if (backend) parts.push(`OCR 后端：${backend}`);
        return parts.join("，");
      }
      return d; // 「Uploaded xxx.pdf (138300288 bytes, 51 pages…)」这种原样即可
    }

    // `status_transition` 的 detail 本来就是中文句子；其余未知 action 同理。
    return d;
  }

  /** 一行里「动作」列的展示：中文名优先，机器码进 title。 */
  function actionLabel(action) {
    const code = String(action == null ? "" : action);
    return ACTION_ZH[code] || code || "—";
  }

  // ── 纯函数：把一行记录变成一组单元格文本（node 侧可直接断言） ──
  //
  // 单元格可以是字符串，也可以是 `{text, title}` —— 后者用于"人类可读但
  // 保留原始串"的列（悬停看到机器原文，便于按 GMP 台账核对）。
  function auditRow(e) {
    return [
      dash(e.created_at),
      { text: actionLabel(e.action), title: dash(e.action) },
      { text: detailZh(e.action, e.detail), title: dash(e.detail) },
    ];
  }

  function llmRow(e) {
    const model = `${dash(e.provider)} / ${dash(e.model)}`;
    // R116：`16898 ms` 对复核员没有意义；换成秒，并把 token 加千分位。
    const ms = e.latency_ms == null ? "—" : fmtMs(e.latency_ms);
    const tok =
      e.total_tokens == null ? "—" : Number(e.total_tokens).toLocaleString("en-US");
    const res =
      Number(e.success) === 1 ? "成功" : `失败：${dash(e.error)}`;
    return [
      dash(e.created_at),
      dash(e.stage),
      model,
      dash(e.prompt_version),
      ms,
      tok,
      res,
    ];
  }

  function archiveRow(e) {
    return [
      dash(e.archived_at),
      e.page == null ? "—" : `第 ${e.page} 页`,
      typeZh(e.type) || dash(e.type),
      statusZh(e.status) || dash(e.status),
      dash(e.description),
      dash(e.reviewer_note),
    ];
  }

  const ROW_FN = { audit: auditRow, llm: llmRow, archived: archiveRow };

  // ── DOM 渲染 ──
  let overlay = null;
  let opener = null;
  let activeTab = "audit";
  const cache = Object.create(null);

  function jobId() {
    const st = global.PbcReview && global.PbcReview.state;
    const id = (st && st.jobId) || (global.__PBC__ && global.__PBC__.job_id) || "";
    return String(id);
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function buildShell() {
    const ov = el(
      "div",
      "fixed inset-0 z-[60] flex items-center justify-center px-4",
    );
    ov.id = "audit-overlay";
    ov.setAttribute("role", "dialog");
    ov.setAttribute("aria-modal", "true");
    ov.setAttribute("aria-labelledby", "audit-title");
    ov.style.cssText =
      "background: hsl(var(--foreground) / 0.32); backdrop-filter: blur(2px);";

    const modal = el(
      "div",
      "bg-card border border-border rounded-lg w-full max-w-3xl overflow-hidden flex flex-col",
    );
    modal.id = "audit-panel";
    modal.style.cssText = "box-shadow: var(--shadow-strong); max-height: 80vh;";

    const head = el("div", "flex items-center gap-2 px-5 py-3 border-b border-border");
    head.appendChild(el("span", "text-[14px] font-medium", "审计记录"));
    head.appendChild(
      el(
        "span",
        "text-[11px] text-muted-foreground",
        "GMP 追溯：变更 / 模型调用 / 重分析归档",
      ),
    );
    const spacer = el("div", "flex-1");
    head.appendChild(spacer);
    const closeBtn = el(
      "button",
      "btn-press focus-ring text-[12px] px-2 py-1 rounded text-muted-foreground hover:text-foreground",
      "关闭",
    );
    closeBtn.type = "button";
    closeBtn.id = "audit-close";
    closeBtn.addEventListener("click", close);
    head.appendChild(closeBtn);
    modal.appendChild(head);

    const tabBar = el("div", "flex items-center gap-1 px-5 pt-2");
    tabBar.id = "audit-tabs";
    TABS.forEach((t) => {
      const b = el(
        "button",
        "btn-press focus-ring text-[12px] px-2 py-1 rounded text-muted-foreground hover:text-foreground",
        t.label,
      );
      b.type = "button";
      b.dataset.tab = t.key;
      b.setAttribute("aria-selected", "false");
      b.addEventListener("click", () => switchTab(t.key));
      tabBar.appendChild(b);
    });
    modal.appendChild(tabBar);

    modal.appendChild(el("div", "px-5 py-1", "")); // 视觉留白
    const body = el("div", "px-5 pb-4 overflow-y-auto");
    body.id = "audit-body";
    modal.appendChild(body);

    ov.appendChild(modal);
    ov.addEventListener("click", (e) => {
      if (e.target === ov) close();
    });
    return ov;
  }

  function markTabs() {
    if (!overlay) return;
    overlay.querySelectorAll("#audit-tabs button").forEach((b) => {
      const on = b.dataset.tab === activeTab;
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.className = on
        ? "btn-press focus-ring text-[12px] px-2 py-1 rounded bg-muted font-medium text-foreground"
        : "btn-press focus-ring text-[12px] px-2 py-1 rounded text-muted-foreground hover:text-foreground";
    });
  }

  function bodyEl() {
    return overlay ? overlay.querySelector("#audit-body") : null;
  }

  function showStatus(text) {
    const body = bodyEl();
    if (!body) return;
    body.replaceChildren(el("p", "text-[12px] text-muted-foreground py-3", text));
  }

  function renderTable(tab, entries) {
    const body = bodyEl();
    if (!body) return;
    const def = TABS.find((t) => t.key === tab);
    const rowFn = ROW_FN[tab];
    if (!entries.length) {
      body.replaceChildren(
        el("p", "text-[12px] text-muted-foreground py-3", EMPTY[tab]),
      );
      return;
    }
    const count = el(
      "p",
      "text-[11px] text-muted-foreground py-1",
      `共 ${entries.length} 条（最多显示 ${LIMIT} 条）`,
    );
    // R116：先给一句"这张表是干什么的"，再上数据。
    const hintEl = el(
      "p",
      "text-[11px] text-muted-foreground py-1 leading-relaxed",
      def.hint || "",
    );
    const table = el("table", "w-full text-[12px] border-collapse");
    const thead = el("thead", "");
    const htr = el("tr", "");
    def.columns.forEach((c) => {
      const th = el(
        "th",
        "text-[11px] uppercase tracking-wider text-muted-foreground text-left py-1 px-1 border-b border-border",
        c,
      );
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    table.appendChild(thead);
    const tbody = el("tbody", "");
    entries.forEach((e) => {
      const tr = el("tr", "");
      rowFn(e).forEach((cell, i) => {
        // 时间/动作/模型 用等宽字体（对齐便于逐行比对）；描述类列自动换行。
        const mono = i === 0 || (tab === "audit" && i === 1);
        // R116：单元格可以是 `{text, title}` —— 人类可读的展示 + 原始机器串悬停。
        const isObj = cell !== null && typeof cell === "object";
        const text = isObj ? cell.text : cell;
        const td = el(
          "td",
          `py-1 px-1 align-top border-b border-border break-words${
            mono ? " font-mono text-[11px] text-muted-foreground" : ""
          }`,
          text,
        );
        if (isObj && cell.title) td.title = cell.title;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    body.replaceChildren(hintEl, count, table);
  }

  async function load(tab) {
    const def = TABS.find((t) => t.key === tab);
    if (!def) return;
    if (cache[tab]) {
      renderTable(tab, cache[tab]);
      return;
    }
    const id = jobId();
    if (!id) {
      showStatus("取不到任务 ID，无法读取审计记录。");
      return;
    }
    showStatus("加载中…");
    try {
      const res = await fetch(def.url(id), {
        headers: { Accept: "application/json" },
      });
      if (!res.ok) {
        // 不静默成空表：状态码本身就是结论（403 = 非本机请求被拒）
        showStatus(`读取失败：HTTP ${res.status}（${def.label}）`);
        return;
      }
      const data = await res.json();
      const entries = Array.isArray(data && data.entries) ? data.entries : [];
      cache[tab] = entries;
      // 异步返回时用户可能已经切走了 —— 只在仍是当前 tab 时落地
      if (tab === activeTab) renderTable(tab, entries);
    } catch (err) {
      showStatus(`读取失败：${(err && err.message) || err}`);
    }
  }

  function switchTab(tab) {
    if (!TABS.some((t) => t.key === tab)) return;
    activeTab = tab;
    markTabs();
    load(tab);
  }

  function onKey(e) {
    if (!overlay) return;
    if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  }

  function close() {
    if (!overlay) return;
    document.removeEventListener("keydown", onKey);
    overlay.remove();
    overlay = null;
    if (opener && typeof opener.focus === "function") opener.focus();
    opener = null;
  }

  function open(ev) {
    if (overlay) return; // 幂等：重复点击不叠层
    opener = ev && ev.currentTarget ? ev.currentTarget : null;
    activeTab = "audit";
    overlay = buildShell();
    document.body.appendChild(overlay);
    markTabs();
    document.addEventListener("keydown", onKey);
    load(activeTab);
  }

  global.PbcAudit = {
    open: open,
    close: close,
    switchTab: switchTab,
    TABS: TABS,
    EMPTY: EMPTY,
    LIMIT: LIMIT,
    auditRow: auditRow,
    llmRow: llmRow,
    archiveRow: archiveRow,
    // R116：人类化判据单独暴露 —— 这样"某个 action 翻错了"可以直接断言，
    // 不必通过整行渲染去反推。
    ACTION_ZH: ACTION_ZH,
    actionLabel: actionLabel,
    detailZh: detailZh,
    fmtMs: fmtMs,
  };
  // 模板内联 onclick 需要全局函数（defer 脚本先于用户点击执行）
  global.openAuditPanel = open;
})(typeof window !== "undefined" ? window : globalThis);
