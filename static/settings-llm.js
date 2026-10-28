/* ============================================================
   Settings 页 · LLM Provider 卡片渲染 + 操作（激活/保存/清 Key/测试/移除）
   ------------------------------------------------------------
   由 static/settings.js 按职责拆出（docs/ADVERSARIAL_REVIEW_2026-09-28.md §维度 4）。
   状态宿主见 settings-state.js —— 本文件**不得**自建 current/activeProvider 等同名变量。
   ============================================================ */
(function () {
  "use strict";

  const S = window.PbcSettings;

  const { log, display, esc, statusBadge, BUILTIN } = S;

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
  // ⚠️ 本函数只服务"**用户显式切换**"这一条路径。自动改写的提示由
  //    showAutoActivateNotice 直接呈现后端事实（B4-4 ②），
  //    所以不再有"自动切换文案后缀"这个选项。
  // ============================================================
  async function setActiveProvider(name, opts = {}) {
    log("switching active provider", { from: S.state.activeProvider, to: name });
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
        S.state.activeProvider = name;
        // 用后端返回的最新 providers 列表刷新本地缓存（configured 标志等）
        if (Array.isArray(data.providers) && data.providers.length) {
          S.state.current.llm.providers = data.providers;
        }
        if (!opts.silent) {
          S.showMsg(`✓ 已切换到 ${display(name)}（立即生效）`, "info");
        }
        // 重新渲染列表: active 排首位
        renderProviders(S.state.current.llm.providers || [], S.state.activeProvider);
        log("active provider switched live", { active: S.state.activeProvider });
      } else {
        S.showMsg(`✗ 切换失败: ${data.detail || data.message || "未知错误"}`, "err");
      }
    } catch (err) {
      S.showMsg(`✗ 切换失败: ${err.message}`, "err");
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
      S.showMsg("请输入 API 密钥", "warn");
      input.focus();
      return;
    }
    if (keyValue === "__CLEAR__") {
      S.showMsg("密钥不能为 __CLEAR__ 保留字", "err");
      return;
    }

    const btn = row.querySelector(".save-key-btn");
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "保存中…";

    try {
      const body = {
        llm_provider: S.state.activeProvider,
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
      if (S.state.pendingAdds.has(providerName)) {
        body.llm_providers_add = providerName;
      }
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (r.ok && data.ok) {
        S.state.pendingAdds.delete(providerName); // 已注册成功，后续保存不再带 add
        const skippedNote = data.skipped?.length
          ? `（未修改: ${data.skipped.join("、")}）`
          : "";
        // Auto-activate (业界做法 — OpenAI/Anthropic/Linear):
        // 若当前 active provider 未配置 Key，保存新 provider 的 Key 后自动切到它。
        // 根除"配置了 SiliconFlow 但测试报 deepseek 未配置 Key"的死亡陷阱。
        const activeProv = (S.state.current.llm.providers || []).find(
          (p) => p.name === S.state.activeProvider,
        );
        const activeUnconfigured = !(activeProv && activeProv.configured);
        if (providerName !== S.state.activeProvider && activeUnconfigured) {
          S.showMsg(
            `✓ ${display(providerName)} 的密钥已保存，并自动设为当前提供商${skippedNote}`,
            "info",
          );
          await setActiveProvider(providerName, { silent: true });
        } else {
          S.showMsg(
            `✓ ${display(providerName)} 的密钥已保存并立即生效${skippedNote}`,
            "info",
          );
          // 用后端返回的 providers 列表刷新（避免 stale configured 标志）
          if (Array.isArray(data.providers) && data.providers.length) {
            S.state.current.llm.providers = data.providers;
          }
          renderProviders(S.state.current.llm.providers || [], S.state.activeProvider);
        }
      } else {
        const errMsg = data.detail?.errors?.join("; ") || data.detail || data.message || "保存失败";
        S.showMsg(`✗ ${errMsg}`, "err");
      }
    } catch (err) {
      S.showMsg(`✗ 保存失败: ${err.message}`, "err");
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
        llm_provider: S.state.activeProvider,
        [`${providerName}_clear_key`]: true,
      };
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (r.ok && data.ok) {
        S.showMsg(`✓ ${display(providerName)} 的密钥已清除`, "info");
        await S.load();
      } else {
        S.showMsg(`✗ 清除失败: ${data.detail || data.message}`, "err");
      }
    } catch (err) {
      S.showMsg(`✗ 清除失败: ${err.message}`, "err");
      log.err("clear provider key failed", err);
    }
  }

  // ============================================================
  // S4+S8: testProvider — 单独测试指定 provider
  // 收集表单中"未保存"的配置一并测试（填了字段但没点保存也能先验证连通性）；
  // 空字段回落到后端已保存配置。
  // ============================================================
  async function testProvider(providerName, row) {
    const resultEl = row.querySelector(".provider-test-result");
    if (!resultEl) return;

    resultEl.classList.remove("hidden");
    resultEl.innerHTML = `<span class="muted">检测中…</span>`;

    try {
      // 收集未保存的候选配置（掩码不传 — 后端会拒绝掩码当密钥）
      const body = { provider: providerName };
      let overridden = false;
      const collect = (suffix) => {
        const el = row.querySelector(`[name="${providerName}${suffix}"]`);
        return el ? el.value.trim() : "";
      };
      const apiKey = collect("_api_key");
      const baseUrl = collect("_base_url");
      const model = collect("_model");
      const protocol = collect("_protocol");
      if (apiKey && !apiKey.includes("****")) {
        body.api_key = apiKey;
        overridden = true;
      }
      if (baseUrl) { body.base_url = baseUrl; overridden = true; }
      if (model) { body.model = model; overridden = true; }
      if (protocol) { body.protocol = protocol; overridden = true; }

      const r = await fetch("/api/settings/test_provider", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok && data.ok) {
        const latency = data.latency_ms ? ` ${data.latency_ms}ms` : "";
        const fromForm = overridden ? "（按表单未保存配置测试）" : "";
        resultEl.innerHTML = `<span class="badge-ok">✓ 连通正常${latency} · 模型：${esc(data.model || "")}${fromForm}</span>`;
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
    S.state.current.llm.providers = (S.state.current.llm.providers || []).filter(p => p.name !== name);
    S.state.removedProviders.add(name);
    S.state.pendingAdds.delete(name); // 刚添加未保存即移除：撤销 pendingAdd，防止保存时复活
    if (S.state.activeProvider === name) {
      const firstRow = document.querySelector(".provider-row");
      if (firstRow) await setActiveProvider(firstRow.dataset.provider);
    }
  }

  Object.assign(S, { renderProviders });
})();
