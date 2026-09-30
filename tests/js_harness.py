"""共享的 node 侧 JS 测试 harness（供 tests/unit 的 `*_js.py` 复用）。

**为什么需要**：`static/*.js` 是浏览器 IIFE。仓库既有的护栏分两档 ——
"node 实跑纯函数"（强）与"源码字符串断言"（弱）。中间那一档（**DOM 交互类
函数的真实行为**）此前完全缺失，于是 `confirm-dialog` 的"回车是否会误确认
删除"这类**数据丢失级**判定只能靠肉眼看代码。

本模块提供最小可用的假 DOM + 定时器队列 + fetch/EventSource stub，让
`confirmDialog` / `updatePageLevelUI` / 行渲染等函数能在 node 里被**真实调用**
并按返回值/副作用断言。

⚠️ **诚实边界**（避免"假绿"）：
  - `innerHTML` 只做**扁平元素**解析：形如 `<tag attr="v">text</tag>` 的
    **同级兄弟**序列会被解析成真实子节点（因此 `querySelector` 能查到），
    但**嵌套** HTML 会被整体跳过（不报错，只是查不到子节点）。
    含 `<` 的文本内容也会让该元素解析失败。需要嵌套结构时请用
    `createElement` + `appendChild` 自建，或直接断言 `innerHTML` 字符串。
  - `innerHTML` 的原始字符串始终保留（`el.innerHTML` 读回原文，**未**解码），
    而解析出的文本节点会被**实体解码**（`&lt;`→`<`），与浏览器一致。
  - 选择器支持子集：`tag` / `.class`（可链式）/ `#id` / `[attr]` /
    `[attr="v"]` / `[attr^="v"]` / `[attr$="v"]` / `[attr*="v"]` /
    `:checked` / 逗号并列 / **后代组合**（`#list li[data-id]`）；`:not(...)` 与
    `>` 被**近似处理**（`>` 当后代；`:not` 直接忽略）。
    ⚠️ 忽略 `:not` 只会让结果**偏多**（断言可能假失败），而忽略 `:checked`
    会让结果**偏空**（断言假绿）—— 故 `:checked` 必须实现，不能"顺手忽略"。
  - `dataset` 会**反射**成 `data-*` 属性（与浏览器一致），故
    `[data-job-id="x"]` 能命中 `el.dataset.jobId = "x"`。
  - `cloneNode(deep)` 已实现，且**不复制事件监听器**（浏览器语义）；
    `replaceWith` 会同步更新 `getElementById` 注册表 —— 二者都是
    `settings.js::bindProviderActions`（"克隆换节点"防重复绑定）所必需。
  - 定时器是**手动驱动**的（`__flushTimers()`），不模拟真实时间流逝。
  - 微任务请用 `await new Promise(r => setImmediate(r))` 排空
    （`setTimeout` 已被接管为队列，不能用来等微任务）。
  ⇒ 断言请落在**可观测决策**上（Promise 的解析值、哪个元素被加了哪个 class、
     处理器是否被调用），而不是 DOM 树的高保真复刻。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "static"


def js_sources(*patterns: str) -> str:
    """把 `static/` 下匹配的全部 JS 按文件名排序拼接成一段文本。

    **口径类断言（限额字面量、端点名、共享常量）不得锚在"某个具体文件"上** ——
    一次拆分或改名就会让它静默失效（本仓已两次踩到这类"锚错文件"）。
    用本函数取"整页的源码面"，从而对文件拆分免疫。

    防空转：pattern 打错时**立刻**红（`assert files`），而不是让断言恒真。
    """
    files: list[Path] = []
    for pat in patterns:
        files.extend(STATIC.glob(pat))
    files = sorted(set(files))
    assert files, f"static/ 下没有文件匹配 {patterns} —— 断言会变成空断言"
    # utf-8-sig：static/settings.js 等 5 个文件带 BOM，BOM 会污染行首锚点
    return "\n".join(p.read_text(encoding="utf-8-sig") for p in files)


# ── 假 DOM / 定时器 / 网络 stub（在目标 JS 之前注入） ────────────────────
DOM_STUB = r"""
globalThis.window = globalThis;
globalThis.__els = {};
globalThis.__timers = [];
globalThis.__toasts = [];

globalThis.setTimeout = (fn, ms) => { const id = { fn, ms, kind: "timeout", cleared: false };
  __timers.push(id); return id; };
globalThis.clearTimeout = (id) => { if (id) id.cleared = true; };
globalThis.setInterval = (fn, ms) => { const id = { fn, ms, kind: "interval", cleared: false };
  __timers.push(id); return id; };
globalThis.clearInterval = (id) => { if (id) id.cleared = true; };
globalThis.__flushTimers = (maxRounds) => {
  let rounds = 0;
  while (rounds++ < (maxRounds || 50)) {
    const pending = __timers.filter((t) => t.kind === "timeout" && !t.cleared && !t._ran);
    if (!pending.length) break;
    pending.forEach((t) => { t._ran = true; t.fn(); });
  }
  return rounds;
};
globalThis.__tickInterval = (ms) => {
  __timers.filter((t) => t.kind === "interval" && !t.cleared && t.ms === ms)
          .forEach((t) => t.fn());
};
globalThis.__clearIntervals = (ms) => {
  __timers.filter((t) => t.kind === "interval" && t.ms === ms).forEach((t) => { t.cleared = true; });
};
globalThis.addEventListener = () => {};
globalThis.EventSource = function () {};
globalThis.fetch = async () => { throw new Error("fetch not stubbed for this test"); };

/* ── 选择器（子集） ── */
function __norm(sel) {
  return String(sel).split(",").map((s) => s.trim()).filter(Boolean);
}
/* 按空白切出后代组合（尊重引号与方括号内的空白，如 [data-x="a b"]）。
 * `>` 由调用方先替换成空白 —— 本引擎**不区分** "a b" 与 "a > b"
 * （都按"存在某祖先"近似），这是有意的简化，见模块 docstring。 */
function __splitDesc(sel) {
  const parts = []; let cur = ""; let depth = 0; let q = null;
  for (const ch of String(sel)) {
    if (q) { cur += ch; if (ch === q) q = null; continue; }
    if (ch === '"' || ch === "'") { q = ch; cur += ch; continue; }
    if (ch === "[") depth++;
    else if (ch === "]") depth--;
    if (/\s/.test(ch) && depth === 0) { if (cur.trim()) parts.push(cur.trim()); cur = ""; continue; }
    cur += ch;
  }
  if (cur.trim()) parts.push(cur.trim());
  return parts;
}
function __matchOne(node, sel) {
  sel = sel.replace(/:not\([^)]*\)/g, "").trim();
  // `:checked` —— `settings.js::feishuSelectedEvents` 用它取勾选的事件。
  // ⚠️ 伪类**不能**像 `:not` 那样"忽略掉"：忽略 `:checked` 会让选择器
  // 静默返回空集（"没勾任何事件"），断言随之变成空断言 —— 正是假绿的方向。
  const wantChecked = /:checked\b/.test(sel);
  sel = sel.replace(/:checked\b/g, "").trim();
  if (!sel) return false;
  if (wantChecked && node.checked !== true) return false;
  const m = sel.match(/^([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)((?:\[[^\]]*\])*)$/);
  if (!m) return false;
  const [, tag, mods, attrs] = m;
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  (mods || "").match(/[.#][\w-]+/g)?.forEach((part) => {
    if (part[0] === ".") { if (!node.classList.contains(part.slice(1))) { m.__bad = true; } }
    else if (part[0] === "#") { if (node.id !== part.slice(1)) { m.__bad = true; } }
  });
  if (m.__bad) return false;
  (attrs || "").match(/\[[^\]]*\]/g)?.forEach((a) => {
    const body = a.slice(1, -1);
    const am = body.match(/^\s*([\w-]+)\s*(\^=|\$=|\*=|=)?\s*(.*)$/);
    if (!am) { m.__bad = true; return; }
    const k = am[1];
    const op = am[2];
    const want = (am[3] || "").trim().replace(/^["']|["']$/g, "");
    // 属性值：优先 _attrs；其次回落到同名 DOM 属性（id/type/value 等反射属性，
    // 元素是用 `__el(id)` 建的，id 只落在属性上而没进 _attrs）。
    let actual = node._attrs[k];
    if (actual === undefined && k !== "class" && node[k] !== undefined
        && typeof node[k] !== "object" && typeof node[k] !== "function") {
      actual = String(node[k]);
    }
    if (k === "class" && actual === undefined) actual = node.className;
    actual = actual === undefined ? undefined : String(actual);
    if (!op) { if (actual === undefined) m.__bad = true; return; }
    if (actual === undefined) { m.__bad = true; return; }
    if (op === "=") { if (actual !== want) m.__bad = true; }
    else if (op === "^=") { if (!actual.startsWith(want)) m.__bad = true; }
    else if (op === "$=") { if (!actual.endsWith(want)) m.__bad = true; }
    else if (op === "*=") { if (!actual.includes(want)) m.__bad = true; }
  });
  return !m.__bad;
}
/* 后代组合：最右一段必须命中本节点，其余各段依次命中其祖先。 */
function __matchDescendant(node, parts) {
  let i = parts.length - 1;
  if (i < 0 || !__matchOne(node, parts[i])) return false;
  i--;
  let anc = node.parentNode;
  while (i >= 0) {
    if (!anc) return false;
    if (__matchOne(anc, parts[i])) i--;
    anc = anc.parentNode;
  }
  return true;
}
function __matches(node, sel) {
  if (!node || !node.tagName) return false;
  return __norm(sel).some((s) => {
    const parts = __splitDesc(s.replace(/>/g, " "));
    return parts.length > 0 && __matchDescendant(node, parts);
  });
}
function __descendants(root, out) {
  (root.children || []).forEach((c) => { out.push(c); __descendants(c, out); });
  return out;
}
function __queryAll(root, sel) {
  return __descendants(root, []).filter((n) => __matches(n, sel));
}

/* ── style（含 setProperty，参数矩阵渲染依赖） ── */
function __makeStyle() {
  return {
    setProperty(k, v) { this[k] = String(v); },
    removeProperty(k) { const v = this[k]; delete this[k]; return v; },
    getPropertyValue(k) { return this[k] === undefined ? "" : this[k]; },
  };
}

/* ── 极简 innerHTML → 子节点（扁平元素序列） ── */
function __decodeEntities(s) {
  return String(s)
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&");
}
function __parseAttrs(str, el) {
  const re = /([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;
  let m;
  while ((m = re.exec(str))) {
    const k = m[1];
    const raw = m[2] !== undefined ? m[2]
      : m[3] !== undefined ? m[3]
        : m[4] !== undefined ? m[4] : "";
    const v = __decodeEntities(raw);
    if (k === "class") el.className = v;
    else if (k === "id") { el.id = v; el._attrs.id = v; }
    else el._attrs[k] = v;
  }
}
/* 只认「同级兄弟」形态：<tag attrs>text</tag>，text 内不得含 `<`。
 * 嵌套结构匹配不到 → 静默跳过该元素（不抛错），调用方须自知边界。 */
function __parseHTML(el, html) {
  const re = /<([a-zA-Z][\w-]*)((?:[^>"']|"[^"]*"|'[^']*')*?)(?:\/>|>([^<]*)<\/\1\s*>)/g;
  let m; let last = 0;
  while ((m = re.exec(html))) {
    const between = html.slice(last, m.index);
    if (between.trim()) el.appendChild(document.createTextNode(__decodeEntities(between)));
    last = re.lastIndex;
    const child = __makeEl(m[1]);
    __parseAttrs(m[2] || "", child);
    if (m[3]) child.textContent = __decodeEntities(m[3]);
    el.appendChild(child);
  }
  const rest = html.slice(last);
  if (rest.trim()) el.appendChild(document.createTextNode(__decodeEntities(rest)));
}

/* ── 元素 ── */
function __makeEl(tag) {
  const cls = new Set();
  const attrs = {};
  const data = {};
  const camelToKebab = (k) => String(k).replace(/[A-Z]/g, (c) => "-" + c.toLowerCase());
  const el = {
    tagName: String(tag || "div").toUpperCase(), _tag: String(tag || "div"),
    children: [], parentNode: null, _attrs: attrs, _listeners: {},
    style: __makeStyle(), _text: "", _html: null,
    disabled: false, value: "", checked: false, id: "", type: "",
    naturalWidth: 0, naturalHeight: 0, offsetLeft: 0, offsetTop: 0,
    offsetWidth: 0, offsetHeight: 0, scrollTop: 0, clientHeight: 0,
    isConnected: true,
  };
  // `dataset` 必须**反射**到属性：浏览器里 `el.dataset.jobId = x` 等价于
  // `setAttribute("data-job-id", x)`，选择器 `[data-job-id="…"]` 才命中。
  // 不反射的话 `findJobRow()` / 事件委托的 `closest("li[data-job-id]")`
  // 在假 DOM 里永远查不到，护栏会静默变成空断言。
  el.dataset = new Proxy(data, {
    set(t, k, v) { t[k] = String(v); attrs["data-" + camelToKebab(k)] = String(v); return true; },
    get(t, k) { return t[k]; },
    has(t, k) { return k in t; },
    deleteProperty(t, k) { delete t[k]; delete attrs["data-" + camelToKebab(k)]; return true; },
    ownKeys(t) { return Reflect.ownKeys(t); },
    getOwnPropertyDescriptor(t, k) {
      return Reflect.getOwnPropertyDescriptor(t, k) ||
        { configurable: true, enumerable: true, value: undefined, writable: true };
    },
  });
  el.classList = {
    add: (...c) => c.forEach((x) => cls.add(x)),
    remove: (...c) => c.forEach((x) => cls.delete(x)),
    toggle: (c, force) => {
      const on = force === undefined ? !cls.has(c) : !!force;
      if (on) cls.add(c); else cls.delete(c);
      return on;
    },
    contains: (c) => cls.has(c),
    _all: () => [...cls],
  };
  Object.defineProperty(el, "className", {
    get: () => [...cls].join(" "),
    set: (v) => { cls.clear(); String(v || "").split(/\s+/).filter(Boolean).forEach((x) => cls.add(x)); },
  });
  Object.defineProperty(el, "textContent", {
    get: () => (el._text !== "" ? el._text : el.children.map((c) => c.textContent).join("")),
    set: (v) => { el._text = String(v == null ? "" : v); el.children.forEach((c) => { c.parentNode = null; }); el.children = []; },
  });
  Object.defineProperty(el, "innerHTML", {
    get: () => (el._html == null ? "" : el._html),
    set: (v) => {
      el._html = String(v == null ? "" : v);
      el.children.forEach((c) => { c.parentNode = null; });
      el.children = [];
      el._text = "";
      // 保留原始字符串（innerHTML 读回原文），同时把**扁平**元素解析成真实
      // 子节点，让 `querySelector("[data-...]")` 这类查询能命中。
      __parseHTML(el, el._html);
    },
  });
  el.appendChild = (c) => { c.parentNode = el; el.children.push(c); return c; };
  el.insertBefore = (c, ref) => {
    c.parentNode = el;
    const i = el.children.indexOf(ref);
    if (i < 0) el.children.push(c); else el.children.splice(i, 0, c);
    return c;
  };
  el.removeChild = (c) => { const i = el.children.indexOf(c); if (i >= 0) el.children.splice(i, 1); c.parentNode = null; return c; };
  el.replaceChildren = (...cs) => { el.children.forEach((c) => { c.parentNode = null; }); el.children = []; cs.forEach((c) => el.appendChild(c)); };
  el.remove = () => { if (el.parentNode) el.parentNode.removeChild(el); el.isConnected = false; };
  /* 深/浅克隆。**不**复制 `_listeners` —— 浏览器同样不复制，而
   * `settings.js::bindProviderActions` 正是靠"克隆换节点"来丢弃旧 handler
   * 实现防重复绑定（"第二次交互起按钮点了没反应"那个 bug）。若这里把
   * listener 也带过去，那条防重复逻辑就永远测不出来。 */
  el.cloneNode = (deep) => {
    const c = __makeEl(el._tag);
    c.className = el.className;
    c.id = el.id;
    Object.assign(c._attrs, el._attrs);
    // `dataset` 是 Proxy，数据存在它自己的 target 里，**不**等于 `_attrs`：
    // 只复制 `_attrs` 会让克隆体的 `dataset.xxx` 全变 undefined（选择器
    // `[data-xxx]` 还能命中，但 `el.dataset.xxx` 读不到 —— 于是按
    // `dataset` 写的断言静默变成 undefined）。按 kebab→camel 回灌。
    Object.keys(el._attrs).forEach((k) => {
      if (k.startsWith("data-")) {
        const camel = k.slice(5).replace(/-([a-z])/g, (_m, ch) => ch.toUpperCase());
        c.dataset[camel] = el._attrs[k];
      }
    });
    c._text = el._text;
    c._html = el._html;
    c.value = el.value;
    c.checked = el.checked;
    c.disabled = el.disabled;
    c.type = el.type;
    c.naturalWidth = el.naturalWidth;
    c.naturalHeight = el.naturalHeight;
    c.offsetLeft = el.offsetLeft;
    c.offsetTop = el.offsetTop;
    c.offsetWidth = el.offsetWidth;
    c.offsetHeight = el.offsetHeight;
    c.scrollTop = el.scrollTop;
    c.clientHeight = el.clientHeight;
    Object.keys(el.style).forEach((k) => {
      if (typeof el.style[k] !== "function") c.style[k] = el.style[k];
    });
    if (deep) el.children.forEach((ch) => c.appendChild(ch.cloneNode(true)));
    return c;
  };
  el.replaceWith = (n) => {
    if (el.parentNode) {
      const p = el.parentNode; const i = p.children.indexOf(el);
      p.children[i] = n; n.parentNode = p; el.parentNode = null; el.isConnected = false;
      // getElementById 必须继续找到**活节点**（浏览器语义）。克隆替换后
      // 若注册表仍指向已脱离文档的旧节点，后续 getElementById 会拿到死节点，
      // 与真实行为分叉（断言看着对、实际测的不是页面上的那个节点）。
      if (el.id && __els[el.id] === el) __els[el.id] = n;
    }
  };
  el.setAttribute = (k, v) => { el._attrs[k] = String(v); if (k === "id") el.id = String(v); };
  el.getAttribute = (k) => (k in el._attrs ? el._attrs[k] : null);
  el.removeAttribute = (k) => { delete el._attrs[k]; };
  el.addEventListener = (t, fn) => { (el._listeners[t] = el._listeners[t] || []).push(fn); };
  el.removeEventListener = (t, fn) => {
    const a = el._listeners[t] || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1);
  };
  el.dispatchEvent = (ev) => { (el._listeners[ev.type] || []).slice().forEach((fn) => fn(ev)); };
  el.focus = () => { globalThis.document.activeElement = el; };
  el.select = () => {};
  el.blur = () => {};
  el.scrollIntoView = () => {};
  el.querySelector = (sel) => __queryAll(el, sel)[0] || null;
  el.querySelectorAll = (sel) => __queryAll(el, sel);
  el.closest = (sel) => { let n = el; while (n) { if (__matches(n, sel)) return n; n = n.parentNode; } return null; };
  return el;
}
globalThis.__makeEl = __makeEl;
globalThis.__queryAll = __queryAll;
globalThis.__matches = __matches;

globalThis.__el = (id, tag) => {
  if (!__els[id]) { const e = __makeEl(tag || "div"); e.id = id; __els[id] = e; }
  return __els[id];
};

const __body = __makeEl("body");
const __docListeners = {};
globalThis.__keydown = (key, extra) => {
  const ev = Object.assign({ key, type: "keydown", preventDefault() {}, shiftKey: false }, extra || {});
  (__docListeners.keydown || []).slice().forEach((fn) => fn(ev));
  return ev;
};
globalThis.__docListenerCount = (type) => (__docListeners[type] || []).length;
globalThis.document = {
  body: __body,
  activeElement: null,
  documentElement: __makeEl("html"),
  styleSheets: [],
  // ⚠️ 注册表优先，**再回退到树内查找**：`createElement` 不登记 id（只有
  // `__el()` 会），而运行期 `createElement` + 设 `.id` + `appendChild` 建出来
  // 的元素（如 confirm-dialog.js 的 `#toast-container`）只查注册表会返回 null ——
  // 元素明明在 DOM 里却"查不到"，断言随即变成假失败或空断言。
  getElementById: (id) =>
    __els[id] || __descendants(__body, []).find((n) => n.id === id) || null,
  createElement: (tag) => __makeEl(tag),
  createTextNode: (t) => { const n = __makeEl("#text"); n._text = String(t); n.tagName = "#text"; return n; },
  createDocumentFragment: () => __makeEl("fragment"),
  querySelector: (sel) => __queryAll(__body, sel)[0] || null,
  querySelectorAll: (sel) => __queryAll(__body, sel),
  addEventListener: (type, fn) => { (__docListeners[type] = __docListeners[type] || []).push(fn); },
  removeEventListener: (type, fn) => {
    const a = __docListeners[type] || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1);
  },
  createRange: () => ({
    setStart() {}, setEnd() {}, surroundContents() {},
  }),
  createTreeWalker: () => ({ nextNode: () => null }),
};
globalThis.NodeFilter = { SHOW_TEXT: 4 };
globalThis.CSS = { escape: (s) => String(s) };

globalThis.PBC = {
  showToast: (msg, type) => { __toasts.push({ msg: String(msg), type: type || "info" }); },
  confirmDialog: () => Promise.resolve(false),
  promptDialog: () => Promise.resolve(null),
};

/* ── XHR / FormData / File ──────────────────────────────────────────
   上传路径（`static/upload.js`）走 `XMLHttpRequest` 而非 fetch，此前 harness
   未提供 ⇒ **该路径从未被行为测试覆盖**。以下是可编程桩：

     __xhr.queue.push({ status: 200, body: { job_id: "J1" } })  // 依次消费
     __xhr.queue.push({ status: 409, body: { detail: "..." } })
     __xhr.queue.push({ networkError: true })   // 触发 onerror
     __xhr.queue.push({ timeout: true })        // 触发 ontimeout

   未排队的请求 → 默认 200 + `{job_id: "JOB-<n>"}`，让"多文件"用例不必逐个排队。

   ⚠️ `send()` **不自动结算**：请求进入 `__xhr._live`，由测试显式
   `__xhr.flushOne()` / `__xhr.flush()` 触发 `onload`。自动结算会让
   **"串行 vs 并行"不可观测** —— 那是本桩存在的首要理由（多文件上传必须串行）。

   `__xhr.sent` 记录每次 send 的 `{method, url, kind, status}`，可断言
   请求顺序与是否带 `?force=1`。 */
globalThis.FormData = class FormData {
  constructor() { this._entries = []; }
  append(k, v) { this._entries.push([k, v]); }
  entries() { return this._entries.slice(); }
};
globalThis.File = class File {
  constructor(name, size) { this.name = name; this.size = size; }
};
globalThis.__xhr = {
  queue: [],
  sent: [],
  _live: [],
  /** 结算**最早**一个未结算请求（用于逐步观察串行队列）。 */
  flushOne() {
    const x = this._live.shift();
    if (x && typeof x.onload === "function") x.onload();
    return !!x;
  },
  /** 结算全部未结算请求（含结算过程中新发起的）。 */
  flush(maxRounds) {
    let rounds = 0;
    while (this._live.length && rounds < (maxRounds || 50)) { this.flushOne(); rounds += 1; }
  },
};
globalThis.XMLHttpRequest = class XMLHttpRequest {
  constructor() {
    this.upload = { addEventListener: () => {} };
    this.status = 0;
    this.responseText = "";
    this.timeout = 0;
  }
  open(method, url) { this._method = method; this._url = url; }
  setRequestHeader() {}
  send() {
    const spec = __xhr.queue.length
      ? __xhr.queue.shift()
      : { status: 200, body: { job_id: "JOB-" + (__xhr.sent.length + 1) } };
    if (spec.networkError) {
      __xhr.sent.push({ method: this._method, url: this._url, kind: "error" });
      if (typeof this.onerror === "function") this.onerror();
      return;
    }
    if (spec.timeout) {
      __xhr.sent.push({ method: this._method, url: this._url, kind: "timeout" });
      if (typeof this.ontimeout === "function") this.ontimeout();
      return;
    }
    this.status = spec.status;
    this.responseText =
      spec.raw !== undefined ? String(spec.raw) : JSON.stringify(spec.body || {});
    __xhr.sent.push({
      method: this._method, url: this._url, kind: "load", status: this.status,
    });
    __xhr._live.push(this);
  }
};
"""


def run_js(
    js_files: list[Path | str],
    probe: str,
    *,
    stub: str = "",
    use_dom: bool = False,
    expect_json: bool = True,
    timeout: int = 30,
):
    """拼接 `stub + js_files + probe` 交给 node 执行。

    probe 的最后一行必须 `console.log(JSON.stringify(...))`（`expect_json=True` 时）。
    """
    if shutil.which("node") is None:
        import pytest

        pytest.skip("node not on PATH")
    parts: list[str] = []
    if use_dom:
        parts.append(DOM_STUB)
    if stub:
        parts.append(stub)
    for f in js_files:
        parts.append((STATIC / f if not Path(f).is_absolute() else Path(f)).read_text(encoding="utf-8"))
    parts.append(probe)
    src = "\n".join(parts)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "probe.js"
        p.write_text(src, encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, f"node failed:\nSTDERR:\n{r.stderr}\nSTDOUT:\n{r.stdout}"
    last = r.stdout.strip().splitlines()[-1]
    return json.loads(last) if expect_json else last


def run_js_async(js_files, probe, *, stub="", use_dom=True, timeout=30):
    """同 `run_js`，但 probe 体被包进 async IIFE（需要 await 的用例）。

    注意是**同步**函数：async IIFE 在 node 内部完成，Python 侧无需 await。
    """
    return run_js(
        js_files,
        "(async () => {\n" + probe + "\n})();",
        stub=stub,
        use_dom=use_dom,
        timeout=timeout,
    )
