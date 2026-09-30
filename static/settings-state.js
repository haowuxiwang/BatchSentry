/* ============================================================
   Settings 页 · 共享状态与纯工具（**唯一宿主**）
   ------------------------------------------------------------
   本文件是 settings 页前端的唯一状态宿主（与 review-state.js 同款模式）：
   `current` / `activeProvider` / `pendingAdds` / `removedProviders`
   只在这里创建，其余模块一律经 `window.PbcSettings.state.*` 读写。

   设计原则（沿用原 settings.js 头部）:
   1. 已配置 provider = 脱敏只读展示 + 操作按钮 (更换/测试/移除/设为当前)
   2. 未配置 provider = 空白 input + "保存此密钥" 即时保存
   3. active provider 切换 = 立即持久化 (不等底部保存)
   4. 单独 provider 测试连接 = 不混淆 active 状态
   5. 底部"保存通用设置" = 仅 OCR backend / 选项等通用字段

   ⚠️ 顺序：本文件必须**先于**其余 settings-*.js 加载（defer 按文档顺序执行）。
      顺序护栏：tests/unit/test_settings_js_modules.py::TestScriptOrder
   拆分依据：docs/ADVERSARIAL_REVIEW_2026-09-28.md §维度 4。
   ============================================================ */
(function () {
  "use strict";

  const S = (window.PbcSettings = window.PbcSettings || {});

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

  // HTML escape — XSS protection for user-controlled strings
  const esc = (s) =>
    String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  // ── 会话内可变状态：唯一宿主 ──────────────────────────────────
  // 其余模块**不得**自建同名变量；一律 `S.state.<name>` 读写，
  // 否则会重演 review.js 的"同名遮蔽"类缺陷。
  S.state = {
    /** /api/settings 的最近一次响应 */
    current: {},
    activeProvider: "",
    /** 本次会话新加、尚未保存到注册表的 provider */
    pendingAdds: new Set(),
    // 会话内已移除的 provider — 与 pendingAdds 对称，保存时随
    // llm_providers_remove 提交后端做差集合并（P1-3: 移除必须持久化，
    // 否则刷新后 provider 复活，UI 承诺与实际不符）
    removedProviders: new Set(),
  };

  Object.assign(S, {
    log,
    BUILTIN,
    DISPLAY_NAMES,
    OCR_DISPLAY,
    display,
    ocrDisplay,
    showBackendForm,
    statusBadge,
    esc,
  });
})();
