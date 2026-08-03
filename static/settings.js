/* ============================================================
   Settings page — dynamic LLM provider list + OCR config
   ============================================================
   - Providers are rendered dynamically from GET /api/settings
   - Each provider row has: name + protocol selector + api_key + base_url + model
   - "Use" button selects the active provider (radio-style)
   - "Remove" button removes custom providers (built-in deepseek/siliconflow cannot be removed)
   - Submit sends all changed fields using <provider>_<field> naming
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

  // Provider display name overrides (for nicer labels)
  const DISPLAY_NAMES = {
    deepseek: "DeepSeek",
    siliconflow: "SiliconFlow",
    glm: "GLM · 智谱",
    kimi: "Kimi · 月之暗面",
    qwen: "Qwen · 通义千问",
    mimo: "MiMo · 小米",
    anthropic: "Anthropic · Claude",
    openai: "OpenAI",
  };

  let current = {};
  let activeProvider = "";
  // Track providers added in this session (sent as llm_providers_add on save)
  const pendingAdds = new Set();

  // HTML escape — protects against XSS when rendering user-controlled
  // strings (base_url / model / provider name) into innerHTML.
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

  // Render a single provider row
  function renderProvider(prov, isActive) {
    const div = document.createElement("div");
    div.className = "provider-row";
    div.dataset.provider = prov.name;
    div.innerHTML = `
      <div class="provider-head">
        <div class="provider-name-row">
          <span class="provider-name">${esc(display(prov.name))}</span>
          <code class="provider-key">${esc(prov.name)}</code>
        </div>
        <div class="provider-actions">
          <button type="button" class="provider-use-btn ${
            isActive ? "active" : ""
          }" data-action="use">
            ${isActive ? "✓ 当前使用" : "使用"}
          </button>
          ${
            BUILTIN.has(prov.name)
              ? ""
              : `<button type="button" class="provider-remove-btn" data-action="remove">移除</button>`
          }
        </div>
      </div>
      <div class="provider-body">
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="field-label">协议</label>
            <select class="input" name="${esc(prov.name)}_protocol">
              <option value="openai" ${
                prov.protocol === "openai" ? "selected" : ""
              }>openai</option>
              <option value="anthropic" ${
                prov.protocol === "anthropic" ? "selected" : ""
              }>anthropic</option>
            </select>
          </div>
          <div>
            <label class="field-label">模型</label>
            <input class="input" name="${esc(prov.name)}_model" value="${esc(
              prov.model,
            )}" />
          </div>
        </div>
        <div class="mt-2">
          <label class="field-label">Base URL</label>
          <input class="input" name="${esc(prov.name)}_base_url" value="${esc(
            prov.base_url,
          )}" />
        </div>
        <div class="mt-2">
          <label class="field-label">API Key</label>
          <input class="input" name="${esc(
            prov.name,
          )}_api_key" placeholder="${esc(prov.api_key) || "sk-..."}" autocomplete="off" />
          <p class="field-hint">${statusBadge(prov.configured)}</p>
        </div>
      </div>
    `;
    return div;
  }

  // Render the entire provider list
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
    // Bind action buttons
    list.querySelectorAll(".provider-use-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const row = e.currentTarget.closest(".provider-row");
        setActive(row.dataset.provider);
      });
    });
    list.querySelectorAll(".provider-remove-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const row = e.currentTarget.closest(".provider-row");
        removeProvider(row.dataset.provider);
      });
    });
  }

  function setActive(name) {
    activeProvider = name;
    document.getElementById("llm-active-name").textContent = display(name);
    document.getElementById("llm-provider-badge").textContent = display(name);
    // Update button states without full re-render (avoids input focus loss)
    document.querySelectorAll(".provider-row").forEach((row) => {
      const isActive = row.dataset.provider === name;
      const btn = row.querySelector(".provider-use-btn");
      if (btn) {
        btn.textContent = isActive ? "✓ 当前使用" : "使用";
        btn.classList.toggle("active", isActive);
      }
    });
  }

  function removeProvider(name) {
    // Just remove the row from DOM; backend will keep the env var but it
    // won't be loaded unless LLM_PROVIDERS still lists it. We don't auto-
    // rewrite LLM_PROVIDERS here — that's an advanced action the user can
    // do by editing .env manually or via a future "purge" action.
    if (
      !confirm(`移除 ${display(name)} 的表单？\n（不会清除 .env 中已有的配置）`)
    )
      return;
    const row = document.querySelector(
      `.provider-row[data-provider="${CSS.escape(name)}"]`,
    );
    if (row) row.remove();
    if (activeProvider === name) {
      // Fall back to deepseek if we removed the active one
      const firstRow = document.querySelector(".provider-row");
      if (firstRow) setActive(firstRow.dataset.provider);
    }
  }

  async function load() {
    const r = await fetch("/api/settings");
    current = await r.json();
    log("settings loaded", current);
    document.getElementById("env-path").textContent = current.env_file;

    // LLM — render dynamic provider list
    activeProvider = current.llm.provider;
    renderProviders(current.llm.providers || [], activeProvider);
    document.getElementById("llm-active-name").textContent =
      display(activeProvider);
    document.getElementById("llm-provider-badge").textContent =
      display(activeProvider);

    // OCR — unchanged
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

  // OCR backend segment switch
  document.getElementById("ocr-backend-seg").addEventListener("click", (e) => {
    if (e.target.dataset.value) {
      setSeg("ocr-backend-seg", e.target.dataset.value);
      showBackendForm(e.target.dataset.value);
      document.getElementById("ocr-backend-badge").textContent =
        e.target.dataset.value === "mineru" ? "MinerU" : "PaddleOCR";
    }
  });

  // Add new provider — show custom input when "__custom" is selected
  document.getElementById("llm-add-select").addEventListener("change", (e) => {
    const custom = document.getElementById("llm-add-custom");
    if (e.target.value === "__custom") {
      custom.classList.remove("hidden");
      custom.focus();
    } else {
      custom.classList.add("hidden");
    }
  });

  document.getElementById("llm-add-btn").addEventListener("click", () => {
    const select = document.getElementById("llm-add-select");
    const custom = document.getElementById("llm-add-custom");
    let name = select.value;
    if (name === "__custom") {
      name = custom.value.trim().toLowerCase();
    }
    if (!name) {
      showMsg("请选择或输入 provider 名称", "warn");
      return;
    }
    // Validate: only [a-z0-9_-], 2-32 chars (mirror backend rule)
    if (!/^[a-z0-9_-]{2,32}$/.test(name)) {
      showMsg("名称只能含小写字母、数字、下划线、连字符（2-32 字符）", "err");
      return;
    }
    // Don't add if already exists
    const existing = document.querySelector(
      `.provider-row[data-provider="${CSS.escape(name)}"]`,
    );
    if (existing) {
      showMsg(`${display(name)} 已存在`, "warn");
      return;
    }

    // Default protocol per known providers
    const defaultProtocol = name === "anthropic" ? "anthropic" : "openai";
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
    // Add to current.providers and re-render
    current.llm.providers = current.llm.providers || [];
    current.llm.providers.push(newProv);
    renderProviders(current.llm.providers, activeProvider);
    // Track that this is a "new" provider to add via llm_providers_add
    pendingAdds.add(name);
    // Reset selector
    select.value = "";
    custom.value = "";
    custom.classList.add("hidden");
    showMsg(`已添加 ${display(name)}，填写后点击保存`, "info");
  });

  // Submit — collect all fields, including dynamic <provider>_<field>
  document
    .getElementById("settings-form")
    .addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const body = {};
      // Skip empty values (placeholder-driven UX)
      for (const [k, v] of fd.entries()) {
        if (v !== "") body[k] = v;
      }
      // Active provider
      body.llm_provider = activeProvider;
      // Pending new providers — tell backend to register them
      if (pendingAdds.size > 0) {
        body.llm_providers_add = Array.from(pendingAdds).join(",");
      }
      // OCR backend
      body.ocr_backend = document.querySelector(
        "#ocr-backend-seg button.active",
      ).dataset.value;
      // bool fields
      body.mineru_enable_formula = document.getElementById(
        "mineru_enable_formula",
      ).checked;
      body.mineru_enable_table = document.getElementById(
        "mineru_enable_table",
      ).checked;
      log("saving settings", Object.keys(body));
      const btn = document.getElementById("save-btn");
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = "保存中…";
      const msg = document.getElementById("save-msg");
      msg.textContent = "保存中…";
      msg.style.color = "hsl(var(--muted-foreground))";
      try {
        const r = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await r.json();
        if (r.ok && data.ok) {
          msg.textContent = "✓ " + data.message;
          msg.style.color = "hsl(142 71% 35%)";
          log("save ok", data);
          pendingAdds.clear();
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
        msg.textContent = "✗ " + err.message;
        msg.style.color = "hsl(0 84% 50%)";
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
          : "hsl(var(--muted-foreground))";
  }

  // === 测试连接按钮 ===
  document
    .getElementById("test-conn-btn")
    ?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      const originalText = btn.textContent;
      btn.disabled = true;
      btn.textContent = "测试中…";
      const msg = document.getElementById("save-msg");
      msg.textContent = "正在探测 OCR 与 LLM 服务…";
      msg.style.color = "hsl(var(--muted-foreground))";
      try {
        const r = await fetch("/api/health/downstream");
        const data = await r.json();
        log("health probe result", data);
        if (data.all_ok) {
          const ocrLatency = data.ocr.latency_ms
            ? ` ${data.ocr.latency_ms}ms`
            : "";
          const llmLatency = data.llm.latency_ms
            ? ` ${data.llm.latency_ms}ms`
            : "";
          msg.textContent = `✓ OCR(${data.ocr_backend})${ocrLatency} · LLM(${data.llm.provider})${llmLatency}`;
          msg.style.color = "hsl(142 71% 35%)";
        } else {
          const parts = [];
          if (!data.ocr.ok) parts.push(`OCR: ${data.ocr.reason || "失败"}`);
          if (!data.llm.ok) parts.push(`LLM: ${data.llm.reason || "失败"}`);
          msg.textContent = "✗ " + parts.join(" · ");
          msg.style.color = "hsl(0 84% 50%)";
        }
      } catch (err) {
        msg.textContent = "✗ 探测失败: " + err.message;
        msg.style.color = "hsl(0 84% 50%)";
        log.err("health probe failed", err);
      } finally {
        btn.disabled = false;
        btn.textContent = originalText;
      }
    });

  load();
})();
