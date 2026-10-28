/* ============================================================
   Settings 页 · 合规规则编辑器 + 模板面板
   ------------------------------------------------------------
   由 static/settings.js 按职责拆出（docs/ADVERSARIAL_REVIEW_2026-09-28.md §维度 4）。
   状态宿主见 settings-state.js —— 本文件**不得**自建 current/activeProvider 等同名变量。
   ============================================================ */
(function () {
  "use strict";

  const S = window.PbcSettings;

  const { log } = S;

  // ============================================================
  // 合规规则编辑器 — 用户自定义规则注入跨页 LLM 分析
  // ============================================================
  let rules = [];
  let ruleHits = {};

  const RULE_TEMPLATES = [
    "产品 {产品名} 的中间体储存温度必须控制在 15-25°C",
    "关键工序（如灭菌、灌装）必须双人复核签名",
    "批号必须在所有页面保持一致，不得混批生产",
    "每批产品必须附有放行检验报告（COA）",
  ];

  // 模板库 — 按 GMP 检查域分组的内置合规规则模板（Ⅰ-2：一键添加）
  const RULE_TEMPLATE_LIBRARY = [
    {
      group: "温湿度",
      items: [
        "产品 {产品名} 的中间体储存温度必须控制在 15-25°C",
        "冷库储存产品温度必须控制在 2-8°C，不得冷冻",
        "生产车间湿度必须控制在 45%-65% RH",
      ],
    },
    {
      group: "批号",
      items: [
        "批号必须在所有页面保持一致，不得混批生产",
        "批号格式必须为 YYMMDD-序号（如 240801-01）",
      ],
    },
    {
      group: "签名复核",
      items: [
        "关键工序（灭菌、灌装、称量）必须双人复核签名",
        "批生产记录每页必须有操作人签名和日期",
      ],
    },
    {
      group: "检验放行",
      items: [
        "每批产品必须附有放行检验报告（COA）",
        "检验不合格的批次不得放行，须执行偏差处理",
      ],
    },
    {
      group: "称量物料",
      items: [
        "称量记录必须与实际投料量一致，误差不得超过 0.5%",
        "关键原辅料必须具有入库检验合格标识",
      ],
    },
    {
      group: "时间过程",
      items: [
        "工艺参数必须符合处方要求，不得擅自变更",
        "设备清洁后必须填写清洁记录并经复核人确认",
      ],
    },
  ];

  let ruleLastSaved = null;

  async function loadRules() {
    try {
      const r = await fetch("/api/settings/rules");
      const data = await r.json();
      rules = Array.isArray(data.rules) ? data.rules : [];
      ruleHits = data.hits && typeof data.hits === "object" ? data.hits : {};
      ruleLastSaved = data.last_saved_at || null;
      log("rules loaded", { count: rules.length, lastSaved: ruleLastSaved });
      renderRules();
    } catch (err) {
      // P3 修复：规则加载失败不再静默 — 区块直接可见提示，避免用户
      // 误以为"没有规则"而重复添加。
      log.err("load rules failed", err);
      const el = document.getElementById("rule-msg");
      if (el) el.textContent = `✗ 规则加载失败: ${err.message}`;
    }
  }

  function renderRuleSavedBadge() {
    const el = document.getElementById("rule-last-saved");
    if (!el) return;
    if (!ruleLastSaved || ruleLastSaved === "刚刚") {
      el.textContent =
        ruleLastSaved === "刚刚" ? "刚刚已保存，将注入下次跨页分析" : "从未成功保存 — 规则不会生效";
      el.className =
        "ml-1.5 px-1.5 py-0.5 rounded bg-destructive/10 text-[11px] font-normal" +
        (ruleLastSaved === "刚刚" ? " text-foreground" : " text-destructive");
    } else {
      el.textContent = `上次保存 ${ruleLastSaved}  · 命中 ${rules.reduce(
        (n, r) => n + (ruleHits[r.id] || 0),
        0
      )} 次`;
      el.className =
        "ml-1.5 px-1.5 py-0.5 rounded bg-muted text-muted-foreground text-[11px] font-normal";
    }
    void el;
  }

  function renderRules() {
    const listEl = document.getElementById("rules-list");
    const countEl = document.getElementById("rules-count");
    renderRuleSavedBadge();
    updateRuleTotalChars();
    if (!listEl) return;
    listEl.innerHTML = "";
    if (countEl) countEl.textContent = `${rules.length} 条`;
    if (rules.length === 0) {
      const empty = document.createElement("p");
      empty.className = "text-[12px] text-muted-foreground";
      empty.textContent = "暂无自定义规则 — 跨页分析仅使用内置规则 R1-R8 与 LLM 语义检查";
      listEl.appendChild(empty);
      return;
    }
    rules.forEach((rule, idx) => {
      const row = document.createElement("div");
      row.className = "flex items-start gap-2";
      row.dataset.ruleIndex = String(idx);

      const num = document.createElement("span");
      num.className = "mt-2.5 w-5 shrink-0 text-right text-[11px] text-muted-foreground";
      num.textContent = `${idx + 1}`;

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "mt-2 w-4 h-4 accent-foreground cursor-pointer";
      checkbox.checked = !!rule.active;
      checkbox.title = "启用/停用";
      checkbox.addEventListener("change", () => {
        rule.active = checkbox.checked;
        ruleDirty();
      });

      const textarea = document.createElement("textarea");
      textarea.rows = 2;
      textarea.className =
        "flex-1 resize-y rounded-md border border-border bg-background px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-foreground/40";
      textarea.value = rule.text || "";
      textarea.maxLength = 1000;
      textarea.placeholder = "输入合规规则，例如：XX 产品中间体储存温度必须 15-25°C";
      textarea.addEventListener("input", () => {
        rule.text = textarea.value;
        ruleDirty();
      });

      const hit = ruleHits[rule.id] || 0;
      const badge = document.createElement("span");
      badge.className =
        "mt-2 shrink-0 px-1.5 py-0.5 rounded text-[11px] " +
        (hit > 0 ? "bg-foreground text-background" : "bg-muted text-muted-foreground");
      badge.textContent = hit > 0 ? `命中 ${hit}` : "0 命中";
      badge.title = `历史命中 ${hit} 次（GMP 溯源：findings.user_rule_id）`;

      const del = document.createElement("button");
      del.type = "button";
      del.className =
        "mt-1.5 shrink-0 px-2 py-0.5 text-[13px] text-muted-foreground hover:text-destructive transition-colors";
      del.textContent = "×";
      del.title = "删除此规则";
      del.addEventListener("click", async () => {
        const ok = await window.PBC.confirmDialog({
          title: "删除此规则？",
          message: "删除后下次跨页分析将不再检查该规则。",
          confirmText: "删除",
          cancelText: "取消",
          danger: true,
        });
        if (!ok) return;
        rules.splice(idx, 1);
        renderRules();
        ruleDirty();
      });

      row.appendChild(num);
      row.appendChild(checkbox);
      row.appendChild(textarea);
      row.appendChild(badge);
      row.appendChild(del);
      listEl.appendChild(row);
    });
  }

  function ruleDirty() {
    const msg = document.getElementById("rule-msg");
    if (msg) msg.textContent = "有未保存的修改";
    updateRuleTotalChars();
  }

  // 总字数实时统计（后端 _USER_RULES_TOTAL_MAX=8000：全部规则注入跨页分析
  // 提示词，过长超出模型上下文）
  function updateRuleTotalChars() {
    const el = document.getElementById("rules-total-chars");
    if (!el) return;
    const total = rules.reduce((n, r) => n + (r.text || "").length, 0);
    const MAX = 8000;
    el.textContent = `${total} / ${MAX} 字`;
    el.className =
      "ml-1.5 px-1.5 py-0.5 rounded text-[11px] font-normal " +
      (total > MAX
        ? "bg-destructive/10 text-destructive"
        : "bg-muted text-muted-foreground");
  }

  async function saveRules() {
    const btn = document.getElementById("rule-save-btn");
    const msg = document.getElementById("rule-msg");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "保存中…";
    }
    try {
      const payload = {
        rules: rules.map((r) => ({
          id: r.id || undefined,
          text: (r.text || "").trim(),
          active: !!r.active,
        })),
      };
      const r = await fetch("/api/settings/rules", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (r.ok && data.ok) {
        rules = data.rules || [];
        ruleLastSaved = "刚刚";
        renderRules();
        if (msg) msg.textContent = "✓ 已保存，将注入下次跨页分析";
      } else {
        const errs = data.detail && data.detail.errors ? data.detail.errors : [data.detail || "保存失败"];
        if (msg) msg.textContent = `✗ ${errs.join("；")}`;
      }
    } catch (err) {
      if (msg) msg.textContent = `✗ ${err.message}`;
      log.err("save rules failed", err);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "保存规则";
      }
    }
  }

  document.getElementById("rule-add-btn")?.addEventListener("click", () => {
    const text = RULE_TEMPLATES[rules.length % RULE_TEMPLATES.length];
    rules.push({ id: undefined, text: "", active: true });
    renderRules();
    const listEl = document.getElementById("rules-list");
    const rows = listEl ? listEl.querySelectorAll("[data-rule-index]") : [];
    const ta = rows.length ? rows[rows.length - 1].querySelector("textarea") : null;
    if (ta) {
      ta.value = text;
      ta.dispatchEvent(new Event("input", { bubbles: true }));
      ta.focus();
      ta.selectionStart = ta.value.length;
    }
    ruleDirty();
  });

  // 模板库面板 — 分组展示内置合规规则模板，点选一键添加。
  // 就地展开（文档流内）而非 absolute dropdown：父卡片 overflow-hidden
  // 会裁剪 absolute 面板导致内容被遮挡，只能靠滚动查看部分模板。
  function renderTemplatePanel() {
    const panel = document.getElementById("rule-template-panel");
    if (!panel) return;
    panel.innerHTML = "";
    RULE_TEMPLATE_LIBRARY.forEach((section) => {
      const group = document.createElement("div");
      group.className =
        "px-2 pt-2 pb-1 text-[11px] font-medium text-muted-foreground uppercase tracking-wide";
      group.textContent = section.group;
      panel.appendChild(group);
      const grid = document.createElement("div");
      grid.className = "grid grid-cols-1 gap-px px-1";
      section.items.forEach((text) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className =
          "text-left w-full px-2 py-1.5 text-[12px] leading-snug rounded-md hover:bg-muted/60 transition-colors";
        item.textContent = text;
        item.addEventListener("click", () => {
          rules.push({ id: undefined, text, active: true });
          renderRules();
          toggleTemplatePanel(false);
          ruleDirty();
        });
        grid.appendChild(item);
      });
      panel.appendChild(grid);
    });
  }

  function toggleTemplatePanel(force) {
    const panel = document.getElementById("rule-template-panel");
    if (!panel) return;
    const show = force === undefined ? panel.classList.contains("hidden") : force;
    panel.classList.toggle("hidden", !show);
    if (show) renderTemplatePanel();
  }

  document.getElementById("rule-template-btn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleTemplatePanel();
  });

  // 就地展开面板：Esc 关闭；点击面板外部不强制关闭（inline 面板与内容
  // 同流，外部点击关闭会让用户选模板时误触关闭）
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") toggleTemplatePanel(false);
  });

  document.getElementById("rule-save-btn")?.addEventListener("click", saveRules);

  Object.assign(S, { loadRules });
})();
