/* ============================================================
   Settings 页 · 飞书通知（群机器人 Webhook）
   ------------------------------------------------------------
   由 static/settings.js 按职责拆出（docs/ADVERSARIAL_REVIEW_2026-09-28.md §维度 4）。
   状态宿主见 settings-state.js —— 本文件**不得**自建 current/activeProvider 等同名变量。
   ============================================================ */
(function () {
  "use strict";

  const S = window.PbcSettings;

  const { log, display } = S;

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
    // 默认 webhook — 与后端 load_feishu_config 的默认一致（config.py / notify.py
    // / read.py / probe.py 全链都是 webhook；此前前端兜底 app_bot 导致未配置时
    // UI 显示自建应用表单但实际默认是 webhook，测试连接报"未配置 App ID"误导）
    const modeEl = document.getElementById("feishu-mode");
    if (modeEl) modeEl.value = mode || "webhook";
    const appBot = document.querySelectorAll(".feishu-app-bot");
    const webhook = document.querySelectorAll(".feishu-webhook");
    appBot.forEach((el) => (el.style.display = mode === "app_bot" ? "" : "none"));
    webhook.forEach((el) => (el.style.display = mode === "webhook" ? "" : "none"));
  }

  function fillFeishuForm() {
    const f = S.state.current.feishu || {};
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
      feishu_mode: modeEl ? modeEl.value : "webhook",
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
    if (S.state.pendingAdds.size > 0) {
      body.llm_providers_add = Array.from(S.state.pendingAdds).join(",");
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
      await S.load();
    } catch (err) {
      feishuMsg(`✗ 保存失败: ${err.message}`, "err");
      log.err("feishu save failed", err);
    }
  }

  async function clearFeishu() {
    const modeEl = document.getElementById("feishu-mode");
    const mode = modeEl ? modeEl.value : "webhook";
    const cleared = mode === "app_bot"
      ? ["App ID", "App Secret", "open_id", "手机号"]
      : ["Webhook 地址", "签名密钥"];
    const ok = await window.PBC.confirmDialog({
      title: `确认清除飞书通知凭据（${mode === "app_bot" ? "自建应用" : "Webhook"}）？`,
      message: `将清空：${cleared.join("、")}。清除后立即生效，通知将停止发送。`,
      confirmText: "确认清除",
      cancelText: "取消",
      danger: true,
    });
    if (!ok) return;

    const body = {
      feishu_enabled: document.getElementById("feishu-enabled").checked,
      feishu_mode: mode,
      feishu_events: feishuSelectedEvents().join(","),
    };
    if (mode === "app_bot") {
      for (const f of ["feishu_app_id", "feishu_app_secret", "feishu_open_id", "feishu_mobile"]) {
        body[f] = "__CLEAR__";
      }
    } else {
      body.feishu_webhook_url = "__CLEAR__";
      body.feishu_secret = "__CLEAR__";
    }
    try {
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (!r.ok) {
        feishuMsg(`✗ 清除失败: ${JSON.stringify(data.detail || data)}`, "err");
        return;
      }
      feishuMsg(`✓ 已清除（${data.updated} 项）`);
      await S.load();
    } catch (err) {
      feishuMsg(`✗ 清除失败: ${err.message}`, "err");
      log.err("feishu clear failed", err);
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
    const body = { mode: modeEl ? modeEl.value : "webhook" };
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
  const feishuClearBtn = document.getElementById("feishu-clear-btn");
  if (feishuClearBtn) feishuClearBtn.addEventListener("click", clearFeishu);

  Object.assign(S, { fillFeishuForm });
})();
