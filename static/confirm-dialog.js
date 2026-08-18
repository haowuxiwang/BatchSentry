/* ============================================================
   Shared UI primitives — Notion 风格确认/输入对话框 + Toast
   在 upload.html / review.html / settings.html 中均需引入
   挂载到 window.PBC 命名空间，避免与页面脚本冲突

   - confirmDialog({...}) → Promise<boolean>
       Esc/overlay 取消 / Enter 确认 / danger 控制确认按钮色调
       statusBadge 可选：{ text, dotClass } — 标题下方状态徽章
   - promptDialog({...})  → Promise<string|null>
       取消返回 null，确认返回输入值（可能为空字符串）
       Esc/overlay 取消 / Enter 确认（自动聚焦并选中文本）
   - showToast(msg, type) → void
       type: "ok" | "err" | "info"，替代原生 alert() 的轻量提示
   ============================================================ */
(function () {
  "use strict";

  window.PBC = window.PBC || {};

  // focus trap：Tab 键焦点循环限制在弹窗内（APG dialog pattern）
  function trapTab(modal, e) {
    const focusables = modal.querySelectorAll(
      'button, input, [href], [tabindex]:not([tabindex="-1"])',
    );
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  // === Toast 提示（替代原生 alert() 的轻量方案）===
  function showToast(msg, type = "info") {
    let container = document.getElementById("toast-container");
    if (!container) {
      container = document.createElement("div");
      container.id = "toast-container";
      container.className = "fixed bottom-4 right-4 z-[70] flex flex-col gap-2";
      document.body.appendChild(container);
    }
    const color =
      type === "ok"
        ? "text-success"
        : type === "err"
          ? "text-destructive"
          : "text-info";
    const toast = document.createElement("div");
    toast.className = `bg-card border border-border rounded-md px-4 py-2.5 text-[12px] ${color} shadow-md max-w-sm`;
    toast.style.cssText = "animation: fade-in-up 0.2s ease-out both;";
    toast.textContent = msg;
    container.appendChild(toast);
    setTimeout(() => {
      toast.style.transition = `opacity var(--ms-300), transform var(--ms-300)`;
      toast.style.opacity = "0";
      toast.style.transform = "translateY(4px)";
      setTimeout(() => toast.remove(), 300);
    }, 3000);
  }

  // === Notion 风格确认弹窗（替代原生 confirm()）===
  // 返回 Promise<boolean>，支持 Esc 取消 / Enter 确认 / 点击遮罩取消
  // danger=true 时确认按钮用 destructive 色调（用于删除等不可恢复操作）
  // statusBadge 可选：{ text, dotClass } — 在标题下方展示任务状态徽章
  function confirmDialog({
    title,
    message = "",
    confirmText = "确认",
    cancelText = "取消",
    danger = false,
    statusBadge = null,
  }) {
    return new Promise((resolve) => {
      let settled = false;
      const settle = (val) => {
        if (settled) return;
        settled = true;
        overlay.style.transition = `opacity var(--motion-hover)`;
        modal.style.transition =
          `opacity var(--motion-hover), transform var(--motion-hover)`;
        overlay.style.opacity = "0";
        modal.style.transform = "scale(0.96)";
        modal.style.opacity = "0";
        setTimeout(() => {
          overlay.remove();
          document.removeEventListener("keydown", onKey);
          document.body.style.overflow = "";
          resolve(val);
        }, 150);
      };

      const overlay = document.createElement("div");
      overlay.className =
        "fixed inset-0 z-[60] flex items-center justify-center px-4";
      // a11y (APG dialog pattern + AntD/shadcn Dialog 规范):
      // role=dialog + aria-modal + aria-labelledby/describedby。
      overlay.setAttribute("role", "dialog");
      overlay.setAttribute("aria-modal", "true");
      overlay.setAttribute("aria-labelledby", "confirm-dialog-title");
      overlay.style.cssText =
        "background: hsl(var(--foreground) / 0.32); backdrop-filter: blur(2px); animation: fade-in-up var(--motion-fade-in) both;";

      const modal = document.createElement("div");
      modal.className =
        "bg-card border border-border rounded-lg w-full max-w-sm overflow-hidden";
      modal.style.cssText =
        "box-shadow: var(--shadow-strong); animation: pbc-modal-in var(--ms-200) var(--ease-snap) both;";

      const body = document.createElement("div");
      body.className = "px-5 py-4";
      const titleEl = document.createElement("div");
      titleEl.id = "confirm-dialog-title";
      titleEl.className = "text-[14px] font-medium";
      titleEl.textContent = title;
      body.appendChild(titleEl);
      // 状态徽章 — 展示任务当前状态，让用户预判操作后果
      if (statusBadge) {
        const badge = document.createElement("div");
        badge.className =
          "inline-flex items-center gap-1.5 mt-2 text-[11px] text-muted-foreground bg-muted/50 px-2 py-0.5 rounded";
        const dot = document.createElement("span");
        dot.className = `w-1.5 h-1.5 rounded-full ${statusBadge.dotClass}`;
        badge.appendChild(dot);
        badge.appendChild(
          document.createTextNode(`当前状态：${statusBadge.text}`),
        );
        body.appendChild(badge);
      }
      if (message) {
        const msgEl = document.createElement("div");
        msgEl.id = "confirm-dialog-desc";
        msgEl.className =
          "text-[12px] text-muted-foreground mt-1.5 whitespace-pre-line leading-relaxed";
        msgEl.textContent = message;
        body.appendChild(msgEl);
        overlay.setAttribute("aria-describedby", "confirm-dialog-desc");
      }
      modal.appendChild(body);

      const footer = document.createElement("div");
      footer.className =
        "flex items-center justify-end gap-2 px-5 py-3 border-t border-border bg-muted/30";
      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.textContent = cancelText;
      cancelBtn.className =
        "btn-press focus-ring text-[12px] px-3 py-1.5 rounded text-muted-foreground hover:text-foreground";
      const confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.textContent = confirmText;
      confirmBtn.className = danger
        ? "btn-press focus-ring text-[12px] px-3 py-1.5 rounded bg-destructive text-destructive-foreground hover:bg-destructive/90 font-medium"
        : "btn-press focus-ring text-[12px] px-3 py-1.5 rounded bg-primary text-primary-foreground hover:bg-primary/90 font-medium";
      cancelBtn.addEventListener("click", () => settle(false));
      confirmBtn.addEventListener("click", () => settle(true));
      footer.appendChild(cancelBtn);
      footer.appendChild(confirmBtn);
      modal.appendChild(footer);

      overlay.appendChild(modal);
      // 点击遮罩空白处取消（点击 modal 本身不取消）
      overlay.addEventListener("click", (e) => {
        if (e.target === overlay) settle(false);
      });
      document.body.appendChild(overlay);
      document.body.style.overflow = "hidden";

      const onKey = (e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          settle(false);
        } else if (e.key === "Enter") {
          // 安全规范：Enter 行为跟随当前焦点。
          // 焦点在按钮上时，让浏览器默认行为处理（button Enter → click），
          // 这样危险操作默认聚焦 cancel，Enter 会触发 cancel 而非 confirm。
          // 焦点不在按钮时（如刚打开未 Tab），非危险操作默认确认（用户期望），
          // 危险操作默认取消（安全兜底）。
          if (
            document.activeElement === cancelBtn ||
            document.activeElement === confirmBtn
          ) {
            return; // 不 preventDefault，让浏览器触发 button click
          }
          e.preventDefault();
          settle(danger ? false : true);
        } else if (e.key === "Tab") {
          trapTab(modal, e);
        }
      };
      document.addEventListener("keydown", onKey);

      // 安全规范：危险操作默认聚焦"取消"按钮，防止回车误触删除
      // （APG/Polaris/Material 3 一致规范）。用户需主动 Tab 到确认按钮
      // 并按 Enter 才会确认，或鼠标点击确认按钮。
      setTimeout(() => cancelBtn.focus(), 50);
    });
  }

  // === Notion 风格输入对话框（替代原生 prompt()）===
  // 返回 Promise<string|null>：确认时返回输入值（可能为空字符串），
  // 取消（Esc / 点击遮罩 / 取消按钮）时返回 null。
  // Enter 触发确认，自动聚焦输入框并选中文本便于覆盖。
  function promptDialog({
    title,
    message = "",
    defaultValue = "",
    placeholder = "",
    confirmText = "确认",
    cancelText = "取消",
  }) {
    return new Promise((resolve) => {
      let settled = false;
      const settle = (val) => {
        if (settled) return;
        settled = true;
        overlay.style.transition = `opacity var(--motion-hover)`;
        modal.style.transition =
          `opacity var(--motion-hover), transform var(--motion-hover)`;
        overlay.style.opacity = "0";
        modal.style.transform = "scale(0.96)";
        modal.style.opacity = "0";
        setTimeout(() => {
          overlay.remove();
          document.removeEventListener("keydown", onKey);
          document.body.style.overflow = "";
          resolve(val);
        }, 150);
      };

      const overlay = document.createElement("div");
      overlay.className =
        "fixed inset-0 z-[60] flex items-center justify-center px-4";
      // a11y (APG dialog pattern + AntD/shadcn Dialog 规范)
      overlay.setAttribute("role", "dialog");
      overlay.setAttribute("aria-modal", "true");
      overlay.setAttribute("aria-labelledby", "prompt-dialog-title");
      overlay.style.cssText =
        "background: hsl(var(--foreground) / 0.32); backdrop-filter: blur(2px); animation: fade-in-up var(--motion-fade-in) both;";

      const modal = document.createElement("div");
      modal.className =
        "bg-card border border-border rounded-lg w-full max-w-sm overflow-hidden";
      modal.style.cssText =
        "box-shadow: var(--shadow-strong); animation: pbc-modal-in var(--ms-200) var(--ease-snap) both;";

      const body = document.createElement("div");
      body.className = "px-5 py-4";
      const titleEl = document.createElement("div");
      titleEl.id = "prompt-dialog-title";
      titleEl.className = "text-[14px] font-medium";
      titleEl.textContent = title;
      body.appendChild(titleEl);
      if (message) {
        const msgEl = document.createElement("div");
        msgEl.id = "prompt-dialog-desc";
        msgEl.className =
          "text-[12px] text-muted-foreground mt-1.5 whitespace-pre-line leading-relaxed";
        msgEl.textContent = message;
        body.appendChild(msgEl);
        overlay.setAttribute("aria-describedby", "prompt-dialog-desc");
      }
      // 输入框 — 使用与 settings.css .input 等价的 Tailwind utility 类，
      // 避免依赖 settings.css（review.html 不引入 settings.css）
      const input = document.createElement("input");
      input.type = "text";
      input.value = defaultValue;
      input.placeholder = placeholder;
      input.className =
        "w-full mt-3 px-3 py-1.5 text-[12px] bg-card border border-border rounded outline-none focus:border-foreground/60 text-foreground";
      body.appendChild(input);
      modal.appendChild(body);

      const footer = document.createElement("div");
      footer.className =
        "flex items-center justify-end gap-2 px-5 py-3 border-t border-border bg-muted/30";
      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.textContent = cancelText;
      cancelBtn.className =
        "btn-press focus-ring text-[12px] px-3 py-1.5 rounded text-muted-foreground hover:text-foreground";
      const confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.textContent = confirmText;
      confirmBtn.className =
        "btn-press focus-ring text-[12px] px-3 py-1.5 rounded bg-primary text-primary-foreground hover:bg-primary/90 font-medium";
      cancelBtn.addEventListener("click", () => settle(null));
      confirmBtn.addEventListener("click", () => settle(input.value));
      footer.appendChild(cancelBtn);
      footer.appendChild(confirmBtn);
      modal.appendChild(footer);

      overlay.appendChild(modal);
      // 点击遮罩空白处取消
      overlay.addEventListener("click", (e) => {
        if (e.target === overlay) settle(null);
      });
      document.body.appendChild(overlay);
      document.body.style.overflow = "hidden";

      const onKey = (e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          settle(null);
        } else if (e.key === "Enter") {
          e.preventDefault();
          settle(input.value);
        } else if (e.key === "Tab") {
          trapTab(modal, e);
        }
      };
      document.addEventListener("keydown", onKey);

      // 自动聚焦输入框并选中默认值，方便覆盖输入
      setTimeout(() => {
        input.focus();
        input.select();
      }, 50);
    });
  }

  window.PBC.showToast = showToast;
  window.PBC.confirmDialog = confirmDialog;
  window.PBC.promptDialog = promptDialog;
})();
