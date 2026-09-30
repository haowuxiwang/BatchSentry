/* ============================================================
   Settings 页 · 入口：事件接线 + 底部"保存全部"/"测试连接" + 启动 load()
   ------------------------------------------------------------
   由 static/settings.js 按职责拆出（docs/ADVERSARIAL_REVIEW_2026-09-28.md §维度 4）。
   状态宿主见 settings-state.js —— 本文件**不得**自建 current/activeProvider 等同名变量。
   ============================================================ */
(function () {
  "use strict";

  const S = window.PbcSettings;

  const { log, display, ocrDisplay, showBackendForm } = S;

  // ============================================================
  // OCR backend segment switch
  // ============================================================
  document.getElementById("ocr-backend-seg").addEventListener("click", (e) => {
    if (e.target.dataset.value) {
      S.setSeg("ocr-backend-seg", e.target.dataset.value);
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
      const body = { llm_provider: S.state.activeProvider, [target]: "__CLEAR__" };
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (r.ok && data.ok) {
        showMsg(`✓ OCR Token 已清除（立即生效）`, "info");
        await S.load();
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
    S.state.current.llm.providers = S.state.current.llm.providers || [];
    S.state.current.llm.providers.push(newProv);
    S.state.pendingAdds.add(name);
    // 对称性修复（对抗审查）：移除后重新添加同一 provider 时，若不清掉
    // removedProviders 里的名字，保存时 add+remove 同批提交相互抵消，
    // provider 会静默消失（等于没添加）。
    S.state.removedProviders.delete(name);
    S.renderProviders(S.state.current.llm.providers, S.state.activeProvider);
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

      body.llm_provider = S.state.activeProvider;
      if (S.state.pendingAdds.size > 0)
        body.llm_providers_add = Array.from(S.state.pendingAdds).join(",");
      if (S.state.removedProviders.size > 0)
        body.llm_providers_remove = Array.from(S.state.removedProviders).join(",");

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
          S.state.pendingAdds.clear();
          S.state.removedProviders.clear();
          // 重新加载所有状态
          setTimeout(() => S.load(), 500);
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
      const providers = S.state.current.llm.providers || [];
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
          const tag = name === S.state.activeProvider ? "·当前" : "";
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

  Object.assign(S, { showMsg });

  // 入口：全部模块已就绪，启动初始渲染（load 定义在 settings-ocr.js）
  S.load();
})();
