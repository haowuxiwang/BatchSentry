"""review-suppressions.js 的行为护栏（此前只有**源码字符串**断言，零行为覆盖）。

**为什么必须测**：本模块是"被降噪规则抑制的候选条目"在复核页的**唯一出口**。
抑制 ≠ 删除 —— 后端把每条被抑制的候选条目连同**理由 + 命中证据**落
`finding_suppressions` 台账（EU GMP Annex 11 §16 / 中国附录《计算机化系统》
第 15/16 条：关键数据修改须记录理由）。台账在界面上不可见 = 审计链断裂。

三条最容易静默失效的路径（都不是"报错"，而是"悄悄给错信息"）：

1. 空 entries 时**没有隐藏面板** → 上一页的抑制条目残留在 DOM 里，复核者
   以为它属于当前页；
2. 加载失败时**清空了面板** → 已展示的证据被抹掉，复核者以为"本页无抑制"；
3. 翻页竞态：过期响应落地 → 覆盖新页的抑制台账（看错页的抑制理由）。

断言全部落在**可观测决策**上：class 是否含 hidden、innerHTML 文本、
请求 URL、toast 文案、按钮禁用态、调用顺序。
"""
from __future__ import annotations

import pytest

from tests.js_harness import run_js_async

FILES = ["findings-map.js", "review-state.js", "review-suppressions.js"]

# 服务端注入上下文 + 可编程 fetch 桩（json/text 双通道，revert 失败分支要读 text）。
_STUB = r"""
globalThis.__PBC__ = { job_id: "J1", page: 2, total_pages: 5 };
globalThis.__fetchQueue = [];
globalThis.__calls = { fetch: [] };
globalThis.__order = [];
globalThis.fetch = async (url, opts) => {
  __calls.fetch.push({ url, opts });
  const spec = __fetchQueue.length
    ? __fetchQueue.shift()
    : { ok: true, status: 200, json: {} };
  return {
    ok: spec.ok,
    status: spec.status,
    json: async () => (spec.json || {}),
    text: async () => (spec.text || ""),
  };
};
"""

_PRE = r"""
const R = window.PbcReview;
const S = R.suppressions;
R.state.jobId = "J1";
R.state.currentPage = 2;

// review.js 的刷新函数未加载 → 桩掉，并记录"被调用时已发出的请求数"，
// 用来证明刷新**先于**抑制台账重载（双面板一起刷新）。
R.refreshCurrentPageFindings = () => {
  __order.push({ at: "refresh", fetches: __calls.fetch.length });
  return Promise.resolve();
};

const mk = (id, tag) => __el(id, tag);
const panel = () => document.getElementById("suppression-panel");
const listEl = () => document.getElementById("suppression-list");
const briefEl = () => document.getElementById("suppression-brief");
const build = () => {
  mk("suppression-panel");
  mk("suppression-list");
  mk("suppression-brief");
};
const hidden = () => panel().classList.contains("hidden");
const settle = async (n) => {
  for (let i = 0; i < (n || 12); i++) await new Promise((r) => setImmediate(r));
};

// 一条典型的被抑制条目（形态取自 finding_suppressions 台账）
const E = (over) => Object.assign({
  id: 42,
  type: "param_out_of_spec",
  type_zh: "参数越界",
  severity: "critical",
  description: "实测 pH 7.9 超出规格上限",
  reason: "与已知偏差重复",
  evidence: { matched: [{ name: "pH", actual: "7.9", spec: "6.0-8.0", state: "超标" }] },
  reverted: false,
}, over || {});
"""


def _probe(body: str):
    return run_js_async(FILES, _PRE + "\n" + body, stub=_STUB)


# ─────────────────────────────────────────────────────────────────────
# 空 entries：必须隐藏面板并清空列表
# ─────────────────────────────────────────────────────────────────────
class TestEmptyEntries:
    def test_empty_entries_hides_panel_and_clears_list(self):
        """空 entries 必须**同时**隐藏面板 + 清空列表。

        只隐藏不清空：DOM 里残留上一页的条目，面板一旦因别处逻辑被显示
        就会露出别页的抑制理由。只清空不隐藏：留下一个空的 `<details>`，
        复核者以为"本页有抑制台账但没内容"。
        """
        body = """
build();
listEl().innerHTML = "<div class='stale'>上一页的抑制条目</div>";
S.renderSuppressions({ entries: [], total: 0 }, 3);
console.log(JSON.stringify({
  hidden: hidden(),
  children: listEl().children.length,
  brief: briefEl().textContent,
}));
"""
        assert _probe(body) == {"hidden": True, "children": 0, "brief": ""}

    def test_data_null_is_treated_as_empty(self):
        """`data` 为 null/undefined（后端返回空体）时按"无抑制"处理，不抛错。"""
        body = """
build();
S.renderSuppressions(null, 3);
const a = { hidden: hidden(), children: listEl().children.length };
S.renderSuppressions(undefined, 3);
console.log(JSON.stringify([a, { hidden: hidden(), children: listEl().children.length }]));
"""
        assert _probe(body) == [
            {"hidden": True, "children": 0},
            {"hidden": True, "children": 0},
        ]

    def test_entries_present_but_total_zero_still_hides(self):
        """`total` 为 0 时不展示 brief（`total ? ... : ""`）—— 避免出现
        "全批 0 条"却列出条目的自相矛盾文案。"""
        body = """
build();
S.renderSuppressions({ entries: [E()], total: 0 }, 2);
console.log(JSON.stringify({ hidden: hidden(), brief: briefEl().textContent }));
"""
        assert _probe(body) == {"hidden": False, "brief": ""}


# ─────────────────────────────────────────────────────────────────────
# 非空 entries：面板 + 摘要 + 证据 + 回退按钮
# ─────────────────────────────────────────────────────────────────────
class TestRenderEntries:
    def test_panel_shown_with_brief_and_evidence(self):
        body = """
build();
S.renderSuppressions({ entries: [E()], total: 5 }, 2);
const html = listEl().innerHTML;
console.log(JSON.stringify({
  hidden: hidden(),
  brief: briefEl().textContent,
  type: html.includes("参数越界"),
  severity: html.includes("critical"),
  desc: html.includes("实测 pH 7.9 超出规格上限"),
  reason: html.includes("抑制理由：与已知偏差重复"),
  evidence: html.includes("命中证据：pH：实测 7.9 / 规格 6.0-8.0 → 超标"),
  button: html.includes("revertSuppression(event, 42)"),
  anchorId: html.includes("suppression-42"),
}));
"""
        assert _probe(body) == {
            "hidden": False,
            "brief": "（第 2 页 1 条 · 全批 5 条）",
            "type": True,
            "severity": True,
            "desc": True,
            "reason": True,
            "evidence": True,
            "button": True,
            "anchorId": True,
        }

    def test_reverted_entry_shows_no_button(self):
        """已回退的条目**不得**再给回退按钮 —— 重复回退会二次写台账。"""
        body = """
build();
S.renderSuppressions({ entries: [E({ reverted: true })], total: 1 }, 2);
const html = listEl().innerHTML;
console.log(JSON.stringify({
  button: html.includes("revertSuppression("),
  mark: html.includes("已回退为正式问题"),
}));
"""
        assert _probe(body) == {"button": False, "mark": True}

    def test_missing_evidence_omits_evidence_line(self):
        """无 matched 证据时不渲染"命中证据："空行（避免出现 `命中证据：` 后接空白）。"""
        body = """
build();
S.renderSuppressions({ entries: [E({ evidence: {} })], total: 1 }, 2);
S.renderSuppressions({ entries: [E({ id: 7, evidence: null })], total: 1 }, 2);
const html = listEl().innerHTML;
console.log(JSON.stringify({
  noLabel: !html.includes("命中证据"),
  stillHasReason: html.includes("抑制理由：与已知偏差重复"),
}));
"""
        assert _probe(body) == {"noLabel": True, "stillHasReason": True}

    def test_type_falls_back_to_raw_type(self):
        """`type_zh` 缺失（后端新增类型未入映射）时回落原始 `type`，
        不能让复核者看到空白的类型名。"""
        body = """
build();
S.renderSuppressions({ entries: [E({ type_zh: "" })], total: 1 }, 2);
console.log(JSON.stringify({ raw: listEl().innerHTML.includes("param_out_of_spec") }));
"""
        assert _probe(body) == {"raw": True}

    def test_missing_fields_do_not_render_undefined(self):
        """字段大面积缺失时不得把 `undefined` 渲染进 DOM。"""
        body = """
build();
S.renderSuppressions({ entries: [{ id: 1 }], total: 1 }, 2);
console.log(JSON.stringify({
  undef: listEl().innerHTML.includes("undefined"),
  nullstr: listEl().innerHTML.includes("null"),
}));
"""
        assert _probe(body) == {"undef": False, "nullstr": False}


# ─────────────────────────────────────────────────────────────────────
# 不可信文本必须转义（LLM / 规则输出 → innerHTML）
# ─────────────────────────────────────────────────────────────────────
class TestEscaping:
    def test_description_and_reason_are_escaped(self):
        """description / reason 来自 LLM 与规则输出，属不可信文本。
        直接拼进 innerHTML = 存储型 XSS（台账内容可被上游污染）。"""
        body = """
build();
S.renderSuppressions({
  entries: [E({
    description: '<img src=x onerror="alert(1)">',
    reason: '<script>steal()</script>',
  })],
  total: 1,
}, 2);
const html = listEl().innerHTML;
console.log(JSON.stringify({
  rawImg: html.includes("<img"),
  rawScript: html.includes("<script"),
  escImg: html.includes("&lt;img"),
  escScript: html.includes("&lt;script&gt;"),
  escQuote: html.includes("&quot;alert(1)&quot;"),
}));
"""
        assert _probe(body) == {
            "rawImg": False,
            "rawScript": False,
            "escImg": True,
            "escScript": True,
            "escQuote": True,
        }

    def test_evidence_fields_are_escaped(self):
        """命中证据的 name/actual/spec/state 同样来自 OCR/规格文本，须转义。"""
        body = """
build();
S.renderSuppressions({
  entries: [E({
    evidence: { matched: [{ name: "<b>x</b>", actual: "<i>", spec: "<u>", state: "<s>" }] },
  })],
  total: 1,
}, 2);
const html = listEl().innerHTML;
console.log(JSON.stringify({
  raw: html.includes("<b>") || html.includes("<i>") || html.includes("<u>") || html.includes("<s>"),
  esc: html.includes("&lt;b&gt;"),
}));
"""
        assert _probe(body) == {"raw": False, "esc": True}


# ─────────────────────────────────────────────────────────────────────
# 缺少容器：不得抛错（模板改动 / 精简页面时的兜底）
# ─────────────────────────────────────────────────────────────────────
class TestMissingContainers:
    def test_no_panel_at_all_is_a_noop(self):
        body = """
const rv = S.renderSuppressions({ entries: [E()], total: 1 }, 2);
console.log(JSON.stringify({ rv: rv === undefined }));
"""
        assert _probe(body) == {"rv": True}

    def test_panel_without_list_is_a_noop(self):
        """只有 panel 没有 list：必须提前返回，不得在 `list.innerHTML` 上抛错。"""
        body = """
mk("suppression-panel");
const rv = S.renderSuppressions({ entries: [E()], total: 1 }, 2);
console.log(JSON.stringify({
  rv: rv === undefined,
  hidden: panel().classList.contains("hidden"),
}));
"""
        assert _probe(body) == {"rv": True, "hidden": False}

    def test_list_without_brief_is_fine(self):
        """brief 缺失只跳过摘要，列表照常渲染。"""
        body = """
mk("suppression-panel");
mk("suppression-list");
S.renderSuppressions({ entries: [E()], total: 1 }, 2);
console.log(JSON.stringify({ rendered: listEl().innerHTML.includes("参数越界") }));
"""
        assert _probe(body) == {"rendered": True}


# ─────────────────────────────────────────────────────────────────────
# loadSuppressions：URL / 失败不清空 / 代际守卫
# ─────────────────────────────────────────────────────────────────────
class TestLoadSuppressions:
    def test_success_renders_with_page_in_url(self):
        body = """
build();
__fetchQueue.push({ ok: true, status: 200, json: { entries: [E()], total: 5 } });
await S.loadSuppressions(2);
console.log(JSON.stringify({
  url: __calls.fetch[0].url,
  noOpts: __calls.fetch[0].opts === undefined,
  hidden: hidden(),
  brief: briefEl().textContent,
}));
"""
        assert _probe(body) == {
            "url": "/api/jobs/J1/suppressions?page=2",
            "noOpts": True,
            "hidden": False,
            "brief": "（第 2 页 1 条 · 全批 5 条）",
        }

    def test_http_error_does_not_clear_existing_panel(self):
        """加载失败**不得**清空/覆盖已有内容 —— 抹掉已展示的证据会让复核者
        以为"本页无抑制条目"（审计链在界面上被静默截断）。"""
        body = """
build();
S.renderSuppressions({ entries: [E()], total: 5 }, 2);
const before = listEl().innerHTML;
__fetchQueue.push({ ok: false, status: 500 });
await S.loadSuppressions(2);
console.log(JSON.stringify({
  unchanged: listEl().innerHTML === before,
  hidden: hidden(),
  fetches: __calls.fetch.length,
}));
"""
        assert _probe(body) == {"unchanged": True, "hidden": False, "fetches": 1}

    def test_stale_response_is_discarded_after_page_change(self):
        """代际守卫：请求发出后用户已翻页 → 过期响应必须丢弃。

        缺这条守卫会出现"第 4 页上显示第 3 页的抑制理由"——复核者据此
        裁决，等于拿别页的证据下结论。
        """
        body = """
build();
S.renderSuppressions({ entries: [E()], total: 5 }, 2);
const before = listEl().innerHTML;
__fetchQueue.push({ ok: true, status: 200, json: { entries: [E({ id: 99, description: "第3页的内容" })], total: 9 } });
const inflight = S.loadSuppressions(3);
R.state.currentPage = 4;          // 期间用户翻到第 4 页
await inflight;
console.log(JSON.stringify({
  unchanged: listEl().innerHTML === before,
  leaked: listEl().innerHTML.includes("第3页的内容"),
}));
"""
        assert _probe(body) == {"unchanged": True, "leaked": False}

    def test_matching_page_still_renders(self):
        """反向：页码一致时必须渲染 —— 守卫不能把正常路径也拦掉。"""
        body = """
build();
__fetchQueue.push({ ok: true, status: 200, json: { entries: [E()], total: 5 } });
await S.loadSuppressions(2);
console.log(JSON.stringify({ hidden: hidden() }));
"""
        assert _probe(body) == {"hidden": False}

    def test_network_exception_is_swallowed(self):
        """fetch 抛错（网络中断）只记日志，不得冒泡成未捕获异常。"""
        body = """
build();
globalThis.fetch = async () => { throw new Error("network down"); };
await S.loadSuppressions(2);
console.log(JSON.stringify({ ok: true, hidden: hidden() }));
"""
        assert _probe(body) == {"ok": True, "hidden": False}


# ─────────────────────────────────────────────────────────────────────
# revertSuppression：回退为正式问题
# ─────────────────────────────────────────────────────────────────────
class TestRevertSuppression:
    def test_success_toasts_then_refreshes_both_panels(self):
        """成功路径：toast → 刷新问题清单 → **再**重载抑制台账。

        顺序不能颠倒：回退后该 finding 才出现在问题清单，抑制台账也随之
        少一条。若先重载台账再刷新清单，两个面板会短暂互相矛盾。
        """
        body = """
build();
__fetchQueue.push({ ok: true, status: 200, json: {} });
__fetchQueue.push({ ok: true, status: 200, json: { entries: [], total: 0 } });
const btn = document.createElement("button");
btn.textContent = "回退为正式问题";
S.revertSuppression({ currentTarget: btn }, 42);
const during = { disabled: btn.disabled, text: btn.textContent };
await settle();
console.log(JSON.stringify({
  during,
  revertUrl: __calls.fetch[0].url,
  revertMethod: __calls.fetch[0].opts && __calls.fetch[0].opts.method,
  reloadUrl: __calls.fetch[1] && __calls.fetch[1].url,
  order: __order,
  toasts: __toasts,
  panelHiddenAfterReload: hidden(),
}));
"""
        # 成功路径**不**手动恢复按钮：抑制面板会整体重载（新节点替换旧节点），
        # 故只断言"重载真的落地了"（空 entries → 面板隐藏），不断言旧节点状态。
        assert _probe(body) == {
            "during": {"disabled": True, "text": "处理中…"},
            "revertUrl": "/api/jobs/J1/suppressions/42/revert",
            "revertMethod": "POST",
            "reloadUrl": "/api/jobs/J1/suppressions?page=2",
            "order": [{"at": "refresh", "fetches": 1}],
            "toasts": [{"msg": "已回退为正式问题，可在问题清单中裁决", "type": "ok"}],
            "panelHiddenAfterReload": True,
        }

    def test_http_error_toasts_and_restores_button(self):
        """失败路径：必须恢复按钮可用态，否则复核者以为"还在处理"而无法重试。"""
        body = """
build();
__fetchQueue.push({ ok: false, status: 409, text: "already reverted" });
const btn = document.createElement("button");
btn.textContent = "回退为正式问题";
S.revertSuppression({ currentTarget: btn }, 7);
await settle();
console.log(JSON.stringify({
  toasts: __toasts,
  disabled: btn.disabled,
  text: btn.textContent,
  fetches: __calls.fetch.length,
  refreshed: __order.length,
}));
"""
        assert _probe(body) == {
            "toasts": [{"msg": "回退失败: HTTP 409", "type": "err"}],
            "disabled": False,
            "text": "回退为正式问题",
            "fetches": 1,
            "refreshed": 0,
        }

    def test_missing_event_target_does_not_throw(self):
        """内联 onclick 在极端情况下可能拿不到 currentTarget —— 不得抛错。"""
        body = """
build();
__fetchQueue.push({ ok: true, status: 200, json: {} });
__fetchQueue.push({ ok: true, status: 200, json: { entries: [], total: 0 } });
S.revertSuppression({}, 42);
S.revertSuppression(null, 42);
await settle();
console.log(JSON.stringify({ toasts: __toasts.length }));
"""
        assert _probe(body) == {"toasts": 2}

    def test_exposed_for_inline_onclick(self):
        """模板里的按钮是内联 onclick，函数必须挂到 window。"""
        body = """
console.log(JSON.stringify({
  globalFn: typeof window.revertSuppression,
  same: window.revertSuppression === S.revertSuppression,
  render: typeof S.renderSuppressions,
  load: typeof S.loadSuppressions,
}));
"""
        assert _probe(body) == {
            "globalFn": "function",
            "same": True,
            "render": "function",
            "load": "function",
        }
