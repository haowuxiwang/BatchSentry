/**
 * Command palette — Ctrl+K / ⌘K (P3-1)
 *
 * Linear-style universal command surface: fuzzy-ish (substring) filter over
 * page navigation, job jump, and page-scoped actions. Uses native <dialog>
 * (built-in focus trap + Esc close) so no custom focus management is needed.
 *
 * Keyboard: ↑/↓ select, Enter run, Esc close, Ctrl+K/⌘K toggle,
 *           G then H/S — goto-mode navigation (Linear-style progressive
 *           shortcut learning).
 *
 * Loaded on all three pages (upload/review/settings) as a shared component.
 */
(function () {
  "use strict";

  const log = (msg, ...rest) => {
    if (window.console && console.debug)
      console.debug("[PBC] " + msg, ...rest);
  };

  let dialog = null;
  let input = null;
  let list = null;
  let items = []; // { id, name, hint, group, run }
  let filtered = [];
  let selected = 0;
  let jobsCache = null;
  let jobsCacheAt = 0;
  let gotoModeUntil = 0;

  // ── 页面内操作（按当前页面存在的元素动态收集） ────────────────
  function pageCommands() {
    const cmds = [];
    const $ = (id) => document.getElementById(id);
    if ($("file-input"))
      cmds.push({
        id: "upload-file",
        name: "选择文件上传",
        hint: "U",
        group: "操作",
        run: () => $("file-input").click(),
      });
    if ($("btn-prev-page"))
      cmds.push({
        id: "review-prev",
        name: "上一页",
        hint: "←",
        group: "操作",
        run: () => $("btn-prev-page").click(),
      });
    if ($("btn-next-page"))
      cmds.push({
        id: "review-next",
        name: "下一页",
        hint: "→",
        group: "操作",
        run: () => $("btn-next-page").click(),
      });
    const reportLink = document.querySelector('a[href*="report.md"]');
    if (reportLink)
      cmds.push({
        id: "review-report",
        name: "下载报告",
        hint: "",
        group: "操作",
        run: () => {
          location.href = reportLink.getAttribute("href");
        },
      });
    if ($("settings-form"))
      cmds.push({
        id: "settings-save",
        name: "保存全部设置",
        hint: "",
        group: "操作",
        run: () => $("settings-form").requestSubmit(),
      });
    if ($("test-conn-btn"))
      cmds.push({
        id: "settings-test",
        name: "测试连接",
        hint: "",
        group: "操作",
        run: () => $("test-conn-btn").click(),
      });
    return cmds;
  }

  // ── 作业跳转（GET /api/jobs 复用 upload 页同端点，30s 缓存） ─────
  async function jobCommands() {
    const now = Date.now();
    if (jobsCache && now - jobsCacheAt < 30_000) return jobsCache;
    try {
      const r = await fetch("/api/jobs?page=1&page_size=20");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      jobsCache = (data.jobs || []).map((j) => ({
        id: "job-" + j.id,
        name: "打开作业：" + (j.filename || j.id),
        hint: "",
        group: "作业",
        run: () => {
          location.href = "/jobs/" + encodeURIComponent(j.id) + "/review?page=1";
        },
      }));
      jobsCacheAt = now;
      return jobsCache;
    } catch (e) {
      console.warn("[PBC] command palette — jobs fetch failed", e);
      return [];
    }
  }

  // ── 渲染 ──────────────────────────────────────────────────
  function render() {
    if (!list) return;
    const q = input.value.trim().toLowerCase();
    filtered = q
      ? items.filter((c) => c.name.toLowerCase().includes(q))
      : items;
    if (selected >= filtered.length) selected = 0;

    list.innerHTML = "";
    let lastGroup = null;
    filtered.forEach((c, i) => {
      if (c.group !== lastGroup) {
        lastGroup = c.group;
        const g = document.createElement("div");
        g.className = "cmd-group-title";
        g.textContent = c.group;
        list.appendChild(g);
      }
      const li = document.createElement("button");
      li.type = "button";
      li.className = "cmd-item" + (i === selected ? " active" : "");
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(i === selected));
      const name = document.createElement("span");
      name.className = "cmd-item-name";
      name.textContent = c.name;
      li.appendChild(name);
      if (c.hint) {
        const k = document.createElement("kbd");
        k.className = "cmd-item-hint";
        k.textContent = c.hint;
        li.appendChild(k);
      }
      li.addEventListener("click", () => runCommand(i));
      li.addEventListener("mousemove", () => selectItem(i));
      list.appendChild(li);
    });
    if (!filtered.length) {
      const empty = document.createElement("div");
      empty.className = "cmd-empty";
      empty.textContent = "无匹配命令";
      list.appendChild(empty);
    }
  }

  function selectItem(i) {
    selected = i;
    const els = list.querySelectorAll(".cmd-item");
    els.forEach((el, idx) => {
      el.classList.toggle("active", idx === i);
      el.setAttribute("aria-selected", String(idx === i));
    });
    if (els[i]) els[i].scrollIntoView({ block: "nearest" });
  }

  function runCommand(i) {
    const c = filtered[i];
    if (!c) return;
    log("command palette — run", c.name);
    dialog.close();
    c.run();
  }

  async function open() {
    if (!dialog) build();
    items = [
      {
        id: "nav-home",
        name: "打开首页",
        hint: "G H",
        group: "导航",
        run: () => (location.href = "/"),
      },
      {
        id: "nav-settings",
        name: "打开设置",
        hint: "G S",
        group: "导航",
        run: () => (location.href = "/settings"),
      },
      ...pageCommands(),
      ...(await jobCommands()),
    ];
    input.value = "";
    selected = 0;
    render();
    if (!dialog.open) dialog.showModal();
    input.focus();
  }

  function build() {
    dialog = document.createElement("dialog");
    dialog.className = "cmd-palette";
    dialog.setAttribute("aria-label", "命令面板");

    input = document.createElement("input");
    input.className = "cmd-input";
    input.type = "text";
    input.placeholder = "搜索命令或作业…";
    input.setAttribute("aria-label", "搜索命令或作业");
    input.addEventListener("input", () => {
      selected = 0;
      render();
    });
    input.addEventListener("keydown", onInputKeydown);

    list = document.createElement("div");
    list.className = "cmd-list";
    list.setAttribute("role", "listbox");

    dialog.appendChild(input);
    dialog.appendChild(list);
    document.body.appendChild(dialog);
  }

  function onInputKeydown(e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (filtered.length) selectItem((selected + 1) % filtered.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (filtered.length)
        selectItem((selected - 1 + filtered.length) % filtered.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      runCommand(selected);
    }
  }

  // ── 全局快捷键 ────────────────────────────────────────────
  function isTyping() {
    const t = document.activeElement;
    if (!t) return false;
    return (
      t.tagName === "INPUT" ||
      t.tagName === "TEXTAREA" ||
      t.tagName === "SELECT" ||
      t.isContentEditable
    );
  }

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (dialog && dialog.open) dialog.close();
      else open();
      return;
    }
    if (dialog && dialog.open) return; // dialog 打开时其它快捷键让位
    if (isTyping()) return;

    // Goto 模式：G 后接 H/S（Linear 式渐进快捷键）
    if (e.key.toLowerCase() === "g" && !e.ctrlKey && !e.metaKey && !e.altKey) {
      gotoModeUntil = Date.now() + 1500;
      return;
    }
    if (Date.now() < gotoModeUntil) {
      const k = e.key.toLowerCase();
      if (k === "h") {
        gotoModeUntil = 0;
        location.href = "/";
        return;
      }
      if (k === "s") {
        gotoModeUntil = 0;
        location.href = "/settings";
        return;
      }
      gotoModeUntil = 0;
    }
    // 上传页：U = 选择文件
    if (e.key.toLowerCase() === "u" && document.getElementById("file-input")) {
      document.getElementById("file-input").click();
    }
  });

  // 顶栏按钮（各页已提供 #cmd-palette-btn）
  document.addEventListener("click", (e) => {
    if (e.target.closest && e.target.closest("#cmd-palette-btn")) open();
  });

  log("command palette initialized");
})();
