"""护栏：e2e 的 `#127` 档位配色判据必须**有判别力**（Round 54）。

被测对象是 :mod:`tests.e2e_status_js` 里的**判据本身**（不是 status.js 的实现
—— 那是 `test_status_js.py` 的职责）。这里回答的问题是：
"这条判据能不能红？"

为什么要单独为"判据"写用例（Round 54 的实测教训）：
  修复前的判据是 `"bg-warning" in upload.js` + 一个匹配
  `if (...) return "bg-x"` 的正则。而配色早已抽到共享件 `status.js`，
  真实写法 `if (["review","done"].includes(st)) return "bg-success"` 会让
  `[^)]+` 在 `.includes(st)` 的 `)` 处截断 ⇒ **正则 0 命中**。
  于是判据两半分别**恒假**与**恒真**，对本文件下面每一条变异都**毫无反应**
  （详细对照见 `devlogs/_verify/mutate_e2e_127.py`），却会对**未变异**的
  源码报红 —— 那不是判据，是写死的假红。

判据与 §二十五/§二十六 的一致性：反向断言必须配**阳性对照**（本文件的
`test_positive_control_real_status_js_passes`），且变异预期落在**同一控制流**。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.e2e_status_js import (  # noqa: E402
    STATES, dot_semantics_problems, status_dot_classes,
)

STATUS_JS = (ROOT / "static" / "status.js").read_text(encoding="utf-8")


def _sub(text: str, old: str, new: str) -> str:
    """定点变异。替换**全部**出现 —— 只改第一处会留下其余副本，
    判据仍能读到 ⇒ 变异无效（Round 54 实测踩到的假阴性来源）。"""
    assert old in text, f"变异锚点不存在（用例与源码漂移）: {old[:60]!r}"
    return text.replace(old, new)


def _problems(status_js: str) -> list[str]:
    return dot_semantics_problems(status_dot_classes(status_js))


# ── 阳性对照 ─────────────────────────────────────────────────────────

def test_positive_control_real_status_js_passes():
    """未变异的真实 status.js 必须**通过** —— 否则判据是恒红，零判别力。"""
    assert _problems(STATUS_JS) == []


def test_states_are_all_covered():
    m = status_dot_classes(STATUS_JS)
    assert set(STATES) <= set(m), f"未覆盖: {set(STATES) - set(m)}"
    assert all(isinstance(v, str) and v for v in m.values()), m


# ── 变异：每一条都必须被捕获 ──────────────────────────────────────────

def test_catches_partial_review_shown_as_success():
    """#127 的原始回归形态：partial_review 与成功态同色。"""
    mutated = _sub(STATUS_JS,
                   'if (st === "partial_review") return "bg-warning";',
                   'if (st === "partial_review") return "bg-success";')
    problems = _problems(mutated)
    assert problems, "partial_review 改回成功色却未被捕获 —— 判据失效"
    assert "partial_review" in problems[0]


def test_catches_error_shown_as_success():
    mutated = _sub(STATUS_JS,
                   'if (st === "error") return "bg-destructive";',
                   'if (st === "error") return "bg-success";')
    problems = _problems(mutated)
    assert problems, "error 改回成功色却未被捕获"
    assert "error" in problems[0]


def test_catches_partial_review_collapsed_into_error():
    """用户须能区分"部分可复核"与"失败"。"""
    mutated = _sub(STATUS_JS,
                   'if (st === "partial_review") return "bg-warning";',
                   'if (st === "partial_review") return "bg-destructive";')
    problems = _problems(mutated)
    assert problems and "error" in problems[0], problems


def test_catches_review_and_done_diverging():
    """同一语义（review/done）不得两种颜色。"""
    mutated = _sub(STATUS_JS,
                   'if (["review", "done"].includes(st)) return "bg-success";',
                   'if (st === "review") return "bg-success";\n'
                   '    if (st === "done") return "bg-info";')
    problems = _problems(mutated)
    assert problems and "review/done" in problems[0], problems


def test_catches_missing_export():
    """导出里删掉 statusDotClass ⇒ 判据必须**红**（fail-closed），不能静默跳过。"""
    mutated = _sub(STATUS_JS, "    statusDotClass: statusDotClass,\n", "")
    with pytest.raises(AssertionError):
        status_dot_classes(mutated)


def test_catches_downgrade_to_two_tiers():
    """整体退回"成功 / 其它"两档（把两个非成功档合并）。"""
    mutated = _sub(
        STATUS_JS,
        '    if (st === "partial_review") return "bg-warning";\n'
        '    if (st === "error") return "bg-destructive";\n',
        "")
    problems = _problems(mutated)
    assert problems, "退回两档后 partial_review 与 error 同色，却未被捕获"


def test_unknown_state_does_not_crash_and_differs():
    """未知状态不得让判据崩掉（它取兜底档），且兜底不应等于成功档。"""
    m = status_dot_classes(STATUS_JS, states=("review", "__no_such_state__"))
    assert set(m) == {"review", "__no_such_state__"}
    assert m["__no_such_state__"] != m["review"]


# ── #127 可见性判据：必须与**文件位置无关**（2026-09-30 实测新增）──────
#
# 背景：`e2e_frozen.py` 的 13b/13c 段原本只在 `upload.js` / `review.js` 里
# 找 #127 修复标记。但消费逻辑随后被**纯模块化重构**拆走：
#   · `job.failed_pages`            → `static/upload-jobs.js:518`
#   · `["error","partial_review"].includes(st)` → `static/upload-jobs.js:583`
#   · `structured._error`           → `static/review-pageinfo.js:88`
# ⇒ 发版冒烟 `upload_js_127` / `review_js_127` **假红**（本轮实测 11 passed / 3 failed）。
# 判据要证明的是它自己注释里写明的**分发事实**——"要分发的那份东西带着修复"——
# 而不是"修复住在哪个文件里"。把位置写进判据，等于让一次纯重构把门禁变成噪声。

E2E_FROZEN = (ROOT / "tests" / "e2e_frozen.py").read_text(encoding="utf-8")


def _location_independence_problems(src: str) -> list[str]:
    """判据的"与位置无关"体检。返回问题清单（空 = 合格）。

    ⚠️ 本体检是**结构性**的（读源码文本），不是行为性的 —— 它没法在没有活服务端
    的情况下跑那条判据。故判据锚定**代码**（`assert not _empty`）而不是**文案**：
    改提示语不会误红，删掉守卫则必红。
    """
    problems = []
    if '_served_js(["/static/upload.js", "/static/upload-jobs.js"])' not in src:
        problems.append("upload 判据未同时扫 upload.js 与 upload-jobs.js")
    if '_served_js(["/static/review.js", "/static/review-pageinfo.js"])' not in src:
        problems.append("原因判据未同时扫 review.js 与 review-pageinfo.js")
    if "assert not _empty" not in src:
        problems.append("缺防空转守卫（取到空内容时会静默通过）")
    return problems


def test_positive_control_real_e2e_frozen_passes():
    """未变异的真实 `e2e_frozen.py` 必须合格 —— 否则体检本身恒红。"""
    assert _location_independence_problems(E2E_FROZEN) == []


def test_catches_single_file_judge_regression():
    """变异：把 upload 判据改回"只扫 upload.js"，体检必须红。"""
    mutated = _sub(
        E2E_FROZEN,
        '_served_js(["/static/upload.js", "/static/upload-jobs.js"])',
        'requests.get(f"{BASE}/static/upload.js", timeout=5).text',
    )
    problems = _location_independence_problems(mutated)
    assert problems and "upload" in problems[0], problems


def test_catches_missing_anti_noop_guard():
    """变异：删掉防空转守卫整行，体检必须红。

    （只改提示语、保留 `assert not _empty` 时**不应**红 —— 那是文案改动，不是回归。）
    """
    mutated = _sub(
        E2E_FROZEN,
        '        assert not _empty, f"静态资源取到空内容，标记断言无意义: {_empty}"\n',
        "",
    )
    problems = _location_independence_problems(mutated)
    assert problems and "防空转" in problems[0], problems


def test_rephrasing_the_message_does_not_false_red():
    """对照：仅改提示语文案，体检**不得**红（防"判据锚文案"这类假失败）。"""
    mutated = _sub(
        E2E_FROZEN,
        'f"静态资源取到空内容，标记断言无意义: {_empty}"',
        'f"served js empty: {_empty}"',
    )
    assert _location_independence_problems(mutated) == []


def _marker_hits(marker: str, sources: dict[str, str]) -> list[str]:
    return [name for name, text in sources.items() if marker in text]


def test_marker_hits_is_not_vacuous():
    """防空转：给一份不含标记的源码，`_marker_hits` 必须返回空。"""
    assert _marker_hits("job.failed_pages", {"a.js": "x"}) == []
    assert _marker_hits("job.failed_pages", {"a.js": "job.failed_pages"}) == ["a.js"]


@pytest.mark.parametrize("marker", ["job.failed_pages", "structured._error"])
def test_127_markers_still_exist_somewhere_in_static(marker):
    """#127 的修复必须**仍在源码树里**（否则产物断言红得没意义 —— 那才是真回归）。"""
    sources = {p.name: p.read_text(encoding="utf-8")
               for p in (ROOT / "static").glob("*.js")}
    hits = _marker_hits(marker, sources)
    assert hits, f"{marker} 在任何 static/*.js 里都找不到 ⇒ #127 修复被删了？"
