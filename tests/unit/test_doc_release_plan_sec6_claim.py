# -*- coding: utf-8 -*-
"""`docs/RELEASE_READINESS_PLAN.md` §SEC-6「模板注入」行的**事实绑定**护栏
（R78 第十八批，对抗性审查）。

由来：该行原写「`|safe`/`Markup(`/`innerHTML` **零命中** → ✅ 已复核，无需动作」。
实测**相反**：`innerHTML` 在 `static/` 里有 **46 处**（`Markup(` 4 处）。
原因是前端在 **R63/R65 拆模块**之后才出现 `innerHTML` —— 该行是"文档复述代码事实、
却无人绑定到真值源"的实例（本项目的惯犯形态）。

⚠️ 本护栏**只绑事实，不判安全**：它证明"文档写的计数与代码一致"，**不**证明
`innerHTML` 的插值都经过了转义。那件（`esc()` 纪律的机检）**尚未做**，已登记 0-19 ——
不得因为本文件绿了就把 §SEC-6 读成"注入面已验"。
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_PLAN = REPO / "docs" / "RELEASE_READINESS_PLAN.md"

#: 与文档所写的口径**逐字一致**：`static/**/*.js` + `templates/**/*.html` + `main.py`。
#: ⚠️ 刻意**不**走 `git ls-files` —— 本护栏要在"没有 `.git` 的树"里也能跑
#: （变异 harness 的临时树就是这种；首版用 git ⇒ 临时树里直接抛错 ⇒ 变异
#: "全部 CAUGHT"其实是**假象**，而基线也红）。判据要能脱离 VCS 自证。
_SCAN_GLOBS = (("static", "*.js"), ("templates", "*.html"))
_SCAN_FILES = ("main.py",)
_TOKENS = ("innerHTML", "Markup(", "|safe")


def _scanned_files() -> list[Path]:
    out: list[Path] = []
    for d, pat in _SCAN_GLOBS:
        out += sorted((REPO / d).rglob(pat))
    out += [REPO / f for f in _SCAN_FILES]
    return [p for p in out if p.is_file()]


def _counts() -> dict[str, int]:
    files = _scanned_files()
    return {t: sum(p.read_text(encoding="utf-8", errors="replace").count(t)
                   for p in files) for t in _TOKENS}


def _sec6_row() -> str:
    for line in _PLAN.read_text(encoding="utf-8").splitlines():
        if line.startswith("|") and "模板注入" in line:
            return line
    raise AssertionError("§SEC-6 里找不到『模板注入』那一行 —— 锚点失效（护栏会恒真）")


# ── 0. 反空转：扫描器必须真的扫到东西 ────────────────────────────────

def test_scanner_is_not_vacuous():
    files = _scanned_files()
    assert len(files) > 10, f"只扫到 {len(files)} 个文件 —— 扫描口径有问题"
    c = _counts()
    assert c["innerHTML"] > 0, "扫描器扫不到任何 innerHTML ⇒ 下面的断言全是空的"
    assert c["|safe"] == 0, "`|safe` 实测非 0 ⇒ 文档里那条 0 的声明要重写"


# ── 1. 文档里的三个计数必须与实测一致 ────────────────────────────────

def test_row_states_the_real_counts():
    """计数写在文档里，就必须与代码一致 —— 否则又是"复述即腐坏"。"""
    row = _sec6_row()
    for tok, n in _counts().items():
        assert f"**{n} 处**" in row, (
            f"§SEC-6『模板注入』行没有写 `{tok}` 的实测计数 **{n} 处**"
            f"（实测变了就必须改文档，反之亦然）"
        )


# ── 2. 禁止原措辞回流 ───────────────────────────────────────────────

def test_row_never_restores_the_false_phrase():
    """「零命中」是被实测否证的原措辞 —— 回流即意味着文档又与代码相反。"""
    row = _sec6_row()
    assert "零命中" not in row, (
        "§SEC-6『模板注入』行又出现了「零命中」—— 实测 `innerHTML` 有 46 处，"
        "该措辞已被否证"
    )
