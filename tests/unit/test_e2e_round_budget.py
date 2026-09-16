"""e2e 轮次预算护栏：必须是"基线 + 每页"，且大于实测耗时。

背景 —— 同一类缺陷**已经犯过两次**，两次都是"写死单值"：
  - 2026-09-04 rot 轮实跑 620s > 默认 600s，被误杀 20s（旋转证据其实完整）；
  - 2026-09-16 pdf 轮实跑 835s > 默认 600s，被判超时（该 job 之后正常进了
    ``review``，835s 是**真实耗时**而非卡死）。

固定值的病根是它同时**对小文档太紧、对大文档太松**。修法是把预算表达成
``基线 + 每页 × 页数``（与 ``core/watchdog.py`` 的产品侧阈值同一思路），
本文件把这个契约锁成机检。
"""
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import e2e_run  # noqa: E402

_DRIVER = _ROOT / "e2e_run.py"

# 冻结产物实测（docs/ADVERSARIAL_AUDIT.md §5 / 2026-09-16 本轮）
_MEASURED = {
    "pdf": (6, 835),
    "rot": (4, 1031),
    "real": (51, 1975),
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """预算相关的环境变量必须清干净 —— 否则测的是本机配置不是契约。"""
    for var in ("E2E_PDF_TIMEOUT", "E2E_ROT_TIMEOUT", "E2E_REAL_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)


def _with_pages(monkeypatch, pages: int):
    monkeypatch.setattr(e2e_run, "_pdf_page_count", lambda path: pages)


@pytest.mark.parametrize("kind", sorted(_MEASURED))
def test_budget_exceeds_measured_duration(kind, monkeypatch):
    """**核心回归**：预算必须大于实测耗时 —— 否则又会误杀（两次前科）。"""
    pages, measured = _MEASURED[kind]
    _with_pages(monkeypatch, pages)
    budget = e2e_run._round_budget_s("x.pdf", kind)
    assert budget > measured, (
        f"{kind} 轮预算 {budget}s ≤ 实测 {measured}s —— 真实长跑会被判超时"
    )


@pytest.mark.parametrize("kind", sorted(_MEASURED))
def test_budget_scales_with_pages(kind, monkeypatch):
    """页数变化必须真的改变预算（防"又变回常数"）。"""
    _with_pages(monkeypatch, 5)
    small = e2e_run._round_budget_s("x.pdf", kind)
    _with_pages(monkeypatch, 51)
    big = e2e_run._round_budget_s("x.pdf", kind)
    assert big > small


@pytest.mark.parametrize("kind", sorted(_MEASURED))
def test_env_override_wins(kind, monkeypatch):
    var = {"pdf": "E2E_PDF_TIMEOUT", "rot": "E2E_ROT_TIMEOUT",
           "real": "E2E_REAL_TIMEOUT"}[kind]
    monkeypatch.setenv(var, "1234")
    _with_pages(monkeypatch, 51)
    assert e2e_run._round_budget_s("x.pdf", kind) == 1234


@pytest.mark.parametrize("kind", sorted(_MEASURED))
def test_garbage_env_falls_back_to_formula(kind, monkeypatch):
    """非整数覆盖值不能把预算变成空/0（那会让整轮立刻误判超时）。"""
    var = {"pdf": "E2E_PDF_TIMEOUT", "rot": "E2E_ROT_TIMEOUT",
           "real": "E2E_REAL_TIMEOUT"}[kind]
    monkeypatch.setenv(var, "not-a-number")
    _with_pages(monkeypatch, 51)
    base, per_page = e2e_run._ROUND_BUDGETS[kind]
    assert e2e_run._round_budget_s("x.pdf", kind) == base + per_page * 51


@pytest.mark.parametrize("kind", sorted(_MEASURED))
def test_unreadable_page_count_falls_back_conservatively(kind, monkeypatch):
    """页数读不出来时按最大真实件估 —— 宁可多等，不可误杀。"""
    _with_pages(monkeypatch, 0)
    base, per_page = e2e_run._ROUND_BUDGETS[kind]
    assert e2e_run._round_budget_s("x.pdf", kind) == (
        base + per_page * e2e_run._UNKNOWN_PAGE_FALLBACK
    )


def test_no_hardcoded_default_budget_returns():
    """**静态护栏**：`E2E_*_TIMEOUT` 不得再带写死的默认值。

    旧写法是 ``_PDF_TIMEOUT_S = int(os.environ.get("E2E_PDF_TIMEOUT", "600"))``
    —— 默认值是固定单值，正是两次误杀的根源。允许的形式只有"读环境变量、
    没有默认值"（预算由公式给）。
    """
    src = _DRIVER.read_text(encoding="utf-8")
    bad = re.findall(r'"E2E_(?:PDF|ROT|REAL)_TIMEOUT"\s*,\s*"\s*\d+\s*"', src)
    assert not bad, (
        f"轮次预算又出现写死的默认值: {bad} —— 必须改成 _round_budget_s 的"
        f"基线+每页公式（见本文件 docstring 的两次前科）"
    )


def test_budget_kwargs_resolve_at_runtime():
    """`run_upload` 的预算实参不得引用已删除的名字。

    等价于一份静态 NameError 检查：删掉 ``REAL_TIMEOUT_S`` 这类常量后，
    调用点若还写着 ``timeout_s=REAL_TIMEOUT_S``，只有真跑到那一轮才会炸
    —— 而那一轮往往是最重的 real 轮（几十分钟后才暴露）。
    """
    import ast

    tree = ast.parse(_DRIVER.read_text(encoding="utf-8"))
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # 该函数作用域内可见的名字：形参（`run_rot(timeout_s=...)` 转传自己的
        # 形参是合法的）+ 模块级属性
        scope = {a.arg for a in list(fn.args.args) + list(fn.args.kwonlyargs)}
        for stmt in fn.body:
            if isinstance(stmt, ast.Assign):
                scope |= {t.id for t in stmt.targets if isinstance(t, ast.Name)}
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                scope.add(stmt.target.id)
        for node in ast.walk(fn):
            if (not isinstance(node, ast.Call)
                    or getattr(node.func, "id", None) != "run_upload"):
                continue
            for kw in node.keywords:
                if kw.arg not in ("timeout_s", "budget_kind"):
                    continue
                if isinstance(kw.value, ast.Name):
                    nm = kw.value.id
                    if nm not in scope and not hasattr(e2e_run, nm):
                        offenders.append(f"{fn.name}(): run_upload({kw.arg}={nm})")
    assert not offenders, (
        f"预算实参引用了不存在的名字（真跑到那一轮才炸）: {offenders}"
    )


def test_removed_constants_are_not_referenced():
    """旧常量一旦删除，源码里就不得再有引用（引用即 NameError）。"""
    src = _DRIVER.read_text(encoding="utf-8")
    for gone in ("REAL_TIMEOUT_S", "_PDF_TIMEOUT_S", "_ROT_TIMEOUT_S"):
        assert not re.search(rf"\b{gone}\b", src), (
            f"{gone} 已被移除，不应再被引用"
        )
