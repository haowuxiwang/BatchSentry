"""R120 / CR-1 ④：`guard_reason`（系统判定说明）必须在复核页**看得见**。

## 要防的失效模式

R97 把"为什么降级/收权"的理由从 `description` 里**拆出来**存进 `guard_reason`
（理由全是内部语：「L2 溯源失败」「LLM 独断 critical」）。这一步是对的 ——
复核员看的是记录缺陷，不是后端实现。但拆完之后**没有任何界面读它**：
`grep guard_reason static/ templates/` 零命中 ⇒ 复核员看到一张 `info` 卡片，
**看不到为什么是 info**。GMP 要求结论变更可解释，而"可解释"只存在于数据库里，
对使用者等于不存在。

## 判据

1. `guard_reason` 非空 ⇒ 卡片里有「系统判定说明」，且理由文本可见；
2. 为空 ⇒ **不渲染**（不能给每条都挂一个空壳）；
3. 理由必须经 `esc()` —— 它含 LLM/规则拼出的自由文本，直接插 innerHTML 是 XSS；
4. 默认**折叠**（`<details>`）：R97 刚把这些内部语从正文里拿掉，不能再摆回去。

⚠️ 防空转：每条"不含"断言都配一条"含描述文本"的前提，否则接线断了会假绿。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest_asyncio

from tests.js_harness import run_js_async

REPO = Path(__file__).resolve().parents[2]
FILES = ["findings-map.js", "review-state.js", "review-findings.js"]

_STUB = r"""
globalThis.__PBC__ = { job_id: "J1", page: 1, total_pages: 3 };
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
"""

_PRE = r"""
const R = window.PbcReview;
const F = R.findings;
const el = (id) => __el(id);
el("findings-list");
const mk = (over) => Object.assign({
  id: 1, page: 1, type: "参数越界", severity: "info", source: "llm_cross",
  description: "第1页 某参数越界", status: "pending", tier: "llm",
}, over || {});
const cardHtml = (f) => { F.renderFindings([f], false, 1); return el("findings-list").innerHTML; };
"""


def _probe(body: str):
    return run_js_async(FILES, _PRE + "\n" + body, stub=_STUB)


class TestGuardReasonVisible:
    def test_reason_is_rendered_and_labelled(self):
        out = _probe(
            """
const html = cardHtml(mk({ guard_reason: "L2 溯源失败：日期在原文中找不到" }));
console.log(JSON.stringify({
  hasDesc: html.includes("某参数越界"),
  hasLabel: html.includes("系统判定说明"),
  hasReason: html.includes("L2 溯源失败"),
  folded: html.includes("<details"),
}));
"""
        )
        assert out["hasDesc"] is True, "卡片没渲染描述 —— 接线断了，后面的断言都是假绿"
        assert out["hasLabel"] is True, "缺「系统判定说明」标签"
        assert out["hasReason"] is True, "理由文本没渲染出来"
        assert out["folded"] is True, "必须是折叠态（R97 刚把内部语从正文拿掉）"

    def test_absent_reason_renders_nothing(self):
        out = _probe(
            """
const html = cardHtml(mk({}));
console.log(JSON.stringify({
  hasDesc: html.includes("某参数越界"),
  hasLabel: html.includes("系统判定说明"),
}));
"""
        )
        assert out["hasDesc"] is True
        assert out["hasLabel"] is False, "无 guard_reason 时不得出现空壳说明"

    def test_reason_is_escaped(self):
        """理由含 HTML ⇒ 必须被转义（自由文本进 innerHTML 就是 XSS 面）。"""
        payload = '<img src=x onerror="alert(1)">'
        out = _probe(
            "const html = cardHtml(mk({ guard_reason: %s }));\n"
            "console.log(JSON.stringify({"
            " raw: html.includes('<img src=x'),"
            " escaped: html.includes('&lt;img') }));"
            % json.dumps(payload)
        )
        assert out["raw"] is False, "guard_reason 未转义就进了 innerHTML"
        assert out["escaped"] is True, "转义后的形态也没出现 —— 说明根本没渲染"


class TestOverviewRowTooltip:
    def test_tooltip_carries_the_reason(self):
        out = _probe(
            """
const html = F.overviewRow(mk({ guard_reason: "LLM 独断 critical" }));
console.log(JSON.stringify({
  hasDesc: html.includes("某参数越界"),
  hasTitle: html.includes("title="),
  hasReason: html.includes("LLM 独断 critical"),
}));
"""
        )
        assert out["hasDesc"] is True
        assert out["hasTitle"] is True, "总览行没挂 title 提示"
        assert out["hasReason"] is True

    def test_no_tooltip_without_reason(self):
        out = _probe(
            """
const html = F.overviewRow(mk({}));
console.log(JSON.stringify({
  hasDesc: html.includes("某参数越界"),
  hasTitle: html.includes("title="),
}));
"""
        )
        assert out["hasDesc"] is True
        assert out["hasTitle"] is False


# ─────────────────────────────────────────────────────────────────────────────
# API 层：字段必须真的被送出去
# ─────────────────────────────────────────────────────────────────────────────

class TestApiDeliversGuardReason:
    """上面 5 条用例**直接往 `f` 里塞** `guard_reason`，所以它们证明不了"字段到得了前端"。

    `list_findings` 现在是 `SELECT *` + `dict(r)`，字段自然在。但只要有人把它改成
    显式列清单（很常见的"减少 SELECT * 开销"重构）而漏了 `guard_reason`，
    那 5 条**仍然全绿**，界面上却永远看不到说明 —— 正是 R105 那种
    "判据层绿、管道层丢字段"。本类把这段接线上锁。
    """

    @pytest_asyncio.fixture
    async def review_db(self, tmp_path):
        import db.client as db_mod
        from config import config as _cfg

        db_path = tmp_path / "guard.db"
        orig = (_cfg["app"].database_path, _cfg["app"].output_dir, db_mod._db)
        _cfg["app"].database_path = str(db_path)
        _cfg["app"].output_dir = str(tmp_path / "output")
        db_mod._db = None
        try:
            yield await db_mod.get_db()
        finally:
            import core.pipeline as p_mod

            p_mod.db_lock._loop = None
            p_mod.db_lock._waiters = None
            p_mod.db_lock._locked = False
            if db_mod._db:
                await db_mod._db.close()
            db_mod._db = orig[2]
            _cfg["app"].database_path = orig[0]
            _cfg["app"].output_dir = orig[1]

    async def test_reason_reaches_the_payload(self, review_db):
        db = review_db
        await db.execute(
            "INSERT INTO jobs (id, filename, status, pdf_path) "
            "VALUES ('job-gr', 't.pdf', 'review', '/tmp/t.pdf')"
        )
        await db.execute(
            "INSERT INTO findings (job_id, page, type, severity, description, "
            "source, status, guard_reason) VALUES "
            "('job-gr', 4, 'signature_time_anomaly', 'info', '签名日期异常', "
            "'llm_cross', 'pending', '文案为推测性表述 ⇒ 降级为待人工核对')"
        )
        await db.execute(
            "INSERT INTO findings (job_id, page, type, severity, description, "
            "source, status) VALUES "
            "('job-gr', 5, 'completeness', 'warning', '缺签名', 'rule', 'pending')"
        )
        await db.commit()

        from api.review import list_findings

        payload = await list_findings("job-gr")
        rows = payload["findings"]
        assert len(rows) == 2, "两条 finding 必须都返回 —— 否则下面的断言是空转"

        by_page = {r["page"]: r for r in rows}
        assert "guard_reason" in by_page[4], (
            "API 没把 guard_reason 送出去 —— 前端读的是不存在的字段"
        )
        assert by_page[4]["guard_reason"] == "文案为推测性表述 ⇒ 降级为待人工核对"
        # 没有理由的那条不能是字符串 "None"（前端 `f.guard_reason ? ...` 会当真值）
        assert by_page[5].get("guard_reason") in (None, "")


class TestSsrParity:
    """SSR 是 JS 接管前的首屏，也是 **JS 加载失败时唯一可读面**。

    只给 `review-findings.js` 加折叠区，上面 6 条用例全绿，但用户在这两种情形下
    仍然看不到"为什么是 info"。故两处都必须有，且 SSR 的必须不被注入。

    这里**真渲染**模板里的那段 Jinja（不是 grep 源码）：把 `{% if f.guard_reason %}`
    到 `{% endif %}` 整块切出来交给 Jinja，配 `autoescape=True` —— 转义行为与线上
    一致（`main.py` 用 Jinja2Templates，.html 默认 autoescape）。
    """

    TEMPLATE = REPO / "templates" / "review.html"
    _BLOCK = r"\{% if f\.guard_reason %\}\s*<details.*?</details>\s*\{% endif %\}"

    def _source(self) -> str:
        import re

        src = self.TEMPLATE.read_text(encoding="utf-8")
        m = re.search(self._BLOCK, src, re.S)
        assert m, "review.html 里找不到 SSR 的「系统判定说明」块 —— SSR 侧接线断了"
        return m.group(0)

    def _render(self, finding: dict) -> str:
        from jinja2 import Environment

        env = Environment(autoescape=True)
        return env.from_string(self._source()).render(f=finding)

    def test_ssr_renders_the_reason(self):
        html = self._render({"guard_reason": "L2 溯源失败：日期在原文中找不到"})
        assert "系统判定说明" in html
        assert "L2 溯源失败" in html
        assert "<details" in html, "必须折叠（R97 刚把内部语从正文拿掉）"

    def test_ssr_renders_nothing_without_reason(self):
        assert self._render({"guard_reason": None}).strip() == ""
        assert self._render({}).strip() == ""

    def test_ssr_block_never_opts_out_of_autoescape(self):
        """`| safe` 是**唯一**能让转义失效的写法 ⇒ 单独立一条静态断言。

        下面那条渲染测试在 autoescape 生效时抓不到"少了 `| e`"（`| e` 是冗余保险，
        不是防线）；真正能破防的是 `| safe`，故在这里钉死。
        """
        assert "safe" not in self._source(), "SSR 的说明块用了 | safe ⇒ 绕过 autoescape"

    def test_ssr_escapes_the_reason(self):
        html = self._render({"guard_reason": '<img src=x onerror="alert(1)">'})
        assert "<img src=x" not in html, "SSR 未转义 —— 模板注入面"
        assert "&lt;img" in html, "转义后形态也没出现 —— 说明根本没渲染"
