/* ============================================================
   Settings 页 · Section 导航 + 知识库面板 + OCR 配置 + load() 初始渲染
   ------------------------------------------------------------
   由 static/settings.js 按职责拆出（docs/ADVERSARIAL_REVIEW_2026-09-28.md §维度 4）。
   状态宿主见 settings-state.js —— 本文件**不得**自建 current/activeProvider 等同名变量。
   ============================================================ */
(function () {
  "use strict";

  const S = window.PbcSettings;

  const { log, display, showBackendForm, statusBadge } = S;

  // ============================================================
  // Section 导航 — 参考 GitHub/Notion sidebar 模式
  // ============================================================
  function initSectionNav() {
    const navs = document.querySelectorAll(".settings-nav, .settings-nav-mobile");
    const sections = document.querySelectorAll("[data-section]");
    if (!navs.length || !sections.length) return;

    function applyNav(target) {
      sections.forEach((s) =>
        s.classList.toggle("hidden", s.dataset.section !== target),
      );
      navs.forEach((n) =>
        n.querySelectorAll(".settings-nav-link").forEach((l) => {
          const on = l.dataset.target === target;
          l.classList.toggle("active", on);
          if (on) l.setAttribute("aria-current", "page");
          else l.removeAttribute("aria-current");
        }),
      );
    }

    function hashTarget() {
      const hash = location.hash.slice(1);
      return hash && document.querySelector('[data-section="' + hash + '"]')
        ? hash
        : "llm";
    }

    navs.forEach((nav) => {
      nav.addEventListener("click", (e) => {
        const link = e.target.closest("[data-target]");
        if (!link) return;
        // 不 preventDefault：<a href="#ocr"> 默认行为更新 hash、
        // 写入历史栈并触发 hashchange，使后退/前进/收藏链接均可用。
        // 此处同步切换避免等待 hashchange 的闪烁。
        applyNav(link.dataset.target);
      });
    });

    // URL hash 变化（点击/后退/前进/直达）统一应用
    window.addEventListener("hashchange", () => applyNav(hashTarget()));

    // 初始: 显示 URL hash 对应的 section，否则默认 llm
    applyNav(hashTarget());
    log("section nav initialized", { initial: hashTarget() });

    // 知识库分区：首次切入时懒加载（KB v8）
    let kbLoaded = false;
    const maybeLoadKb = (target) => {
      if (target !== "kb" || kbLoaded) return;
      kbLoaded = true;
      initKbPanel();
    };
    document.querySelectorAll(".settings-nav, .settings-nav-mobile").forEach(
      (nav) => nav.addEventListener("click", (e) => {
        const link = e.target.closest("[data-target]");
        if (link) maybeLoadKb(link.dataset.target);
      }),
    );
    window.addEventListener("hashchange", () => maybeLoadKb(hashTarget()));
    if (hashTarget() === "kb") maybeLoadKb("kb");
  }

  function initKbPanel() {
    const list = document.getElementById("kb-list");
    const meta = document.getElementById("kb-meta");
    const count = document.getElementById("kb-count");
    const search = document.getElementById("kb-search");
    const sourcesEl = document.getElementById("kb-sources");
    const sourceFilter = document.getElementById("kb-source-filter");
    const sourceMsg = document.getElementById("kb-source-msg");
    if (!list) return;

    // 最近一次 /api/settings/kb 返回的来源清单（开关状态由 disabled 派生）
    let sources = [];
    let disabled = new Set();

    function renderSources() {
      if (!sourcesEl) return;
      sourcesEl.innerHTML = "";
      if (!sources.length) {
        const p = document.createElement("p");
        p.className = "text-[12px] text-muted-foreground px-1 py-1";
        p.textContent = "无可用来源";
        sourcesEl.appendChild(p);
        return;
      }
      for (const s of sources) {
        const row = document.createElement("div");
        row.className = "flex items-center gap-2 px-1 py-1";

        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.className = "w-4 h-4 accent-foreground cursor-pointer shrink-0";
        cb.checked = !disabled.has(s.source_id);
        cb.title = "启用/停用该来源（停用后不再参与引用）";
        cb.addEventListener("change", () => onToggle(s.source_id, cb));

        const label = document.createElement("span");
        label.className = "text-[12px] text-foreground truncate";
        label.textContent = s.title || s.source_id;
        label.title = `${s.source_id}${s.document_no ? " · " + s.document_no : ""}`;

        const kind = document.createElement("span");
        kind.className = "shrink-0 px-1.5 py-0.5 rounded text-[10px] " +
          (s.text_kind === "original"
            ? "bg-muted text-muted-foreground"
            : "bg-foreground/10 text-foreground");
        kind.textContent = s.text_kind === "original" ? "原文" : "摘编";

        const info = document.createElement("span");
        info.className =
          "ml-auto shrink-0 text-[11px] text-muted-foreground tabular-nums";
        info.textContent = `${s.entries} 条 · v${s.version}`;

        row.appendChild(cb);
        row.appendChild(label);
        row.appendChild(kind);
        row.appendChild(info);
        sourcesEl.appendChild(row);
      }
    }

    async function onToggle(sourceId, cb) {
      const next = new Set(disabled);
      if (cb.checked) next.delete(sourceId); else next.add(sourceId);
      // 不允许停用全部来源（后端亦拒绝；前端先拦，避免无谓请求）
      if (next.size >= sources.length) {
        cb.checked = true;
        if (sourceMsg) sourceMsg.textContent = "✗ 至少保留一个来源";
        return;
      }
      const prev = disabled;
      disabled = next;
      if (sourceMsg) sourceMsg.textContent = "保存中…";
      try {
        const r = await fetch("/api/settings/kb/sources", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ disabled: [...disabled] }),
        });
        const d = await r.json();
        if (r.ok && d.ok) {
          disabled = new Set(d.disabled || []);
          if (sourceMsg) {
            sourceMsg.textContent = disabled.size
              ? `✓ 已停用 ${disabled.size} 个来源`
              : "✓ 全部来源已启用";
          }
        } else {
          disabled = prev;
          const errs = d.detail && d.detail.errors
            ? d.detail.errors : [d.detail || "保存失败"];
          if (sourceMsg) sourceMsg.textContent = `✗ ${errs.join("；")}`;
        }
      } catch (err) {
        disabled = prev;
        if (sourceMsg) sourceMsg.textContent = `✗ ${err.message}`;
        log.err("kb source toggle failed", err);
      }
      renderSources();
    }

    function renderFilterOptions() {
      if (!sourceFilter) return;
      const cur = sourceFilter.value;
      sourceFilter.innerHTML = "";
      const all = document.createElement("option");
      all.value = "";
      all.textContent = "全部来源";
      sourceFilter.appendChild(all);
      for (const s of sources) {
        const o = document.createElement("option");
        o.value = s.source_id;
        o.textContent = s.title || s.source_id;
        sourceFilter.appendChild(o);
      }
      sourceFilter.value = cur;
    }

    async function loadKb(q) {
      try {
        const src = sourceFilter ? sourceFilter.value : "";
        let url = "/api/settings/kb?limit=200";
        if (q) url += "&q=" + encodeURIComponent(q);
        if (src) url += "&source=" + encodeURIComponent(src);
        const r = await fetch(url);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const d = await r.json();
        sources = d.sources || [];
        // 来源开关状态来自 config（后端随 sources 一并返回）——首屏据此回填
        if (Array.isArray(d.disabled_sources)) {
          disabled = new Set(d.disabled_sources);
        }
        renderFilterOptions();
        renderSources();
        meta.textContent =
          `知识库 v${d.kb_version} · ${sources.length} 个来源`;
        count.textContent = src
          ? `显示 ${d.returned} / 共 ${d.total_entries} 条`
          : `显示 ${d.returned} / 共 ${d.total_entries} 条（全部来源）`;
        list.innerHTML = "";
        if (!d.entries.length) {
          const p = document.createElement("p");
          p.className = "text-[12px] text-muted-foreground px-3 py-3";
          p.textContent = "无匹配条款";
          list.appendChild(p);
          return;
        }
        // 跨源浏览时标出来源，避免"同名条款号"混淆（多条法规都有第一条）
        const multi = !src && sources.length > 1;
        for (const e of d.entries) {
          const row = document.createElement("details");
          row.className = "px-3 py-2";
          const sum = document.createElement("summary");
          sum.className =
            "cursor-pointer text-[12px] font-medium text-foreground " +
            "hover:text-muted-foreground select-none";
          const tag = multi && e.source_title ? `【${e.source_title}】` : "";
          sum.textContent = `${tag}${e.article_label} · ${e.chapter}`;
          const body = document.createElement("p");
          body.className =
            "text-[12px] leading-relaxed text-muted-foreground mt-1 whitespace-pre-wrap";
          body.textContent = e.text; // textContent：无 XSS 面
          row.appendChild(sum);
          row.appendChild(body);
          list.appendChild(row);
        }
      } catch (err) {
        list.innerHTML = "";
        const p = document.createElement("p");
        p.className = "text-[12px] text-destructive px-3 py-3";
        p.textContent = "知识库加载失败：" + err.message;
        list.appendChild(p);
        log.err("kb panel load failed", err);
      }
    }

    let debounce = null;
    search.addEventListener("input", () => {
      clearTimeout(debounce);
      debounce = setTimeout(() => loadKb(search.value.trim()), 300);
    });
    if (sourceFilter) {
      sourceFilter.addEventListener("change", () => loadKb(search.value.trim()));
    }
    loadKb("");
  }

  // ============================================================
  // OCR 独立保存 — 与飞书/规则保持一致的 per-section save
  // ============================================================
  async function saveOcrConfig() {
    const btn = document.getElementById("ocr-save-btn");
    const msg = document.getElementById("ocr-msg");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "保存中…";
    }
    try {
      const body = { llm_provider: S.state.activeProvider };

      // OCR 后端
      const activeSegBtn = document.querySelector(
        "#ocr-backend-seg button.active",
      );
      if (!activeSegBtn) {
        if (msg) msg.textContent = "✗ 请先选择 OCR 后端";
        return;
      }
      body.ocr_backend = activeSegBtn.dataset.value;

      // PaddleOCR
      const paddleToken = document.getElementById("paddle_ocr_token");
      if (paddleToken && paddleToken.value.trim()) {
        body.paddle_ocr_token = paddleToken.value.trim();
      }
      const paddleUrl = document.getElementById("paddle_ocr_api_url");
      if (paddleUrl) body.paddle_ocr_api_url = paddleUrl.value;
      const paddleModel = document.getElementById("paddle_ocr_model");
      if (paddleModel) body.paddle_ocr_model = paddleModel.value;

      // MinerU
      const mineruToken = document.getElementById("mineru_token");
      if (mineruToken && mineruToken.value.trim()) {
        body.mineru_token = mineruToken.value.trim();
      }
      const mineruBaseUrl = document.getElementById("mineru_base_url");
      if (mineruBaseUrl) body.mineru_base_url = mineruBaseUrl.value.trim();
      const mineruVersion = document.getElementById("mineru_model_version");
      if (mineruVersion) body.mineru_model_version = mineruVersion.value;
      const mineruLang = document.getElementById("mineru_language");
      if (mineruLang) body.mineru_language = mineruLang.value;
      body.mineru_enable_formula = document.getElementById(
        "mineru_enable_formula",
      ).checked;
      body.mineru_enable_table = document.getElementById(
        "mineru_enable_table",
      ).checked;
      const slicesEl = document.getElementById("ocr_slices");
      if (slicesEl) {
        const n = parseInt(slicesEl.value, 10);
        body.ocr_slices = Number.isFinite(n) && n >= 1 ? Math.min(n, 20) : 1;
      }

      // P1 修复：新增 provider 注册随 OCR 保存一并提交 — 否则用户添加
      // provider 后直接点"保存 OCR 设置"，刷新后 provider 丢失且无提示。
      if (S.state.pendingAdds.size > 0) {
        body.llm_providers_add = Array.from(S.state.pendingAdds).join(",");
      }

      log("saving OCR config", Object.keys(body));
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (r.ok && data.ok) {
        if (msg) msg.textContent = "✓ OCR 设置已保存";
        await load();
      } else {
        const errMsg =
          data.detail?.errors?.join("; ") ||
          data.detail ||
          data.message ||
          "保存失败";
        if (msg) msg.textContent = "✗ " + errMsg;
      }
    } catch (err) {
      if (msg) msg.textContent = "✗ " + err.message;
      log.err("OCR save failed", err);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "保存 OCR 设置";
      }
    }
  }

  document
    .getElementById("ocr-save-btn")
    ?.addEventListener("click", saveOcrConfig);

  // ============================================================
  // 加载设置 — 初始渲染
  // ============================================================
  async function load() {
    let r;
    try {
      r = await fetch("/api/settings");
    } catch (err) {
      // 网络层失败：整页留白且无提示是坏体验 — 显式错误态 + 重试指引
      log.err("settings load failed (network)", err);
      S.showMsg(
        "设置加载失败：无法连接后端服务。请确认应用正在运行，然后刷新页面。",
        "err",
      );
      return;
    }
    if (!r.ok) {
      log.err("settings load failed", r.status);
      S.showMsg(`设置加载失败（HTTP ${r.status}），请刷新重试。`, "err");
      return;
    }
    S.state.current = await r.json();
    log("settings loaded", S.state.current);

    S.state.activeProvider = S.state.current.llm.active_provider || S.state.current.llm.provider;

    // 🔴 B4-4：provider 自动改写的**唯一决策点在后端**
    // （`config._resolve_active_provider`，在 import 期就执行完了）。
    //
    // 前端此前**也**自己切一次（改用"占位判定"的结果 `p.configured` 当触发条件，
    // 再 POST /set_active_provider 落盘）—— 两份实现，且：
    //   ① 后端先切完 ⇒ 前端条件恒不成立 ⇒ 那条**唯一有提示**的路径被绕过
    //      ⇒ 用户看到的就是"provider 被静默换掉"；
    //   ② 前端那份的判断依据是启发式（见 `_is_real_key` 注释），
    //      真实 key 含 `placeholder` 子串时会在**生产环境换模型**。
    // 现改为：只呈现后端回传的事实（`current.llm.auto_activated`）。
    const providers = S.state.current.llm.providers || [];
    S.renderProviders(providers, S.state.activeProvider);
    fillOcrForm();
    S.fillFeishuForm();
    await S.loadRules();
    initSectionNav();
    showAutoActivateNotice(S.state.current.llm.auto_activated);
  }

  // 呈现「当前 provider 徽标」+ 后端回传的「provider 自动改写」事实（B4-4 ②）。
  // `info` = {applied, from, to, reason} 或 null（本次启动无事发生）。
  // ⚠️ **本函数只显示，不切换** —— 决策在后端，避免第二实现点。
  // ❗**加载期徽标的唯一写入点**：无通知时也要写回纯名称，否则徽标无人渲染。
  //    （曾放在 fillOcrForm 里 ⇒ 徽标文案依赖"两个函数的调用顺序"这一隐形契约）
  function showAutoActivateNotice(info) {
    const badgeEl = document.getElementById("llm-provider-badge");
    if (badgeEl) {
      badgeEl.textContent =
        info && info.applied && info.to
          ? `${display(S.state.activeProvider)}（已自动从 ${display(info.from)} 切换）`
          : display(S.state.activeProvider);
    }
    if (!info || !info.from) return;
    log("auto-activate notice", info);
    S.showMsg(info.reason, "warn");
  }

  // OCR / 飞书表单填充（从 load() 抽出，便于分序与单测定位）
  function fillOcrForm() {
    // OCR
    setSeg("ocr-backend-seg", S.state.current.ocr.backend);
    showBackendForm(S.state.current.ocr.backend);
    document.getElementById("paddle_ocr_token").placeholder =
      S.state.current.ocr.paddle.token || "访问令牌（可选）";
    document.getElementById("paddle_ocr_api_url").value =
      S.state.current.ocr.paddle.api_url;
    document.getElementById("paddle_ocr_model").value =
      S.state.current.ocr.paddle.model;
    document.getElementById("paddle-status").innerHTML = statusBadge(
      S.state.current.ocr.paddle.configured,
    );
    document.getElementById("mineru_token").placeholder =
      S.state.current.ocr.mineru.token || "sk-...";
    document.getElementById("mineru_base_url").value =
      S.state.current.ocr.mineru.base_url || "";
    document.getElementById("mineru_model_version").value =
      S.state.current.ocr.mineru.model_version;
    document.getElementById("mineru_language").value =
      S.state.current.ocr.mineru.language;
    document.getElementById("mineru_enable_formula").checked =
      S.state.current.ocr.mineru.enable_formula;
    document.getElementById("mineru_enable_table").checked =
      S.state.current.ocr.mineru.enable_table;
    document.getElementById("ocr_slices").value =
      S.state.current.ocr.slices != null ? S.state.current.ocr.slices : 1;
    document.getElementById("mineru-status").innerHTML = statusBadge(
      S.state.current.ocr.mineru.configured,
    );
    document.getElementById("ocr-backend-badge").textContent =
      S.state.current.ocr.backend === "mineru" ? "MinerU" : "PaddleOCR";
  }

  function setSeg(id, value) {
    document.querySelectorAll(`#${id} button`).forEach((b) => {
      const on = b.dataset.value === value;
      b.classList.toggle("active", on);
      b.setAttribute("aria-checked", String(on));
    });
  }

  Object.assign(S, { load, setSeg, showAutoActivateNotice });
})();
