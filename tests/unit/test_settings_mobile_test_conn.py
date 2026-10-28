"""settings 页"测试连接"必须在**窄屏可达**（R87 定位的 UI 缺陷）。

**要防的失效模式**
------------------
唯一的连通性检测入口 `#test-conn-btn` 位于
`<nav class="settings-nav hidden md:flex …">` —— **桌面侧栏专属**。窄屏
（<768px）下侧栏整体 `hidden`，而 `.settings-nav-mobile` 只有 5 个 section
锚点、没有任何检测入口 ⇒ 移动端用户**完全无法**测试 OCR/LLM 连通性。

这是"元素存在"与"元素可达"的差别：`ui_e2e.py` 用
`page.locator("#test-conn-btn").click()` 在**桌面视口**下点得到，因此
缺陷对既有 e2e **不可见**。

**本文件钉住四条**
  1. 声明的触发器 == 接线的触发器（双向：不漏接、不悬空）；
  2. 触发器**不止一个**（否则必然回到"只有一个、且它在桌面侧栏"）；
  3. 至少一个触发器落在 `md:hidden` 容器里（窄屏可达）；
  4. 桌面触发器确实在 `hidden md:flex` 容器里（记录**为什么**需要第二个），
     且**只有一份**实现（`fetch("/api/health/downstream")` 全模块仅一处）。
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_HTML = _REPO / "templates" / "settings.html"
_JS = _REPO / "static" / "settings.js"

_TRIGGER_ID = re.compile(r'id="(test-conn-btn[a-z0-9-]*)"')
_BOUND_ID = re.compile(r'getElementById\("(test-conn-btn[a-z0-9-]*)"\)')


def _html() -> str:
    return _HTML.read_text(encoding="utf-8")


def _js() -> str:
    return _JS.read_text(encoding="utf-8")


def _declared_ids() -> set[str]:
    return set(_TRIGGER_ID.findall(_html()))


def _bound_ids() -> set[str]:
    return set(_BOUND_ID.findall(_js()))


def _enclosing_class(text: str, needle: str) -> str:
    """取 `needle` **之前最近**的一个 `class="…"`。

    模板里 `id` 写在元素自身 `class` 之前，故"最近的前一个 class"即其
    **外层容器**的 class（对移动端按钮即 `<div class="md:hidden mb-6">`）。
    """
    pos = text.index(needle)
    last = ""
    for m in re.finditer(r'class="([^"]*)"', text[:pos]):
        last = m.group(1)
    return last


def _enclosing_nav_class(text: str, needle: str) -> str:
    """取**包住** `needle` 的那个 `<nav …>` 的 class（未闭合才算包住）。"""
    pos = text.index(needle)
    nav_pos = text.rfind("<nav", 0, pos)
    nav_close = text.rfind("</nav>", 0, pos)
    assert nav_pos > nav_close, f"{needle} 不在任何 <nav> 元素内"
    m = re.search(r'class="([^"]*)"', text[nav_pos:pos])
    assert m, f"未解析到 <nav> 的 class（{needle}）"
    return m.group(1)


# ─── 1. 双向接线 ─────────────────────────────────────────────────


def test_declared_triggers_match_bound_triggers():
    """声明与接线必须一致：漏接 ⇒ 按钮点了没反应；悬空 ⇒ 死代码。"""
    declared, bound = _declared_ids(), _bound_ids()
    assert declared, "settings.html 里找不到任何 test-conn-btn* 触发器"
    assert declared == bound, (
        f"触发器声明/接线不一致 —— 声明 {sorted(declared)} vs 接线 {sorted(bound)}"
    )


# ─── 2. 可达性 ───────────────────────────────────────────────────


def test_there_is_more_than_one_trigger():
    """只有一个触发器 ⇒ 必然重演"它在桌面侧栏、窄屏不可达"。"""
    ids = _declared_ids()
    assert len(ids) >= 2, (
        f"settings 页只有 {len(ids)} 个连通性检测入口（{sorted(ids)}）—— "
        "窄屏用户将无法检测 OCR/LLM 连通性"
    )


def test_at_least_one_trigger_is_mobile_reachable():
    """至少一个触发器必须在 `md:hidden` 容器内（即窄屏可见）。"""
    html = _html()
    mobile = [i for i in _declared_ids() if i.endswith("-mobile")]
    assert mobile, (
        "没有任何 `*-mobile` 触发器 —— 桌面侧栏在窄屏整体 hidden，"
        "移动端将完全没有检测入口"
    )
    for tid in mobile:
        cls = _enclosing_class(html, f'id="{tid}"')
        assert "md:hidden" in cls, (
            f"#{tid} 的外层容器 class 不含 `md:hidden`（实际 {cls!r}）—— "
            "该入口在窄屏下会与侧栏一起被隐藏"
        )


def test_desktop_trigger_is_inside_the_desktop_only_sidebar():
    """记录**为什么**需要移动入口：桌面触发器确实只在 `md:` 以上可见。

    若哪天侧栏改成全视口可见，本用例会红 —— 那是**有意**的提醒：
    届时可重新评估是否还需要第二个入口。
    """
    cls = _enclosing_nav_class(_html(), 'id="test-conn-btn"')
    assert "hidden" in cls and "md:flex" in cls, (
        f"#test-conn-btn 所在的 <nav> class 为 {cls!r}，"
        "预期是 `hidden md:flex`（桌面专属）—— 前提变了就要重估移动入口"
    )


# ─── 3. 单一实现 ─────────────────────────────────────────────────


def test_connection_probe_has_a_single_implementation():
    """两个入口必须共用同一实现 —— 两份逻辑必然漂移。"""
    js = _js()
    assert js.count('fetch("/api/health/downstream")') == 1, (
        "settings.js 里 `/api/health/downstream` 的调用点不止一处 —— "
        "两个入口应共用同一函数，而不是各写一份"
    )
    assert "async function runConnectionTests(" in js, (
        "未找到共用的 runConnectionTests —— 重构可能被回退成内联 handler"
    )
    for flag in ("runConnectionTests(e.currentTarget, false)",
                 "runConnectionTests(e.currentTarget, true)"):
        assert flag in js, f"入口未绑定到共用实现：缺少 {flag}"
