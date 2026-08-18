/* ============================================================
   Settings page — 业界做法重构 (参考 OpenAI/Anthropic/Linear)
   ------------------------------------------------------------
   设计原则:
   1. 已配置 provider = 脱敏只读展示 + 操作按钮 (更换/测试/移除/设为当前)
   2. 未配置 provider = 空白 input + "保存此密钥" 即时保存
   3. active provider 切换 = 立即持久化 (不等底部保存)
   4. 单独 provider 测试连接 = 不混淆 active 状态
   5. 底部"保存通用设置" = 仅 OCR backend / 选项等通用字段
   ============================================================ */
(function () {
  "use strict";

  const log = (...a) =>
    console.log("%c[PBC]", "color:#0ea5e9;font-weight:bold", ...a);
  log.warn = (...a) =>
    console.warn("%c[PBC]", "color:#f59e0b;font-weight:bold", ...a);
  log.err = (...a) =>
    console.error("%c[PBC]", "color:#ef4444;font-weight:bold", ...a);

  // Built-in providers (cannot be removed via UI)
  const BUILTIN = new Set(["deepseek", "siliconflow"]);

  // Provider display name overrides
  const DISPLAY_NAMES = {
    deepseek: "DeepSeek",
    siliconflow: "SiliconFlow",
    glm: "GLM · 智谱",
    kimi: "Kimi · 月之暗面",
    qwen: "Qwen · 通义千问",
    mimo: "MiMo · 小米",
    anthropic: "Anthropic · Claude",
    anthropictest: "Anthropic · Claude",
    openai: "OpenAI",
  };

  let current = {};
  let activeProvider = "";
  const pendingAdds = new Set();
  // 会话内已移除的 provider — 与 pendingAdds 对称，保存时随
  // llm_providers_remove 提交后端做差集合并（P1-3: 移除必须持久化，
  // 否则刷新后 provider 复活，UI 承诺与实际不符）
  const removedProviders = new Set();

  // HTML escape — XSS protection for user-controlled strings
  const esc = (s) =>
    String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  function display(name) {
    return DISPLAY_NAMES[name] || name;
  }

  // OCR 后端显示名（测试连接消息用）
  const OCR_DISPLAY = { mineru: "MinerU", paddle: "PaddleOCR" };
  function ocrDisplay(backend) {
    return OCR_DISPLAY[backend] || backend || "未知";
  }

  function showBackendForm(backend) {
    document.querySelectorAll("[data-backend-form]").forEach((el) => {
      el.classList.toggle("hidden", el.dataset.backendForm !== backend);
    });
  }

  function statusBadge(ok) {
    return ok
      ? '<span class="badge-ok">✓ 已配置</span>'
      : '<span class="badge-no">未配置</span>';
  }

  // ============================================================
  // S5: Provider 卡片渲染 — 已配置=脱敏只读 / 未配置=空白 input
  // ============================================================
  function renderProvider(prov, isActive) {
    const div = document.createElement("div");
    div.className = "provider-row" + (isActive ? " is-active" : "");
    div.dataset.provider = prov.name;
    const isConfigured = prov.configured;

    div.innerHTML = `
      <div class="provider-head">
        <div class="provider-name-row">
          <span class="provider-name">${esc(display(prov.name))}</span>
          ${isActive ? '<span class="provider-active-tag">当前</span>' : ""}
          <span class="provider-status">${statusBadge(prov.configured)}</span>
        </div>
        <div class="provider-actions">
          ${isActive ? "" : `<button type="button" class="provider-use-btn" data-action="use" title="设为当前使用的提供商">设为当前</button>`}
          <button type="button" class="provider-test-btn" data-action="test" title="测试此提供商的连通性">测试</button>
          <button type="button" class="provider-toggle-btn" data-action="toggle">${isConfigured ? "更换密钥" : "展开"}</button>
          ${BUILTIN.has(prov.name) ? "" : `<button type="button" class="provider-remove-btn" data-action="remove" title="移除该提供商">移除</button>`}
        </div>
      </div>
      <div class="provider-body hidden">
        ${renderProviderBody(prov, isConfigured)}
      </div>
      <div class="provider-test-result hidden"></div>
    `;
    return div;
  }

  // 已配置: 显示脱敏 key + base_url/model 只读 + "更换密钥" input (默认隐藏)
  // 未配置: 显示空白 input + 引导文案
  function renderProviderBody(prov, isConfigured) {
    if (isConfigured) {
      // 已配置 — 脱敏只读展示
      return `
        <div class="grid grid-cols-2 gap-3 mt-3">
          <div>
            <label class="field-label">协议</label>
            <select class="input" name="${esc(prov.name)}_protocol">
              <option value="openai" ${prov.protocol === "openai" ? "selected" : ""}>OpenAI 兼容协议</option>
              <option value="anthropic" ${prov.protocol === "anthropic" ? "selected" : ""}>Anthropic 协议</option>
            </select>
          </div>
          <div>
            <label class="field-label">模型</label>
            <input class="input" name="${esc(prov.name)}_model" value="${esc(prov.model)}" />
          </div>
        </div>
        <div class="mt-3">
          <label class="field-label">接口地址（Base URL）</label>
          <input class="input" name="${esc(prov.name)}_base_url" value="${esc(prov.base_url)}" />
        </div>
        <div class="mt-3">
          <label class="field-label">当前 API 密钥</label>
          <div class="key-display">${esc(prov.api_key)} <span class="muted">(已保存)</span></div>
          <div class="mt-2 key-replace-section hidden">
            <label class="field-label">输入新密钥覆盖原值</label>
            <input class="input" type="password" name="${esc(prov.name)}_api_key" placeholder="粘贴新的 API 密钥..." autocomplete="new-password" />
            <div class="mt-2 flex gap-2">
              <button type="button" class="save-key-btn btn-primary-small" data-provider="${esc(prov.name)}">保存</button>
              <button type="button" class="clear-key-btn btn-danger-small" data-provider="${esc(prov.name)}" title="清除已保存的 API 密钥">移除密钥</button>
              <button type="button" class="cancel-replace-btn btn-text">取消</button>
            </div>
          </div>
        </div>
      `;
    }
    // 未配置 — 空白 input 引导
    return `
      <div class="grid grid-cols-2 gap-3 mt-3">
        <div>
          <label class="field-label">协议</label>
          <select class="input" name="${esc(prov.name)}_protocol">
            <option value="openai" ${prov.protocol === "openai" ? "selected" : ""}>OpenAI 兼容协议</option>
            <option value="anthropic" ${prov.protocol === "anthropic" ? "selected" : ""}>Anthropic 协议</option>
          </select>
        </div>
        <div>
          <label class="field-label">模型</label>
          <input class="input" name="${esc(prov.name)}_model" value="${esc(prov.model)}" />
        </div>
      </div>
      <div class="mt-3">
        <label class="field-label">接口地址（Base URL）</label>
        <input class="input" name="${esc(prov.name)}_base_url" value="${esc(prov.base_url)}" />
      </div>
      <div class="mt-3">
        <label class="field-label">API 密钥 <span class="muted">— 粘贴 ${esc(display(prov.name))} 的密钥</span></label>
        <input class="input" type="password" name="${esc(prov.name)}_api_key" placeholder="sk-..." autocomplete="new-password" />
        <div class="mt-2">
          <button type="button" class="save-key-btn btn-primary-small" data-provider="${esc(prov.name)}">保存</button>
        </div>
      </div>
    `;
  }

  function renderProviders(providers, active) {
    const list = document.getElementById("llm-providers-list");
    list.innerHTML = "";
    // Active provider first, then others sorted by name
    const sorted = [...providers].sort((a, b) => {
      if (a.name === active) return -1;
      if (b.name === active) return 1;
      return a.name.localeCompare(b.name);
    });
    for (const prov of sorted) {
      list.appendChild(renderProvider(prov, prov.name === active));
    }
    bindProviderActions();
  }

  // ============================================================
  // Provider 操作事件绑定 (事件委托)
  // ============================================================
  function bindProviderActions() {
    // 防重复绑定：renderProviders 每次调用（load / 切换 / 保存密钥 /
    // 添加 provider 后）都会走到这里，直接 addEventListener 会叠加 N 个
    // handler → 第二次交互起按钮"点了没反应"或双请求。用克隆替换丢弃
    // 旧节点上的 listener（子元素全部走委托、无直接绑定，克隆安全）。
    let list = document.getElementById("llm-providers-list");
    if (!list) return;
    const fresh = list.cloneNode(true);
    list.replaceWith(fresh);
    list = fresh;

    // Use event delegation to handle all clicks via data-action
    list.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-action]");
      if (!btn) return;
      e.stopPropagation();
      const row = btn.closest(".provider-row");
      if (!row) return;
      const providerName = row.dataset.provider;
      const action = btn.dataset.action;

      switch (action) {
        case "use":
          await setActiveProvider(providerName);
          break;
        case "test":
          await testProvider(providerName, row);
          break;
        case "toggle":
          toggleProviderBody(row, btn);
          break;
        case "remove":
          await removeProvider(providerName, row);
          break;
      }
    });

    // Per-provider "保存此密钥" + "移除密钥" + "取消"
    list.addEventListener("click", async (e) => {
      const saveBtn = e.target.closest(".save-key-btn");
      const clearBtn = e.target.closest(".clear-key-btn");
      const cancelBtn = e.target.closest(".cancel-replace-btn");
      if (!saveBtn && !clearBtn && !cancelBtn) return;
      e.stopPropagation();
      const row = e.target.closest(".provider-row");
      if (!row) return;
      const providerName = row.dataset.provider;

      if (saveBtn) {
        await saveProviderConfig(providerName, row);
      } else if (clearBtn) {
        await clearProviderKey(providerName, row);
      } else if (cancelBtn) {
        // 取消更换密钥: 隐藏 input 区域
        const replaceSection = row.querySelector(".key-replace-section");
        if (replaceSection) replaceSection.classList.add("hidden");
      }
    });
  }

  // ============================================================
  // S1: setActiveProvider — 立即保存到后端 (业界做法)
  // opts.silent: 不显示自己的 "已切换" 消息（由调用方负责提示）
  // opts.autoReason: auto-activated 时的文案后缀
  // ============================================================
  async function setActiveProvider(name, opts = {}) {
    log("switching active provider", { from: activeProvider, to: name });
    const badgeEl = document.getElementById("llm-provider-badge");
    if (badgeEl) badgeEl.textContent = display(name);

    try {
      const r = await fetch("/api/settings/set_active_provider", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: name }),
      });
      const data = await r.json();
      if (r.ok && data.ok) {
        activeProvider = name;
        // 用后端返回的最新 providers 列表刷新本地缓存（configured 标志等）
        if (Array.isArray(data.providers) && data.providers.length) {
          current.llm.providers = data.providers;
        }
        if (!opts.silent) {
          const suffix = opts.autoReason ? `（${opts.autoReason}）` : "（立即生效）";
          showMsg(`✓ 已切换到 ${display(name)}${suffix}`, "info");
        }
        // 重新渲染列表: active 排首位
        renderProviders(current.llm.providers || [], activeProvider);
        log("active provider switched live", { active: activeProvider });
      } else {
        showMsg(`✗ 切换失败: ${data.detail || data.message || "未知错误"}`, "err");
      }
    } catch (err) {
      showMsg(`✗ 切换失败: ${err.message}`, "err");
      log.err("set active provider failed", err);
    }
  }

  // ============================================================
  // toggleProviderBody — "更换密钥"/"展开" 按钮
  // ============================================================
  function toggleProviderBody(row, btn) {
    const body = row.querySelector(".provider-body");
    const replaceSection = row.querySelector(".key-replace-section");
    if (body.classList.contains("hidden")) {
      body.classList.remove("hidden");
      btn.textContent = "折叠";
      // 已配置的 provider: 自动展开"更换密钥"输入区
      if (replaceSection) replaceSection.classList.remove("hidden");
    } else {
      body.classList.add("hidden");
      btn.textContent = row.querySelector(".key-display") ? "更换密钥" : "展开";
      if (replaceSection) replaceSection.classList.add("hidden");
    }
  }

  // ============================================================
  // S5: saveProviderConfig — 单 provider 保存全字段 (立即生效)
  // T2.1 统一保存语义：Key + 协议 + Base URL + 模型 一次性提交。
  // 此前"保存此密钥"只存 Key，而底部"保存全部设置"跳过 api_key —
  // 两条路径各管一半，用户改 base_url 后点保存密钥 会静默丢失。
  // ============================================================
  async function saveProviderConfig(providerName, row) {
    const input = row.querySelector(`input[name="${CSS.escape(providerName)}_api_key"]`);
    if (!input) return;
    const keyValue = input.value.trim();
    if (!keyValue) {
      showMsg("请输入 API 密钥", "warn");
      input.focus();
      return;
    }
    if (keyValue === "__CLEAR__") {
      showMsg("密钥不能为 __CLEAR__ 保留字", "err");
      return;
    }

    const btn = row.querySelector(".save-key-btn");
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "保存中…";

    try {
      const body = {
        llm_provider: activeProvider,
        [`${providerName}_api_key`]: keyValue,
      };
      // 同卡片内的协议 / 模型 / Base URL 一并保存（非空值才提交，
      // 空值保持后端现有值 —— 与底部保存的空值跳过语义一致）
      for (const suffix of ["_protocol", "_base_url", "_model"]) {
        const el = row.querySelector(
          `[name="${CSS.escape(providerName + suffix)}"]`,
        );
        if (el && el.value.trim() !== "") {
          body[`${providerName}${suffix}`] = el.value.trim();
        }
      }
      // 若该 provider 是本次会话刚添加、尚未保存注册表的（pendingAdds），
      // 必须同批提交 llm_providers_add — 否则后端写 Key 时注册表里没有它，
      // 随后的自动激活 404 "not in registry"，刷新后 provider 消失
      // （对抗审查：此前"保存"对新 provider 必然失败）。
      if (pendingAdds.has(providerName)) {
        body.llm_providers_add = providerName;
      }
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (r.ok && data.ok) {
        pendingAdds.delete(providerName); // 已注册成功，后续保存不再带 add
        const skippedNote = data.skipped?.length
          ? `（未修改: ${data.skipped.join("、")}）`
          : "";
        // Auto-activate (业界做法 — OpenAI/Anthropic/Linear):
        // 若当前 active provider 未配置 Key，保存新 provider 的 Key 后自动切到它。
        // 根除"配置了 SiliconFlow 但测试报 deepseek 未配置 Key"的死亡陷阱。
        const activeProv = (current.llm.providers || []).find(
          (p) => p.name === activeProvider,
        );
        const activeUnconfigured = !(activeProv && activeProv.configured);
        if (providerName !== activeProvider && activeUnconfigured) {
          showMsg(
            `✓ ${display(providerName)} 的密钥已保存，并自动设为当前提供商${skippedNote}`,
            "info",
          );
          await setActiveProvider(providerName, { silent: true });
        } else {
          showMsg(
            `✓ ${display(providerName)} 的密钥已保存并立即生效${skippedNote}`,
            "info",
          );
          // 用后端返回的 providers 列表刷新（避免 stale configured 标志）
          if (Array.isArray(data.providers) && data.providers.length) {
            current.llm.providers = data.providers;
          }
          renderProviders(current.llm.providers || [], activeProvider);
        }
      } else {
        const errMsg = data.detail?.errors?.join("; ") || data.detail || data.message || "保存失败";
        showMsg(`✗ ${errMsg}`, "err");
      }
    } catch (err) {
      showMsg(`✗ 保存失败: ${err.message}`, "err");
      log.err("save provider key failed", err);
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  // ============================================================
  // S5: clearProviderKey — 单 provider 清除 Key (立即生效)
  // ============================================================
  async function clearProviderKey(providerName, row) {
    const ok = await window.PBC.confirmDialog({
      title: `确认清除 ${display(providerName)} 的 API 密钥？`,
      message: "Key 将从配置文件中删除，立即生效。",
      confirmText: "确认清除",
      cancelText: "取消",
      danger: true,
    });
    if (!ok) return;

    try {
      const body = {
        llm_provider: activeProvider,
        [`${providerName}_clear_key`]: true,
      };
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (r.ok && data.ok) {
        showMsg(`✓ ${display(providerName)} 的密钥已清除`, "info");
        await load();
      } else {
        showMsg(`✗ 清除失败: ${data.detail || data.message}`, "err");
      }
    } catch (err) {
      showMsg(`✗ 清除失败: ${err.message}`, "err");
      log.err("clear provider key failed", err);
    }
  }

  // ============================================================
  // S4+S8: testProvider — 单独测试指定 provider
  // ============================================================
  async function testProvider(providerName, row) {
    const resultEl = row.querySelector(".provider-test-result");
    if (!resultEl) return;

    resultEl.classList.remove("hidden");
    resultEl.innerHTML = `<span class="muted">检测中…</span>`;

    try {
      const r = await fetch("/api/settings/test_provider", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: providerName }),
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok && data.ok) {
        const latency = data.latency_ms ? ` ${data.latency_ms}ms` : "";
        resultEl.innerHTML = `<span class="badge-ok">✓ 连通正常${latency} · 模型：${esc(data.model || "")}</span>`;
      } else {
        // 非 200 (如 403 Forbidden) 或 ok=false — 优先显示后端 reason，其次 detail
        const reason = data.reason || data.detail || `HTTP ${r.status}`;
        if (reason.includes("not configured") || reason.includes("API key")) {
          resultEl.innerHTML = `<span class="badge-no">✗ 未配置 API 密钥 — 请点击"更换密钥"或"展开"输入</span>`;
        } else {
          resultEl.innerHTML = `<span class="badge-no">✗ ${esc(reason)}</span>`;
        }
      }
    } catch (err) {
      resultEl.innerHTML = `<span class="badge-no">✗ 请求失败: ${esc(err.message)}</span>`;
      log.err("test provider failed", err);
    }
  }

  // ============================================================
  // removeProvider — 标记移除（保存通用设置时经 llm_providers_remove 持久化；
  // 若移除的是本次会话刚添加的 provider，须同时从 pendingAdds 撤销）
  // ============================================================
  async function removeProvider(name, row) {
    const ok = await window.PBC.confirmDialog({
      title: `确认移除 ${display(name)} 提供商？`,
      message: "保存后将从注册表中移除该提供商。",
      confirmText: "确认",
      cancelText: "取消",
      danger: true,
    });
    if (!ok) return;
    row.remove();
    current.llm.providers = (current.llm.providers || []).filter(p => p.name !== name);
    removedProviders.add(name);
    pendingAdds.delete(name); // 刚添加未保存即移除：撤销 pendingAdd，防止保存时复活
    if (activeProvider === name) {
      const firstRow = document.querySelector(".provider-row");
      if (firstRow) await setActiveProvider(firstRow.dataset.provider);
    }
  }

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
        n.querySelectorAll(".settings-nav-link").forEach((l) =>
          l.classList.toggle("active", l.dataset.target === target),
        ),
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
      const body = { llm_provider: activeProvider };

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
      if (pendingAdds.size > 0) {
        body.llm_providers_add = Array.from(pendingAdds).join(",");
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
      showMsg(
        "设置加载失败：无法连接后端服务。请确认应用正在运行，然后刷新页面。",
        "err",
      );
      return;
    }
    if (!r.ok) {
      log.err("settings load failed", r.status);
      showMsg(`设置加载失败（HTTP ${r.status}），请刷新重试。`, "err");
      return;
    }
    current = await r.json();
    log("settings loaded", current);

    activeProvider = current.llm.active_provider || current.llm.provider;

    // Auto-activate 迁移（修复存量配置的死亡陷阱）：
    // 若当前 active provider 未配置 Key，但另一个 provider 已配置 Key，
    // 自动切换到第一个已配置的 provider（持久化 + 热更新）。
    // 场景：用户之前保存了 SiliconFlow Key，但 active 仍是默认 deepseek（无 Key），
    // 导致测试连接报 "API key not configured"。此处静默修复，无需用户手动操作。
    const providers = current.llm.providers || [];
    const activeProv = providers.find((p) => p.name === activeProvider);
    if (activeProv && !activeProv.configured) {
      const firstConfigured = providers.find(
        (p) => p.configured && p.name !== activeProvider,
      );
      if (firstConfigured) {
        log(
          "auto-activating first configured provider (active is unconfigured)",
          { from: activeProvider, to: firstConfigured.name },
        );
        // 先渲染，再静默切换（切换会重新渲染）
        renderProviders(providers, activeProvider);
        await setActiveProvider(firstConfigured.name, {
          silent: false,
          autoReason: `${display(activeProvider)} 未配置，已自动切换`,
        });
        // setActiveProvider 已重新渲染 + 更新 badge，跳过下面的重复渲染
        // 但仍需填充 OCR 表单
        fillOcrForm();
        fillFeishuForm();
        await loadRules();
        initSectionNav();
        return;
      }
    }

    renderProviders(providers, activeProvider);
    fillOcrForm();
    fillFeishuForm();
    await loadRules();
    initSectionNav();
  }

  // OCR 表单填充（从 load() 抽出，auto-activate 路径也复用）
  function fillOcrForm() {
    const badgeEl = document.getElementById("llm-provider-badge");
    if (badgeEl) badgeEl.textContent = display(activeProvider);

    // OCR
    setSeg("ocr-backend-seg", current.ocr.backend);
    showBackendForm(current.ocr.backend);
    document.getElementById("paddle_ocr_token").placeholder =
      current.ocr.paddle.token || "访问令牌（可选）";
    document.getElementById("paddle_ocr_api_url").value =
      current.ocr.paddle.api_url;
    document.getElementById("paddle_ocr_model").value =
      current.ocr.paddle.model;
    document.getElementById("paddle-status").innerHTML = statusBadge(
      current.ocr.paddle.configured,
    );
    document.getElementById("mineru_token").placeholder =
      current.ocr.mineru.token || "sk-...";
    document.getElementById("mineru_base_url").value =
      current.ocr.mineru.base_url || "";
    document.getElementById("mineru_model_version").value =
      current.ocr.mineru.model_version;
    document.getElementById("mineru_language").value =
      current.ocr.mineru.language;
    document.getElementById("mineru_enable_formula").checked =
      current.ocr.mineru.enable_formula;
    document.getElementById("mineru_enable_table").checked =
      current.ocr.mineru.enable_table;
    document.getElementById("ocr_slices").value =
      current.ocr.slices != null ? current.ocr.slices : 1;
    document.getElementById("mineru-status").innerHTML = statusBadge(
      current.ocr.mineru.configured,
    );
    document.getElementById("ocr-backend-badge").textContent =
      current.ocr.backend === "mineru" ? "MinerU" : "PaddleOCR";
  }

  function setSeg(id, value) {
    document.querySelectorAll(`#${id} button`).forEach((b) => {
      b.classList.toggle("active", b.dataset.value === value);
    });
  }

  // ============================================================
  // 飞书通知 — 群机器人 Webhook（任务完成/出错推送）
  // ============================================================
  const FEISHU_EVENT_OPTIONS = [
    { value: "review", label: "分析完成" },
    { value: "partial_review", label: "部分完成" },
    { value: "error", label: "处理失败" },
    { value: "cancelled", label: "已取消" },
  ];

  function applyFeishuModeUI(mode) {
    const modeEl = document.getElementById("feishu-mode");
    if (modeEl) modeEl.value = mode || "app_bot";
    const appBot = document.querySelectorAll(".feishu-app-bot");
    const webhook = document.querySelectorAll(".feishu-webhook");
    appBot.forEach((el) => (el.style.display = mode === "app_bot" ? "" : "none"));
    webhook.forEach((el) => (el.style.display = mode === "webhook" ? "" : "none"));
  }

  function fillFeishuForm() {
    const f = current.feishu || {};
    const enabledEl = document.getElementById("feishu-enabled");
    const urlEl = document.getElementById("feishu-url");
    const secretEl = document.getElementById("feishu-secret");
    const appIdEl = document.getElementById("feishu-app-id");
    const appSecretEl = document.getElementById("feishu-app-secret");
    const openIdEl = document.getElementById("feishu-open-id");
    const mobileEl = document.getElementById("feishu-mobile");
    if (enabledEl) enabledEl.checked = !!f.enabled;
    applyFeishuModeUI(f.mode);
    if (urlEl) {
      urlEl.value = "";
      urlEl.placeholder = f.webhook_url || "https://open.feishu.cn/open-apis/bot/v2/hook/…";
    }
    if (secretEl) {
      secretEl.value = "";
      secretEl.placeholder = f.secret
        ? `${f.secret}（已保存，留空保持不变）`
        : "群机器人安全设置中的加签密钥";
    }
    if (appIdEl) appIdEl.value = f.app_id || "";
    if (appSecretEl) {
      appSecretEl.value = "";
      appSecretEl.placeholder = f.app_secret
        ? `${f.app_secret}（已保存，留空保持不变）`
        : "开发者后台「凭证与基础信息」";
    }
    if (openIdEl) openIdEl.value = f.open_id || "";
    if (mobileEl) mobileEl.value = f.mobile || "";
    const eventsEl = document.getElementById("feishu-events");
    if (!eventsEl) return;
    eventsEl.innerHTML = "";
    const selected = new Set(f.events || []);
    FEISHU_EVENT_OPTIONS.forEach((opt) => {
      const label = document.createElement("label");
      label.className =
        "flex items-center gap-1.5 px-2 py-1 text-[12px] border border-border rounded-md cursor-pointer hover:bg-muted/40";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = opt.value;
      cb.checked = selected.has(opt.value);
      cb.className = "w-3.5 h-3.5 accent-foreground cursor-pointer";
      cb.dataset.feishuEvent = opt.value;
      label.appendChild(cb);
      label.appendChild(document.createTextNode(opt.label));
      eventsEl.appendChild(label);
    });
  }

  function feishuSelectedEvents() {
    return Array.from(
      document.querySelectorAll('input[data-feishu-event]:checked'),
    ).map((el) => el.value);
  }

  function feishuMsg(text, kind = "info") {
    const msg = document.getElementById("feishu-msg");
    if (!msg) return;
    msg.textContent = text;
    msg.className = "text-[12px] " + (kind === "err" ? "text-destructive" : "text-muted-foreground");
  }

  async function saveFeishu() {
    const modeEl = document.getElementById("feishu-mode");
    const urlEl = document.getElementById("feishu-url");
    const secretEl = document.getElementById("feishu-secret");
    const appIdEl = document.getElementById("feishu-app-id");
    const appSecretEl = document.getElementById("feishu-app-secret");
    const openIdEl = document.getElementById("feishu-open-id");
    const mobileEl = document.getElementById("feishu-mobile");
    const body = {
      feishu_enabled: document.getElementById("feishu-enabled").checked,
      feishu_mode: modeEl ? modeEl.value : "app_bot",
      feishu_events: feishuSelectedEvents().join(","),
    };
    // 掩码/空白输入不回写（保持已保存的值）
    if (urlEl && urlEl.value.trim() && !urlEl.value.includes("…")) {
      body.feishu_webhook_url = urlEl.value.trim();
    }
    if (secretEl && secretEl.value.trim()) {
      body.feishu_secret = secretEl.value.trim();
    }
    if (appIdEl && appIdEl.value.trim()) {
      body.feishu_app_id = appIdEl.value.trim();
    }
    if (appSecretEl && appSecretEl.value.trim() && !appSecretEl.value.includes("…")) {
      body.feishu_app_secret = appSecretEl.value.trim();
    }
    if (openIdEl && openIdEl.value.trim()) {
      body.feishu_open_id = openIdEl.value.trim();
    }
    if (mobileEl && mobileEl.value.trim()) {
      body.feishu_mobile = mobileEl.value.trim();
    }
    // P1 修复：新增 provider 注册随飞书保存一并提交（同 OCR 保存）
    if (pendingAdds.size > 0) {
      body.llm_providers_add = Array.from(pendingAdds).join(",");
    }
    try {
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (!r.ok) {
        feishuMsg(`✗ 保存失败: ${JSON.stringify(data.detail || data)}`, "err");
        return;
      }
      // P2 修复：掩码值被跳过（未修改）时后端会回显 skipped —— 此前被
      // 丢弃，用户把掩码复制回去提交会看到"已保存"但实际未变。
      const skippedNote = data.skipped?.length
        ? `（未修改: ${data.skipped.join("、")}）`
        : "";
      feishuMsg(`✓ 已保存（${data.updated} 项）${skippedNote}`);
      await load();
    } catch (err) {
      feishuMsg(`✗ 保存失败: ${err.message}`, "err");
      log.err("feishu save failed", err);
    }
  }

  async function testFeishu() {
    const modeEl = document.getElementById("feishu-mode");
    const urlEl = document.getElementById("feishu-url");
    const secretEl = document.getElementById("feishu-secret");
    const appIdEl = document.getElementById("feishu-app-id");
    const appSecretEl = document.getElementById("feishu-app-secret");
    const openIdEl = document.getElementById("feishu-open-id");
    const mobileEl = document.getElementById("feishu-mobile");
    const btn = document.getElementById("feishu-test-btn");
    if (!btn) return;
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "发送中…";
    const body = { mode: modeEl ? modeEl.value : "app_bot" };
    if (urlEl && urlEl.value.trim() && !urlEl.value.includes("…")) {
      body.webhook_url = urlEl.value.trim();
    }
    if (secretEl && secretEl.value.trim()) {
      body.secret = secretEl.value.trim();
    }
    if (appIdEl && appIdEl.value.trim()) body.app_id = appIdEl.value.trim();
    if (appSecretEl && appSecretEl.value.trim() && !appSecretEl.value.includes("…")) {
      body.app_secret = appSecretEl.value.trim();
    }
    if (openIdEl && openIdEl.value.trim()) body.open_id = openIdEl.value.trim();
    if (mobileEl && mobileEl.value.trim()) body.mobile = mobileEl.value.trim();
    try {
      const r = await fetch("/api/settings/test_feishu", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (data.ok) {
        feishuMsg(
          body.mode === "app_bot"
            ? "✓ 测试消息已发送，请查看飞书私聊"
            : "✓ 测试消息已发送，请查看飞书群",
          "info",
        );
      } else {
        feishuMsg(`✗ ${data.reason || "发送失败"}`, "err");
      }
    } catch (err) {
      feishuMsg(`✗ 测试失败: ${err.message}`, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  const feishuModeEl = document.getElementById("feishu-mode");
  if (feishuModeEl) feishuModeEl.addEventListener("change", () => applyFeishuModeUI(feishuModeEl.value));
  const feishuSaveBtn = document.getElementById("feishu-save-btn");
  if (feishuSaveBtn) feishuSaveBtn.addEventListener("click", saveFeishu);
  const feishuTestBtn = document.getElementById("feishu-test-btn");
  if (feishuTestBtn) feishuTestBtn.addEventListener("click", testFeishu);

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
        "ml-1.5 px-1.5 py-0.5 rounded bg-destructive/10 text-[10px] font-normal" +
        (ruleLastSaved === "刚刚" ? " text-foreground" : " text-destructive");
    } else {
      el.textContent = `上次保存 ${ruleLastSaved}  · 命中 ${rules.reduce(
        (n, r) => n + (ruleHits[r.id] || 0),
        0
      )} 次`;
      el.className =
        "ml-1.5 px-1.5 py-0.5 rounded bg-muted text-muted-foreground text-[10px] font-normal";
    }
    void el;
  }

  function renderRules() {
    const listEl = document.getElementById("rules-list");
    const countEl = document.getElementById("rules-count");
    renderRuleSavedBadge();
    if (!listEl) return;
    listEl.innerHTML = "";
    if (countEl) countEl.textContent = `${rules.length} 条`;
    if (rules.length === 0) {
      const empty = document.createElement("p");
      empty.className = "text-[12px] text-muted-foreground/50";
      empty.textContent = "暂无自定义规则 — 跨页分析仅使用内置规则 R1-R8 与 LLM 语义检查";
      listEl.appendChild(empty);
      return;
    }
    rules.forEach((rule, idx) => {
      const row = document.createElement("div");
      row.className = "flex items-start gap-2";
      row.dataset.ruleIndex = String(idx);

      const num = document.createElement("span");
      num.className = "mt-2.5 w-5 shrink-0 text-right text-[11px] text-muted-foreground/60";
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
        "mt-2 shrink-0 px-1.5 py-0.5 rounded text-[10px] " +
        (hit > 0 ? "bg-foreground text-background" : "bg-muted text-muted-foreground/60");
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

  // 模板库 dropdown — 分组展示内置合规规则模板，点选一键添加
  function renderTemplatePanel() {
    const panel = document.getElementById("rule-template-panel");
    if (!panel) return;
    panel.innerHTML = "";
    RULE_TEMPLATE_LIBRARY.forEach((section) => {
      const group = document.createElement("div");
      group.className =
        "px-2 pt-2 pb-1 text-[10px] font-medium text-muted-foreground/60 uppercase tracking-wide";
      group.textContent = section.group;
      panel.appendChild(group);
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
        panel.appendChild(item);
      });
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

  document.addEventListener("click", (e) => {
    const wrap = document.getElementById("rule-template-wrap");
    if (wrap && !wrap.contains(e.target)) toggleTemplatePanel(false);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") toggleTemplatePanel(false);
  });

  document.getElementById("rule-save-btn")?.addEventListener("click", saveRules);

  // ============================================================
  // OCR backend segment switch
  // ============================================================
  document.getElementById("ocr-backend-seg").addEventListener("click", (e) => {
    if (e.target.dataset.value) {
      setSeg("ocr-backend-seg", e.target.dataset.value);
      showBackendForm(e.target.dataset.value);
      document.getElementById("ocr-backend-badge").textContent =
        e.target.dataset.value === "mineru" ? "MinerU" : "PaddleOCR";
    }
  });

  // ============================================================
  // S6: OCR token 清除按钮 (PaddleOCR + MinerU) — 立即生效
  // ============================================================
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".ocr-clear-btn");
    if (!btn) return;
    const target = btn.dataset.target; // "paddle_ocr_token" or "mineru_token"
    const ok = await window.PBC.confirmDialog({
      title: "确认清除 OCR Token？",
      message: "Token 将从配置文件中删除，立即生效。",
      confirmText: "确认清除",
      cancelText: "取消",
      danger: true,
    });
    if (!ok) return;
    try {
      // 发送 __CLEAR__ 标记，后端识别后写入空字符串
      const body = { llm_provider: activeProvider, [target]: "__CLEAR__" };
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (r.ok && data.ok) {
        showMsg(`✓ OCR Token 已清除（立即生效）`, "info");
        await load();
      } else {
        showMsg(`✗ 清除失败: ${data.detail || data.message}`, "err");
      }
    } catch (err) {
      showMsg(`✗ 清除失败: ${err.message}`, "err");
      log.err("OCR clear failed", err);
    }
  });

  // ============================================================
  // Add provider — toggle form visibility
  // ============================================================
  document.getElementById("llm-add-toggle").addEventListener("click", () => {
    document.getElementById("llm-add-form").classList.remove("hidden");
    document.getElementById("llm-add-toggle").classList.add("hidden");
    document.getElementById("llm-add-select").focus();
  });

  document.getElementById("llm-add-cancel").addEventListener("click", () => {
    document.getElementById("llm-add-form").classList.add("hidden");
    document.getElementById("llm-add-toggle").classList.remove("hidden");
    document.getElementById("llm-add-select").value = "";
    document.getElementById("llm-add-custom").value = "";
    document.getElementById("llm-add-custom").classList.add("hidden");
  });

  document.getElementById("llm-add-select").addEventListener("change", (e) => {
    const custom = document.getElementById("llm-add-custom");
    custom.classList.toggle("hidden", e.target.value !== "__custom");
  });

  document.getElementById("llm-add-btn").addEventListener("click", () => {
    const select = document.getElementById("llm-add-select");
    const custom = document.getElementById("llm-add-custom");
    let name = select.value;
    if (name === "__custom") name = custom.value.trim().toLowerCase();
    if (!name) {
      showMsg("请选择或输入提供商名称", "warn");
      return;
    }
    if (!/^[a-z0-9_-]{2,32}$/.test(name)) {
      showMsg("名称格式：小写字母、数字、_ 或 -（2-32 字符）", "err");
      return;
    }
    const existing = document.querySelector(
      `.provider-row[data-provider="${CSS.escape(name)}"]`,
    );
    if (existing) {
      showMsg(`${display(name)} 已存在`, "warn");
      return;
    }

    const defaultProtocol =
      name === "anthropic" || name === "anthropictest" ? "anthropic" : "openai";
    const defaultBaseUrls = {
      glm: "https://open.bigmodel.cn/api/paas/v4",
      kimi: "https://api.moonshot.cn/v1",
      qwen: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      mimo: "https://api.mimo.xiaomi.com/v1",
      anthropic: "https://api.anthropic.com",
      openai: "https://api.openai.com/v1",
    };
    const defaultModels = {
      glm: "glm-4-plus",
      kimi: "moonshot-v1-32k",
      qwen: "qwen-plus",
      mimo: "mimo-7b",
      anthropic: "claude-3-5-sonnet-20241022",
      openai: "gpt-4o-mini",
    };
    const newProv = {
      name,
      protocol: defaultProtocol,
      api_key: "",
      base_url: defaultBaseUrls[name] || "",
      model: defaultModels[name] || "",
      configured: false,
    };
    current.llm.providers = current.llm.providers || [];
    current.llm.providers.push(newProv);
    pendingAdds.add(name);
    // 对称性修复（对抗审查）：移除后重新添加同一 provider 时，若不清掉
    // removedProviders 里的名字，保存时 add+remove 同批提交相互抵消，
    // provider 会静默消失（等于没添加）。
    removedProviders.delete(name);
    renderProviders(current.llm.providers, activeProvider);
    // Reset form
    select.value = "";
    custom.value = "";
    custom.classList.add("hidden");
    document.getElementById("llm-add-form").classList.add("hidden");
    document.getElementById("llm-add-toggle").classList.remove("hidden");
    showMsg(`已添加 ${display(name)} — 请填写字段并保存密钥`, "info");
  });

  // ============================================================
  // S7: 底部"保存全部设置" — 保存 OCR backend / enable_* 等通用字段
  // (per-provider Key 由卡片内"保存"按钮即时保存，此处仅做遗漏提醒)
  // ============================================================
  document
    .getElementById("settings-form")
    .addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const body = {};

      // 收集所有非空字段(排除 per-provider api_key — 卡片内独立保存)
      const unsavedKeys = [];
      for (const [k, v] of fd.entries()) {
        if (v === "") continue;
        if (k.endsWith("_api_key")) {
          // T2.1: 非空填写的 Key 若未被独立保存，显式提醒（旧行为静默丢弃）
          // 已掩码值（形如 abcd****）— 正则覆盖 %-decoded / 原始两种形态
          const masked = /^[\w-]{4}\*{4}/.test(v) || /^.{4}\*{4}$/.test(v);
          if (!masked) {
            const prov = k.replace(/_api_key$/, "");
            unsavedKeys.push(prov);
          }
          continue;
        }
        body[k] = v;
      }
      if (unsavedKeys.length > 0) {
        showMsg(
          `⚠ 检测到 ${unsavedKeys.join("、")} 已填写但未保存的 API 密钥，` +
            "请点击对应卡片内的「保存」按钮（通用设置已保存）",
          "warn",
        );
      }

      body.llm_provider = activeProvider;
      if (pendingAdds.size > 0)
        body.llm_providers_add = Array.from(pendingAdds).join(",");
      if (removedProviders.size > 0)
        body.llm_providers_remove = Array.from(removedProviders).join(",");

      // OCR 通用设置
      const activeSegBtn = document.querySelector(
        "#ocr-backend-seg button.active",
      );
      if (!activeSegBtn) {
        showMsg("请先选择 OCR 后端", "err");
        return;
      }
      body.ocr_backend = activeSegBtn.dataset.value;
      body.mineru_enable_formula = document.getElementById(
        "mineru_enable_formula",
      ).checked;
      body.mineru_enable_table = document.getElementById(
        "mineru_enable_table",
      ).checked;
      const slicesEl = document.getElementById("ocr_slices");
      if (slicesEl) {
        const n = parseInt(slicesEl.value, 10);
        body.ocr_slices = Number.isFinite(n) && n >= 1 ? n : 1;
      }

      log("saving general settings", Object.keys(body));
      showMsg("保存中…", "info");
      try {
        const r = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await r.json();
        if (r.ok && data.ok) {
          showMsg("✓ " + data.message, "info");
          log("save ok", data);
          pendingAdds.clear();
          removedProviders.clear();
          // 重新加载所有状态
          setTimeout(() => load(), 500);
        } else {
          const errMsg =
            data.detail?.errors?.join("; ") ||
            data.detail ||
            data.message ||
            "保存失败";
          throw new Error(errMsg);
        }
      } catch (err) {
        showMsg(`✗ ${err.message}`, "err");
        log.err("save failed", err);
      }
    });

  function showMsg(text, level) {
    const msg = document.getElementById("save-msg");
    msg.textContent = text;
    // P1-12: 硬编码 HSL → 状态令牌（此前 success 用 35% 明度，与
    // --success（45%）漂移；颜色统一从 design-tokens 取值）
    msg.style.color =
      level === "err"
        ? "hsl(var(--destructive))"
        : level === "warn"
          ? "hsl(var(--warning))"
          : level === "info"
            ? "hsl(var(--success))"
            : "hsl(var(--muted-foreground))";
  }

  // ============================================================
  // 底部"测试连接"按钮 — 并行测试 OCR + 所有 LLM provider
  // (不再只测 active；逐个显示结果，避免"配了 SiliconFlow 却报
  //  deepseek 未配置 Key"的死亡陷阱)
  // ============================================================
  document
    .getElementById("test-conn-btn")
    ?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      const originalText = btn.textContent;
      btn.disabled = true;
      btn.textContent = "检测中…";
      const providers = current.llm.providers || [];
      showMsg(`正在检测 OCR + ${providers.length} 个 LLM 提供商…`, "info");
      try {
        // 并行: downstream (用于 OCR) + 每个 provider 的 test_provider
        // P2 修复：health 探测失败（网络异常/后端 500）不应拖垮整个结果 —
        // 单项失败降级为 ✗ 条目，已完成的其他 provider 结果保留。
        const [healthData, ...providerTests] = await Promise.all([
          fetch("/api/health/downstream")
            .then((r) => r.json())
            .catch((e) => ({
              ok: false,
              ocr: { ok: false, reason: `探测请求失败: ${e.message}` },
            })),
          ...providers.map(async (p) => {
            const r = await fetch("/api/settings/test_provider", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ provider: p.name }),
            });
            const data = await r.json().catch(() => ({}));
            return { name: p.name, ok: r.ok && data.ok, data };
          }),
        ]);
        log("health probe result", healthData);
        log("provider test results", providerTests);

        const parts = [];
        // OCR
        const ocr = healthData.ocr || {};
        const ocrLat = ocr.latency_ms ? ` ${ocr.latency_ms}ms` : "";
        if (ocr.ok) {
          parts.push(`✓ OCR(${ocrDisplay(healthData.ocr_backend)})${ocrLat}`);
        } else {
          parts.push(`✗ OCR(${ocrDisplay(healthData.ocr_backend)}): ${ocr.reason || "失败"}`);
        }
        // LLM providers: 已配置/连通的排前
        const sorted = [...providerTests].sort((a, b) => {
          const ac = a.ok ? 0 : 1;
          const bc = b.ok ? 0 : 1;
          return ac - bc;
        });
        let llmAnyOk = false;
        for (const { name, ok, data } of sorted) {
          const tag = name === activeProvider ? "·当前" : "";
          if (ok) {
            llmAnyOk = true;
            const lat = data.latency_ms ? ` ${data.latency_ms}ms` : "";
            parts.push(`✓ ${display(name)}${lat}${tag}`);
          } else {
            const reason = data.reason || data.detail || "失败";
if (reason.includes("未配置") || reason.includes("密钥")) {
              parts.push(`○ ${display(name)}：未配置${tag}`);
            } else {
              parts.push(`✗ ${display(name)}:${reason}${tag}`);
            }
          }
        }
        const allOk = ocr.ok && llmAnyOk;
        showMsg(parts.join(" | "), allOk ? "info" : "err");
      } catch (err) {
        showMsg(`✗ 检测失败: ${err.message}`, "err");
        log.err("health probe failed", err);
      } finally {
        btn.disabled = false;
        btn.textContent = originalText;
      }
    });

  load();
})();
