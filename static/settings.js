/* ============================================================
   Settings page — 业界做法重构 (参考 OpenAI/Anthropic/Linear)
   ------------------------------------------------------------
   设计原则:
   1. 已配置 provider = 脱敏只读展示 + 操作按钮 (更换/测试/移除/设为当前)
   2. 未配置 provider = 空白 input + "保存此 Key" 即时保存
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
    return OCR_DISPLAY[backend] || backend;
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
          <button type="button" class="provider-toggle-btn" data-action="toggle">${isConfigured ? "更换 Key" : "展开"}</button>
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

  // 已配置: 显示脱敏 key + base_url/model 只读 + "更换 Key" input (默认隐藏)
  // 未配置: 显示空白 input + 引导文案
  function renderProviderBody(prov, isConfigured) {
    if (isConfigured) {
      // 已配置 — 脱敏只读展示
      return `
        <div class="grid grid-cols-2 gap-3 mt-3">
          <div>
            <label class="field-label">协议</label>
            <select class="input" name="${esc(prov.name)}_protocol">
              <option value="openai" ${prov.protocol === "openai" ? "selected" : ""}>openai</option>
              <option value="anthropic" ${prov.protocol === "anthropic" ? "selected" : ""}>anthropic</option>
            </select>
          </div>
          <div>
            <label class="field-label">模型</label>
            <input class="input" name="${esc(prov.name)}_model" value="${esc(prov.model)}" />
          </div>
        </div>
        <div class="mt-3">
          <label class="field-label">Base URL</label>
          <input class="input" name="${esc(prov.name)}_base_url" value="${esc(prov.base_url)}" />
        </div>
        <div class="mt-3">
          <label class="field-label">当前 API Key</label>
          <div class="key-display">${esc(prov.api_key)} <span class="muted">(已保存)</span></div>
          <div class="mt-2 key-replace-section hidden">
            <label class="field-label">输入新 Key 覆盖原值</label>
            <input class="input" type="password" name="${esc(prov.name)}_api_key" placeholder="粘贴新的 API Key..." autocomplete="new-password" />
            <div class="mt-2 flex gap-2">
              <button type="button" class="save-key-btn btn-primary-small" data-provider="${esc(prov.name)}">保存此 Key</button>
              <button type="button" class="clear-key-btn btn-danger-small" data-provider="${esc(prov.name)}" title="清除已保存的 API Key">移除 Key</button>
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
            <option value="openai" ${prov.protocol === "openai" ? "selected" : ""}>openai</option>
            <option value="anthropic" ${prov.protocol === "anthropic" ? "selected" : ""}>anthropic</option>
          </select>
        </div>
        <div>
          <label class="field-label">模型</label>
          <input class="input" name="${esc(prov.name)}_model" value="${esc(prov.model)}" />
        </div>
      </div>
      <div class="mt-3">
        <label class="field-label">Base URL</label>
        <input class="input" name="${esc(prov.name)}_base_url" value="${esc(prov.base_url)}" />
      </div>
      <div class="mt-3">
        <label class="field-label">API Key <span class="muted">— 粘贴 ${esc(display(prov.name))} 的密钥</span></label>
        <input class="input" type="password" name="${esc(prov.name)}_api_key" placeholder="sk-..." autocomplete="new-password" />
        <div class="mt-2">
          <button type="button" class="save-key-btn btn-primary-small" data-provider="${esc(prov.name)}">保存此 Key</button>
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
    const list = document.getElementById("llm-providers-list");

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

    // Per-provider "保存此 Key" + "移除 Key" + "取消"
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
        await saveProviderKey(providerName, row);
      } else if (clearBtn) {
        await clearProviderKey(providerName, row);
      } else if (cancelBtn) {
        // 取消更换 Key: 隐藏 input 区域
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
  // toggleProviderBody — "更换 Key"/"展开" 按钮
  // ============================================================
  function toggleProviderBody(row, btn) {
    const body = row.querySelector(".provider-body");
    const replaceSection = row.querySelector(".key-replace-section");
    if (body.classList.contains("hidden")) {
      body.classList.remove("hidden");
      btn.textContent = "折叠";
      // 已配置的 provider: 自动展开"更换 Key"输入区
      if (replaceSection) replaceSection.classList.remove("hidden");
    } else {
      body.classList.add("hidden");
      btn.textContent = row.querySelector(".key-display") ? "更换 Key" : "展开";
      if (replaceSection) replaceSection.classList.add("hidden");
    }
  }

  // ============================================================
  // S5: saveProviderKey — 单 provider 保存 Key (立即生效)
  // ============================================================
  async function saveProviderKey(providerName, row) {
    const input = row.querySelector(`input[name="${CSS.escape(providerName)}_api_key"]`);
    if (!input) return;
    const keyValue = input.value.trim();
    if (!keyValue) {
      showMsg("请输入 API Key", "warn");
      input.focus();
      return;
    }
    if (keyValue === "__CLEAR__") {
      showMsg("Key 不能为 __CLEAR__ 保留字", "err");
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
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (r.ok && data.ok) {
        // Auto-activate (业界做法 — OpenAI/Anthropic/Linear):
        // 若当前 active provider 未配置 Key，保存新 provider 的 Key 后自动切到它。
        // 根除"配置了 SiliconFlow 但测试报 deepseek 未配置 Key"的死亡陷阱。
        const activeProv = (current.llm.providers || []).find(
          (p) => p.name === activeProvider,
        );
        const activeUnconfigured = !(activeProv && activeProv.configured);
        if (providerName !== activeProvider && activeUnconfigured) {
          showMsg(
            `✓ ${display(providerName)} 的 Key 已保存，并自动设为当前提供商`,
            "info",
          );
          await setActiveProvider(providerName, { silent: true });
        } else {
          showMsg(
            `✓ ${display(providerName)} 的 Key 已保存并立即生效`,
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
      title: `确认清除 ${display(providerName)} 的 API Key？`,
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
        showMsg(`✓ ${display(providerName)} 的 Key 已清除`, "info");
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
        resultEl.innerHTML = `<span class="badge-ok">✓ 连通正常${latency} · 模型: ${esc(data.model || "")}</span>`;
      } else {
        // 非 200 (如 403 Forbidden) 或 ok=false — 优先显示后端 reason，其次 detail
        const reason = data.reason || data.detail || `HTTP ${r.status}`;
        if (reason.includes("not configured") || reason.includes("API key")) {
          resultEl.innerHTML = `<span class="badge-no">✗ 未配置 API Key — 请点击"更换 Key"或"展开"输入</span>`;
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
  // removeProvider — 从注册表移除 (仅前端, 保存通用设置时持久化)
  // ============================================================
  async function removeProvider(name, row) {
    const ok = await window.PBC.confirmDialog({
      title: `确认移除 ${display(name)} 提供商？`,
      message: "（配置文件将保留，仅从 UI 列表移除）",
      confirmText: "确认",
      cancelText: "取消",
      danger: true,
    });
    if (!ok) return;
    row.remove();
    current.llm.providers = (current.llm.providers || []).filter(p => p.name !== name);
    if (activeProvider === name) {
      const firstRow = document.querySelector(".provider-row");
      if (firstRow) await setActiveProvider(firstRow.dataset.provider);
    }
  }

  // ============================================================
  // 加载设置 — 初始渲染
  // ============================================================
  async function load() {
    const r = await fetch("/api/settings");
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
        return;
      }
    }

    renderProviders(providers, activeProvider);
    fillOcrForm();
    fillFeishuForm();
    await loadRules();
  }

  // OCR 表单填充（从 load() 抽出，auto-activate 路径也复用）
  function fillOcrForm() {
    const badgeEl = document.getElementById("llm-provider-badge");
    if (badgeEl) badgeEl.textContent = display(activeProvider);

    // OCR
    setSeg("ocr-backend-seg", current.ocr.backend);
    showBackendForm(current.ocr.backend);
    document.getElementById("paddle_ocr_token").placeholder =
      current.ocr.paddle.token || "token";
    document.getElementById("paddle_ocr_api_url").value =
      current.ocr.paddle.api_url;
    document.getElementById("paddle_ocr_model").value =
      current.ocr.paddle.model;
    document.getElementById("paddle-status").innerHTML = statusBadge(
      current.ocr.paddle.configured,
    );
    document.getElementById("mineru_token").placeholder =
      current.ocr.mineru.token || "sk-...";
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
      feishuMsg(`✓ 已保存（${data.updated} 项）`);
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

  async function loadRules() {
    try {
      const r = await fetch("/api/settings/rules");
      const data = await r.json();
      rules = Array.isArray(data.rules) ? data.rules : [];
      ruleHits = data.hits && typeof data.hits === "object" ? data.hits : {};
      log("rules loaded", { count: rules.length });
      renderRules();
    } catch (err) {
      log.err("load rules failed", err);
    }
  }

  function renderRules() {
    const listEl = document.getElementById("rules-list");
    const countEl = document.getElementById("rules-count");
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

      row.appendChild(checkbox);
      row.appendChild(textarea);
      row.appendChild(del);
      const hit = document.createElement("div");
      hit.className =
        "pl-6 text-[11px] text-muted-foreground/60";
      hit.textContent = `历史命中 ${ruleHits[rule.id] ?? 0} 次`;
      listEl.appendChild(row);
      listEl.appendChild(hit);
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
    if (listEl && listEl.lastElementChild) {
      const ta = listEl.lastElementChild.querySelector("textarea");
      if (ta) {
        ta.value = text;
        ta.focus();
        ta.selectionStart = ta.value.length;
      }
    }
    ruleDirty();
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
    renderProviders(current.llm.providers, activeProvider);
    // Reset form
    select.value = "";
    custom.value = "";
    custom.classList.add("hidden");
    document.getElementById("llm-add-form").classList.add("hidden");
    document.getElementById("llm-add-toggle").classList.remove("hidden");
    showMsg(`已添加 ${display(name)} — 请填写字段并保存 Key`, "info");
  });

  // ============================================================
  // S7: 底部"保存通用设置" — 仅保存 OCR backend / enable_* 等通用字段
  // (per-provider Key 已由"保存此 Key"按钮即时保存)
  // ============================================================
  document
    .getElementById("settings-form")
    .addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const body = {};

      // 收集所有非空字段(排除 per-provider api_key — 已独立保存)
      for (const [k, v] of fd.entries()) {
        if (v === "") continue;
        if (k.endsWith("_api_key")) continue; // per-provider Key 由独立按钮处理
        body[k] = v;
      }

      // 收集 per-provider 的 protocol/base_url/model (允许批量更新)
      for (const [k, v] of fd.entries()) {
        if (k.endsWith("_protocol") || k.endsWith("_base_url") || k.endsWith("_model")) {
          if (v !== "") body[k] = v;
        }
      }

      body.llm_provider = activeProvider;
      if (pendingAdds.size > 0)
        body.llm_providers_add = Array.from(pendingAdds).join(",");

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
      const btn = document.getElementById("save-btn");
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = "保存中…";
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
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    });

  function showMsg(text, level) {
    const msg = document.getElementById("save-msg");
    msg.textContent = text;
    msg.style.color =
      level === "err"
        ? "hsl(0 84% 50%)"
        : level === "warn"
          ? "hsl(38 92% 50%)"
          : level === "info"
            ? "hsl(142 71% 35%)"
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
        const [healthData, ...providerTests] = await Promise.all([
          fetch("/api/health/downstream").then((r) => r.json()),
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
            if (reason.includes("not configured") || reason.includes("API key")) {
              parts.push(`○ ${display(name)}:未配置${tag}`);
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
