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
