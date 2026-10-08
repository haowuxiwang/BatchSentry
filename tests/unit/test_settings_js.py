"""settings 页前端的行为护栏（此前**零前端覆盖** —— 原本 1861 行的单文件只有后端 API 测试）。

⚠️ 该页已在 R65 按职责拆为 6 个模块（`settings-state.js` / `-llm.js` / `-ocr.js` /
`-feishu.js` / `-rules.js` + 入口 `settings.js`），见
`docs/ADVERSARIAL_REVIEW_2026-09-28.md` §维度 4。本文件因此**不再**锚单个文件：
`FILES` 按**依赖序**列出全部模块，口径类断言走 `js_sources("settings*.js")`。

**为什么必须测**：设置页是"配错就整条链路不通"的入口。本文件锁定四条
"配错了但界面看着正常"的路径：

1. **不可信文本的转义** —— provider 的 `name`/`model`/`base_url`/`api_key`
   全部来自配置文件（用户可手改 config.json），直接拼进 `innerHTML` 即存储型 XSS；
2. **规则总字数上限指示器** —— 后端 `_USER_RULES_TOTAL_MAX=8000` 是硬拒绝
   （400），前端那个 `N / 8000 字` 是**提交前唯一的预警**。它若不翻红，
   用户写完 8000 字才被拒，且不知道是哪条超了；
3. **飞书通知事件清单** —— `feishuSelectedEvents()` 决定提交哪些事件，
   勾错 = 该推的不推（GMP 场景下"任务失败"没通知出去）；
4. **provider 自动改写的呈现** —— 后端可能静默换掉当前 provider；
   `showAutoActivateNotice` 是用户唯一能看到的告知。

另有一条**跨语言常量一致性**机检（`TestRuleLimitParityWithBackend`）：
8000 / 1000 这两个上限在前端是硬编码字面量，后端是
`_USER_RULES_TOTAL_MAX` / `USER_RULES_TEXT_MAX`。本仓库对这类"两处实现"
的既有做法是加机检锁定（见 `test_status_js.py` / `test_findings_map_js.py`）。

拆分后各模块经 `window.PbcSettings` 命名空间导出（状态宿主 `settings-state.js` 建命名
空间，其余模块 `Object.assign` 挂自己的导出），但**测试仍全部经由真实入口驱动** ——
按依赖序拼接 `FILES` 后调用 `load()` 的 bootstrap + 模块自己绑的事件监听器，
**未新增任何生产代码**。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.js_harness import js_sources, run_js_async

REPO = Path(__file__).resolve().parents[2]
RULES_PY = REPO / "api" / "settings" / "rules.py"

#: settings 页按依赖序加载的全部模块（拆分见
#: docs/ADVERSARIAL_REVIEW_2026-09-28.md §维度 4）。
#: ⚠️ 顺序即依赖序：settings-state.js 必须最先（状态宿主），
#: settings.js 必须最后（入口，末尾调用 load()）。顺序护栏见
#: tests/unit/test_settings_js_modules.py::TestScriptOrder。
FILES = [
    "settings-state.js",
    "settings-llm.js",
    "settings-ocr.js",
    "settings-feishu.js",
    "settings-rules.js",
    "settings.js",
]


# ─────────────────────────────────────────────────────────────────────
# 服务端 /api/settings 的真实响应形态（取自 api/settings/read.py）
# ─────────────────────────────────────────────────────────────────────
def _base_settings() -> dict:
    return {
        "config_file": "/tmp/config.json",
        "config_exists": True,
        "llm": {
            "provider": "deepseek",
            "active_provider": "deepseek",
            "auto_activated": None,
            "providers": [
                {
                    "name": "deepseek", "configured": True, "protocol": "openai",
                    "model": "deepseek-chat", "base_url": "https://api.deepseek.com",
                    "api_key": "sk-****abcd",
                },
                {
                    "name": "glm", "configured": False, "protocol": "openai",
                    "model": "glm-4", "base_url": "", "api_key": "",
                },
            ],
        },
        "ocr": {
            "backend": "mineru", "slices": 1, "dual_compare": False, "json_mode": False,
            "paddle": {
                "api_url": "https://paddle", "token": "p-****",
                "model": "PP-OCRv4", "configured": True,
            },
            "mineru": {
                "token": "m-****", "base_url": "", "model_version": "vlm",
                "language": "ch", "enable_formula": True, "enable_table": True,
                "configured": True,
            },
        },
        "app": {"host": "127.0.0.1", "port": 8000},
        "feishu": {
            "enabled": False, "mode": "webhook", "webhook_url": "", "secret": "",
            "app_id": "", "app_secret": "", "open_id": "", "mobile": "", "events": [],
        },
    }


def _settings(**over) -> dict:
    s = _base_settings()
    if "active" in over:
        s["llm"]["active_provider"] = over["active"]
        s["llm"]["provider"] = over["active"]
    if "auto" in over:
        s["llm"]["auto_activated"] = over["auto"]
    if "providers" in over:
        s["llm"]["providers"] = over["providers"]
    if "ocr_backend" in over:
        s["ocr"]["backend"] = over["ocr_backend"]
    if "feishu" in over:
        s["feishu"].update(over["feishu"])
    return s


def _prov(name: str, **over) -> dict:
    p = {"name": name, "configured": False, "protocol": "openai",
         "model": "", "base_url": "", "api_key": ""}
    p.update(over)
    return p


# settings.js 的 bootstrap 会**无守卫**地访问这些元素（缺一个就整页脚本挂掉）
_REQUIRED_IDS = [
    "save-msg", "settings-form",
    "llm-providers-list", "llm-provider-badge",
    "llm-add-toggle", "llm-add-form", "llm-add-cancel", "llm-add-select",
    "llm-add-custom", "llm-add-btn",
    "ocr-backend-seg", "ocr-backend-badge",
    "paddle_ocr_token", "paddle_ocr_api_url", "paddle_ocr_model", "paddle-status",
    "mineru_token", "mineru_base_url", "mineru_model_version", "mineru_language",
    "mineru_enable_formula", "mineru_enable_table", "ocr_slices", "mineru-status",
    "rules-list", "rules-count", "rules-total-chars", "rule-msg", "rule-last-saved",
    "feishu-enabled", "feishu-mode", "feishu-url", "feishu-secret", "feishu-app-id",
    "feishu-app-secret", "feishu-open-id", "feishu-mobile", "feishu-events",
    "feishu-save-btn", "feishu-test-btn", "feishu-clear-btn", "feishu-msg",
]

_STUB_TMPL = r"""
globalThis.__fetchCalls = [];
globalThis.__postBodies = [];
globalThis.__confirmQueue = [];

/* 页面骨架 —— 必须先于 settings.js 建立（它的顶层监听器无守卫）。
 * 挂到 body 下：bindProviderActions 用 cloneNode + replaceWith 换节点，
 * 未入文档的节点 replaceWith 是空操作，与真实行为分叉。 */
__IDS__.forEach((id) => {
  const e = __el(id);
  document.body.appendChild(e);
});

globalThis.__settings = __SETTINGS__;
globalThis.__rules = __RULES__;

globalThis.fetch = async (url, opts) => {
  __fetchCalls.push({ url, opts });
  if (opts && opts.method === "POST") {
    __postBodies.push({ url, body: JSON.parse(opts.body) });
    return {
      ok: true, status: 200,
      json: async () => ({ updated: 1, skipped: [] }),
      text: async () => "",
    };
  }
  if (url === "/api/settings") {
    return { ok: true, status: 200, json: async () => __settings, text: async () => "" };
  }
  if (url === "/api/settings/rules") {
    return { ok: true, status: 200, json: async () => __rules, text: async () => "" };
  }
  return { ok: true, status: 200, json: async () => ({}), text: async () => "" };
};
window.PBC.confirmDialog = async () => (__confirmQueue.length ? __confirmQueue.shift() : false);
"""

_PRE = r"""
const settle = async (n) => {
  for (let i = 0; i < (n || 10); i++) await new Promise((r) => setImmediate(r));
};
const el = (id) => document.getElementById(id);
const txt = (id) => { const e = el(id); return e ? e.textContent : null; };
const provRows = () => el("llm-providers-list").querySelectorAll(".provider-row");
// ⚠️ harness 的 `innerHTML` 只保存**原始字符串**：`renderProvider` 用
// `div.innerHTML = …` 构建行 ⇒ 行内可读；而列表本身是 `appendChild` 拼的
// ⇒ `list.innerHTML` 为空。故断言一律拼各行原文，不读容器。
const provHtml = () => provRows().map((r) => r.innerHTML).join("");
const ruleRows = () => el("rules-list").querySelectorAll("[data-rule-index]");
const ruleTextareas = () => el("rules-list").querySelectorAll("textarea");
const feishuBoxes = () => el("feishu-events").querySelectorAll("input");
const clickIn = (container, node) => container.dispatchEvent({ type: "click", target: node });
"""


def _stub(settings: dict | None = None, rules: dict | None = None) -> str:
    return (
        _STUB_TMPL.replace("__IDS__", json.dumps(_REQUIRED_IDS))
        .replace("__SETTINGS__", json.dumps(settings or _settings(), ensure_ascii=False))
        .replace(
            "__RULES__",
            json.dumps(rules or {"rules": [], "hits": {}, "last_saved_at": None},
                       ensure_ascii=False),
        )
    )


def _probe(body: str, *, settings: dict | None = None, rules: dict | None = None):
    return run_js_async(
        FILES,
        _PRE + "\nawait settle();\n" + body,
        stub=_stub(settings, rules),
    )


# ─────────────────────────────────────────────────────────────────────
# KB 条文注入开关（C4/#169）用的 stub 扩展
# ─────────────────────────────────────────────────────────────────────
# 该控件在「知识库」分区；`initKbInjectToggle` 在**模块作用域**绑定（分区是
# `hidden` 而非销毁 ⇒ 元素始终在 DOM，与 `ocr-save-btn` 同款）。故需额外两件事：
#   ① 把控件元素建出来（不在 `_REQUIRED_IDS` 里 —— 生产代码对它有 `if (!cb)`
#      守卫，不是"缺了就整页挂掉"那一类）；
#   ② 让 POST 响应带上 `ok: true`（`_STUB_TMPL` 的 POST 返回体只有
#      `{updated, skipped}`，会让 `r.ok && data.ok` 走失败分支，测不出成功路径）。
#      `globalThis.__postOk = false` 可切到失败分支。
_KB_EXTRA = r"""
["kb_prompt_inject", "kb-inject-msg"].forEach((id) => {
  document.body.appendChild(__el(id));
});
const __origFetch = globalThis.fetch;
globalThis.fetch = async (url, opts) => {
  if (opts && opts.method === "POST") {
    __fetchCalls.push({ url, opts });
    __postBodies.push({ url, body: JSON.parse(opts.body) });
    const ok = globalThis.__postOk !== false;
    return {
      ok, status: ok ? 200 : 400,
      json: async () => (ok
        ? { ok: true, updated: 1, skipped: [], dropped: [] }
        : { detail: "boom" }),
      text: async () => "",
    };
  }
  return __origFetch(url, opts);
};
"""


def _probe_kb(body: str, *, settings: dict | None = None):
    """同 `_probe`，但额外建出 KB 注入开关元素 + 让 POST 可成功。"""
    return run_js_async(
        FILES,
        _PRE + "\nawait settle();\n" + body,
        stub=_stub(settings, None) + "\n" + _KB_EXTRA,
    )


# ─────────────────────────────────────────────────────────────────────
# provider 渲染
# ─────────────────────────────────────────────────────────────────────
class TestProviderRender:
    def test_known_names_use_display_override_and_status_badge(self):
        body = """
const html = provHtml();
console.log(JSON.stringify({
  deepseek: html.includes("DeepSeek"),
  glm: html.includes("GLM · 智谱"),
  ok: html.includes("✓ 已配置"),
  no: html.includes("未配置"),
  rows: provRows().length,
}));
"""
        assert _probe(body) == {
            "deepseek": True, "glm": True, "ok": True, "no": True, "rows": 2,
        }

    def test_unknown_provider_falls_back_to_raw_name(self):
        """后端新增 provider 时前端还没有显示名 —— 必须回落原始 name，
        不能渲染成空白（用户会看到一个没有名字的卡片）。"""
        body = """
console.log(JSON.stringify({
  raw: provHtml().includes("custom_llm"),
}));
"""
        assert _probe(body, settings=_settings(providers=[_prov("custom_llm")])) == {
            "raw": True,
        }

    def test_active_provider_is_rendered_first_with_tag(self):
        body = """
const rows = provRows();
console.log(JSON.stringify({
  first: rows[0].dataset.provider,
  tag: rows[0].innerHTML.includes("当前"),
  activeCls: rows[0].classList.contains("is-active"),
  second: rows[1].dataset.provider,
}));
"""
        assert _probe(body, settings=_settings(active="glm")) == {
            "first": "glm", "tag": True, "activeCls": True, "second": "deepseek",
        }

    def test_builtin_provider_cannot_be_removed(self):
        """内置 provider（deepseek / siliconflow）不渲染"移除"按钮 ——
        移除它们会让整条链路无可用模型。"""
        body = """
const byName = {};
provRows().forEach((r) => {
  byName[r.dataset.provider] = r.querySelectorAll('[data-action="remove"]').length;
});
console.log(JSON.stringify(byName));
"""
        assert _probe(body) == {"deepseek": 0, "glm": 1}

    def test_configured_provider_shows_masked_key(self):
        body = """
console.log(JSON.stringify({
  masked: provHtml().includes("sk-****abcd"),
  saved: provHtml().includes("已保存"),
}));
"""
        assert _probe(body) == {"masked": True, "saved": True}

    def test_ocr_backend_badge_and_seg_follow_config(self):
        body = """
console.log(JSON.stringify({
  badge: txt("ocr-backend-badge"),
  mineruStatus: el("mineru-status").innerHTML.includes("✓ 已配置"),
}));
"""
        assert _probe(body) == {"badge": "MinerU", "mineruStatus": True}

    def test_paddle_backend_switches_badge(self):
        body = """
console.log(JSON.stringify({ badge: txt("ocr-backend-badge") }));
"""
        assert _probe(body, settings=_settings(ocr_backend="paddle")) == {
            "badge": "PaddleOCR",
        }


# ─────────────────────────────────────────────────────────────────────
# 不可信文本必须转义（配置文件可被手改 = 存储型 XSS 面）
# ─────────────────────────────────────────────────────────────────────
class TestProviderEscaping:
    def test_provider_fields_are_escaped(self):
        """`name`/`model`/`base_url`/`api_key` 都来自 config.json，属不可信文本。
        直接拼进 `innerHTML` ⇒ 打开设置页即执行脚本。

        注：`div.dataset.provider = prov.name` 走的是 DOM API（不经 HTML 解析），
        本身不构成注入面，故本用例只锁 `innerHTML` 拼接的四处字段。
        """
        evil = '<img src=x onerror="alert(1)">'
        body = """
const html = provHtml();
console.log(JSON.stringify({
  rawImg: html.includes("<img"),
  escaped: html.includes("&lt;img"),
  escQuote: html.includes("&quot;alert(1)&quot;"),
  escapedFields: html.split("&lt;img").length - 1,
}));
"""
        out = _probe(
            body,
            settings=_settings(providers=[
                _prov(evil, model=evil, base_url=evil, api_key=evil),
            ]),
        )
        assert out["rawImg"] is False
        assert out["escaped"] is True
        assert out["escQuote"] is True
        # name（名称 span + 若干 name= 属性）/ model / base_url / api_key 均须转义
        assert out["escapedFields"] >= 3


# ─────────────────────────────────────────────────────────────────────
# 规则区：计数 / 命中徽标 / 字数上限预警 / 脏标记
# ─────────────────────────────────────────────────────────────────────
class TestRulesPanel:
    def test_empty_rules_shows_hint_and_zero_count(self):
        body = """
console.log(JSON.stringify({
  count: txt("rules-count"),
  hint: el("rules-list").textContent.includes("暂无自定义规则"),
  rows: ruleRows().length,
}));
"""
        assert _probe(body) == {"count": "0 条", "hint": True, "rows": 0}

    def test_rule_rows_render_text_and_hit_badge(self):
        rules = {
            "rules": [
                {"id": "r1", "text": "温度必须 15-25°C", "active": True},
                {"id": "r2", "text": "批号格式 B+8位", "active": False},
            ],
            "hits": {"r1": 3},
            "last_saved_at": "2026-09-01 10:00",
        }
        body = """
const rows = ruleRows();
console.log(JSON.stringify({
  count: txt("rules-count"),
  rows: rows.length,
  ta0: ruleTextareas()[0].value,
  checked0: rows[0].querySelectorAll("input[type=checkbox]")[0].checked,
  checked1: rows[1].querySelectorAll("input[type=checkbox]")[0].checked,
  badgeHit: rows[0].textContent.includes("命中 3"),
  badgeZero: rows[1].textContent.includes("0 命中"),
  savedBadge: txt("rule-last-saved"),
}));
"""
        out = _probe(body, rules=rules)
        assert out["count"] == "2 条"
        assert out["rows"] == 2
        assert out["ta0"] == "温度必须 15-25°C"
        assert out["checked0"] is True
        assert out["checked1"] is False
        assert out["badgeHit"] is True
        assert out["badgeZero"] is True
        assert "2026-09-01 10:00" in out["savedBadge"]

    def test_never_saved_badge_warns_rules_do_not_apply(self):
        """`last_saved_at` 为空必须明说"规则不会生效" —— 用户以为规则已生效
        就会依赖一个不存在的检查。"""
        body = """
console.log(JSON.stringify({
  badge: txt("rule-last-saved"),
  destructive: el("rule-last-saved").className.includes("text-destructive"),
}));
"""
        assert _probe(body) == {"badge": "从未成功保存 — 规则不会生效", "destructive": True}

    def test_total_chars_indicator_under_limit(self):
        rules = {"rules": [{"id": "r1", "text": "x" * 3000, "active": True}],
                 "hits": {}, "last_saved_at": None}
        body = """
console.log(JSON.stringify({
  text: txt("rules-total-chars"),
  muted: el("rules-total-chars").className.includes("bg-muted"),
  red: el("rules-total-chars").className.includes("text-destructive"),
}));
"""
        assert _probe(body, rules=rules) == {
            "text": "3000 / 8000 字", "muted": True, "red": False,
        }

    def test_total_chars_turns_red_over_limit(self):
        """超过 8000 必须翻红 —— 这是提交前唯一的预警（后端会 400 硬拒）。"""
        rules = {"rules": [{"id": "r1", "text": "x" * 4500, "active": True},
                           {"id": "r2", "text": "y" * 4000, "active": True}],
                 "hits": {}, "last_saved_at": None}
        body = """
console.log(JSON.stringify({
  text: txt("rules-total-chars"),
  red: el("rules-total-chars").className.includes("text-destructive"),
  muted: el("rules-total-chars").className.includes("bg-muted"),
}));
"""
        assert _probe(body, rules=rules) == {
            "text": "8500 / 8000 字", "red": True, "muted": False,
        }

    def test_editing_a_rule_marks_dirty_and_updates_counter(self):
        """textarea 的 input 事件必须：写回规则文本 + 置脏 + 更新字数。

        三者缺一，用户就会"改了但不知道有没有生效 / 不知道已经超限"。
        """
        rules = {"rules": [{"id": "r1", "text": "abc", "active": True}],
                 "hits": {}, "last_saved_at": None}
        body = """
const ta = ruleTextareas()[0];
ta.value = "z".repeat(1234);
ta.dispatchEvent({ type: "input" });
console.log(JSON.stringify({
  dirty: txt("rule-msg"),
  total: txt("rules-total-chars"),
}));
"""
        assert _probe(body, rules=rules) == {
            "dirty": "有未保存的修改", "total": "1234 / 8000 字",
        }

    def test_toggling_checkbox_marks_dirty(self):
        rules = {"rules": [{"id": "r1", "text": "abc", "active": True}],
                 "hits": {}, "last_saved_at": None}
        body = """
const cb = ruleRows()[0].querySelectorAll("input[type=checkbox]")[0];
cb.checked = false;
cb.dispatchEvent({ type: "change" });
console.log(JSON.stringify({ dirty: txt("rule-msg") }));
"""
        assert _probe(body, rules=rules) == {"dirty": "有未保存的修改"}

    def test_delete_requires_confirmation(self):
        """删除规则是破坏性操作：取消 ⇒ 一条都不能少；确认 ⇒ 才移除。

        规则是 GMP 检查项的一部分，误删等于静默放宽检查范围。
        """
        rules = {"rules": [{"id": "r1", "text": "abc", "active": True}],
                 "hits": {}, "last_saved_at": None}
        body = """
const list = el("rules-list");
const delBtn = () => list.querySelectorAll("button")[0];
__confirmQueue.push(false);
delBtn().dispatchEvent({ type: "click" });
await settle(4);
const afterCancel = ruleRows().length;
__confirmQueue.push(true);
list.querySelectorAll("button")[0].dispatchEvent({ type: "click" });
await settle(4);
console.log(JSON.stringify({ afterCancel, afterConfirm: ruleRows().length }));
"""
        assert _probe(body, rules=rules) == {"afterCancel": 1, "afterConfirm": 0}

    def test_rule_textarea_has_per_rule_maxlength(self):
        rules = {"rules": [{"id": "r1", "text": "abc", "active": True}],
                 "hits": {}, "last_saved_at": None}
        body = """
console.log(JSON.stringify({ maxLength: ruleTextareas()[0].maxLength }));
"""
        assert _probe(body, rules=rules) == {"maxLength": 1000}

    def test_rules_load_failure_is_surfaced(self):
        """规则加载失败不得静默 —— 否则用户以为"没有规则"而重复添加。"""
        body = """
console.log(JSON.stringify({ msg: txt("rule-msg") }));
"""
        out = run_js_async(
            FILES,
            _PRE + "\nawait settle();\n" + body,
            stub=_stub().replace(
                'if (url === "/api/settings/rules") {\n'
                '    return { ok: true, status: 200, json: async () => __rules, text: async () => "" };',
                'if (url === "/api/settings/rules") {\n'
                '    throw new Error("rules endpoint down");',
            ),
        )
        assert "规则加载失败" in out["msg"]
        assert "rules endpoint down" in out["msg"]

    def test_rule_render_survives_missing_list_element(self):
        """`#rules-list` 缺失时 renderRules 必须提前返回（只更新计数/字数），
        不得在 `listEl.innerHTML` 上抛错。"""
        body = """
const list = el("rules-list");
list.remove();
el("rules-list").id = "";
console.log(JSON.stringify({ ok: true, count: txt("rules-count") }));
"""
        assert _probe(body) == {"ok": True, "count": "0 条"}


class TestProviderActionBinding:
    def test_render_does_not_accumulate_click_listeners(self):
        """`renderProviders` 每次调用都会 `bindProviderActions()`。若不"克隆换
        节点"丢弃旧 handler，第二次渲染起每个按钮会绑上 N 份 handler ——
        "点了没反应"或双请求（双请求在保存密钥场景下会写两次配置）。

        可观测信号：列表节点上的 click 监听器**数量**。两次渲染后仍须为 2
        （委托块本身两个）。
        """
        body = """
const list0 = el("llm-providers-list");
const before = list0._listeners.click.length;
// 触发一次重新加载（saveFeishu 成功后会 await load() → 重新 renderProviders）
el("feishu-enabled").checked = true;
el("feishu-save-btn").dispatchEvent({ type: "click" });
await settle(8);
const list1 = el("llm-providers-list");
console.log(JSON.stringify({
  before,
  after: list1._listeners.click.length,
  swapped: list0 !== list1,
  rows: list1.querySelectorAll(".provider-row").length,
}));
"""
        assert _probe(body) == {"before": 2, "after": 2, "swapped": True, "rows": 2}


# ─────────────────────────────────────────────────────────────────────
# 飞书通知：勾选的事件必须原样提交
# ─────────────────────────────────────────────────────────────────────
class TestFeishuEvents:
    def test_selected_events_are_submitted_in_dom_order(self):
        """勾了哪些就提交哪些 —— 勾错 = 该推的不推（"处理失败"没通知出去
        在 GMP 场景下是合规事故）。"""
        settings = _settings(feishu={"events": ["error"]})
        body = """
const boxes = feishuBoxes();
const checkedAtLoad = boxes.filter((b) => b.checked).map((b) => b.value);
boxes[0].checked = true;      // review
el("feishu-enabled").checked = true;
clickIn(el("feishu-save-btn"), el("feishu-save-btn"));
await settle(6);
console.log(JSON.stringify({
  boxes: boxes.length,
  checkedAtLoad,
  body: __postBodies.length ? __postBodies[0].body : null,
  url: __postBodies.length ? __postBodies[0].url : null,
}));
"""
        out = _probe(body, settings=settings)
        assert out["boxes"] == 4
        assert out["checkedAtLoad"] == ["error"]
        assert out["url"] == "/api/settings"
        assert out["body"]["feishu_events"] == "review,error"
        assert out["body"]["feishu_enabled"] is True

    def test_no_selection_submits_empty_string(self):
        body = """
el("feishu-enabled").checked = true;
clickIn(el("feishu-save-btn"), el("feishu-save-btn"));
await settle(6);
console.log(JSON.stringify({ events: __postBodies[0].body.feishu_events }));
"""
        assert _probe(body) == {"events": ""}


# ─────────────────────────────────────────────────────────────────────
# provider 自动改写：用户唯一能看到的告知
# ─────────────────────────────────────────────────────────────────────
class TestAutoActivateNotice:
    def test_badge_and_message_when_backend_switched_provider(self):
        """后端启动期可能自动改写 provider（决策只在后端一处）。
        前端必须把这件事**说出来**，否则用户看到的就是"provider 被静默换掉"。"""
        settings = _settings(
            active="glm",
            auto={"applied": True, "from": "deepseek", "to": "glm",
                  "reason": "DeepSeek 密钥无效，已切换到 GLM · 智谱"},
        )
        body = """
console.log(JSON.stringify({
  badge: txt("llm-provider-badge"),
  msg: txt("save-msg"),
}));
"""
        assert _probe(body, settings=settings) == {
            "badge": "GLM · 智谱（已自动从 DeepSeek 切换）",
            "msg": "DeepSeek 密钥无效，已切换到 GLM · 智谱",
        }

    def test_badge_shows_plain_name_without_notice(self):
        """无通知时也必须写回纯名称 —— 徽标否则无人渲染（历史缺陷）。"""
        body = """
console.log(JSON.stringify({ badge: txt("llm-provider-badge") }));
"""
        assert _probe(body) == {"badge": "DeepSeek"}

    def test_unknown_active_provider_still_renders_a_name(self):
        body = """
console.log(JSON.stringify({ badge: txt("llm-provider-badge") }));
"""
        assert _probe(body, settings=_settings(active="custom_llm")) == {
            "badge": "custom_llm",
        }


# ─────────────────────────────────────────────────────────────────────
# 加载失败必须可见（不能整页留白）
# ─────────────────────────────────────────────────────────────────────
class TestLoadFailure:
    def test_network_failure_shows_actionable_message(self):
        body = """
console.log(JSON.stringify({
  msg: txt("save-msg"),
  color: el("save-msg").style.color,
}));
"""
        # fetch 抛错：用自定义 stub 覆盖
        out = run_js_async(
            FILES,
            _PRE + "\nawait settle();\n" + body,
            stub=_stub().replace(
                "globalThis.fetch = async (url, opts) => {",
                'globalThis.fetch = async () => { throw new Error("offline"); };\n'
                "globalThis.__unused = async (url, opts) => {",
            ),
        )
        assert "设置加载失败" in out["msg"]
        assert out["color"] == "hsl(var(--destructive))"

    def test_http_error_shows_status_code(self):
        body = """
console.log(JSON.stringify({ msg: txt("save-msg") }));
"""
        out = run_js_async(
            FILES,
            _PRE + "\nawait settle();\n" + body,
            stub=_stub().replace(
                'if (url === "/api/settings") {\n'
                '    return { ok: true, status: 200, json: async () => __settings, text: async () => "" };',
                'if (url === "/api/settings") {\n'
                '    return { ok: false, status: 503, json: async () => ({}), text: async () => "" };',
            ),
        )
        assert "HTTP 503" in out["msg"]


# ─────────────────────────────────────────────────────────────────────
# 跨语言常量一致性（本仓库对"两处实现"的既有做法：机检锁定）
# ─────────────────────────────────────────────────────────────────────
class TestRuleLimitParityWithBackend:
    """8000（总字数）与 1000（单条上限）在前端是硬编码字面量，后端是
    `_USER_RULES_TOTAL_MAX` / `USER_RULES_TEXT_MAX`。两处漂移的后果是
    "前端显示绿色、提交被 400 拒绝" —— 用户按提示写完才发现不行。"""

    def test_total_chars_limit_matches_backend(self):
        from api.settings.rules import _USER_RULES_TOTAL_MAX

        src = js_sources("settings*.js")
        assert f"const MAX = {_USER_RULES_TOTAL_MAX};" in src, (
            "settings 页前端的总字数上限必须等于后端 _USER_RULES_TOTAL_MAX"
        )

    def test_per_rule_maxlength_matches_backend(self):
        from api.settings.rules import _USER_RULES_TEXT_MAX

        src = js_sources("settings*.js")
        assert f"textarea.maxLength = {_USER_RULES_TEXT_MAX};" in src, (
            "settings 页前端的单条规则 maxLength 必须等于后端 USER_RULES_TEXT_MAX"
        )

    def test_backend_still_enforces_both_limits(self):
        """护栏的前提：后端**确实**在拒绝（否则前端的预警毫无意义）。"""
        from api.settings.rules import _USER_RULES_TEXT_MAX, _USER_RULES_TOTAL_MAX

        src = RULES_PY.read_text(encoding="utf-8")
        assert "len(text) > _USER_RULES_TEXT_MAX" in src
        assert "total_chars > _USER_RULES_TOTAL_MAX" in src
        assert _USER_RULES_TOTAL_MAX > _USER_RULES_TEXT_MAX, (
            "总上限必须大于单条上限，否则 8000 永远先被单条 1000 挡住"
        )

    def test_total_limit_is_not_derived_from_rule_count_by_accident(self):
        """总上限 8000 与"规则条数 × 单条上限"（100×1000=100000）**不同量级**：
        它不是算术推导出来的，而是按上下文窗口估的（见 rules.py 注释）。
        锁住这一点，防止有人"顺手"把它改成 MAX×TEXT_MAX。"""
        from api.settings.rules import (
            _USER_RULES_MAX,
            _USER_RULES_TEXT_MAX,
            _USER_RULES_TOTAL_MAX,
        )

        assert _USER_RULES_TOTAL_MAX != _USER_RULES_MAX * _USER_RULES_TEXT_MAX


# ─────────────────────────────────────────────────────────────────────
# KB 条文注入开关必须真的接通（C4/#169 —— 此前是"装饰开关"）
# ─────────────────────────────────────────────────────────────────────
class TestKbInjectToggle:
    """`kb_prompt_inject` 的**两端**都必须接线：读 GET、写 POST。

    背景：该开关此前只有 `config.py` 支持 —— `api/settings/write.py` 的
    `_STATIC_FIELDS` 白名单不含它，未知字段被**静默丢弃**，界面也无控件。
    于是"后端支持、代码活着、用户却改不了"。少任何一端，界面看着正常而
    开关没接线 —— 正是本文件要锁的那类"配错了但界面看着正常"。
    """

    def test_reads_payload_and_posts_on_toggle(self):
        """初值来自 GET 的 `kb.prompt_inject`（不是写死），勾选后真的 POST。

        ⚠️ payload 必须取 `True`（**异于**假 DOM 的默认 `false`）——
        否则"读了 payload"与"没读、恰好默认也是 false"无法区分，护栏会静默
        失效。R72 变异 **M5**（摘掉 `load()` 里的 `syncKbInjectToggle()`）
        一开始就是这么漏过去的；改成 `True` 后才真正咬住。
        """
        s = _settings()
        s["kb"] = {"prompt_inject": True}
        body = """
const cb = el("kb_prompt_inject");
const before = cb.checked;
cb.checked = false;
cb.dispatchEvent({ type: "change" });
await settle(20);
const posts = __postBodies.filter((p) => p.url === "/api/settings");
console.log(JSON.stringify({
  before: before,
  posted: posts.length ? posts[posts.length - 1].body.kb_prompt_inject : null,
  msg: txt("kb-inject-msg"),
}));
"""
        assert _probe_kb(body, settings=s) == {
            "before": True, "posted": False,
            "msg": "✓ 已关闭条文注入（仅保留引用）",
        }

    def test_defaults_to_true_when_payload_omits_kb(self):
        """后端字段缺失时回填 `true`（与 `config.py` 默认一致）。

        渲染成"关闭"是更坏的错 —— 用户会以为注入被关了，而实际仍在注入。
        """
        s = _settings()
        s.pop("kb", None)
        body = """
console.log(JSON.stringify({ checked: el("kb_prompt_inject").checked }));
"""
        assert _probe_kb(body, settings=s) == {"checked": True}

    def test_failed_save_rolls_back_checkbox(self):
        """保存失败必须回滚复选框 —— 否则界面显示一个**未生效**的状态。"""
        s = _settings()
        s["kb"] = {"prompt_inject": True}
        body = """
globalThis.__postOk = false;
const cb = el("kb_prompt_inject");
cb.checked = false;
cb.dispatchEvent({ type: "change" });
await settle(20);
console.log(JSON.stringify({
  checked: cb.checked,
  msg: txt("kb-inject-msg"),
}));
"""
        out = _probe_kb(body, settings=s)
        assert out["checked"] is True, "保存失败后必须回滚到旧值"
        assert out["msg"].startswith("✗"), out["msg"]
