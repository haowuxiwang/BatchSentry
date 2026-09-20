"""B3-4 护栏：写路径的「重建 + 降级物化」只允许有**一个**实现点。

背景（B3-4，Round 46 发现）：`core/rules/llm_finding_guard.py::apply_review`
曾是**死代码**（生产侧零消费点），而两个真正的调用方各自手抄了同一段
"重建 findings + 物化降级 severity"逻辑，且已与其**漂移**
（`apply_review` 对 description 做了 `.rstrip()`，两处调用方没有）。
这与 `_get_analyzed_pages`（#131）、`is_congestion_error`（#120）踩过的
"同一段逻辑抄多份、迟早不一致"是同型坑 —— 本护栏用**结构**断言把它钉死。

三道断言：
  (a) 正向： `core/pipeline/stage2.py` / `stage3.py` 必须调用 `apply_review`；
  (b) 反向： `core/pipeline/` 下不得直接调用底层引擎 `review_llm_findings`
             （否则等于绕过唯一入口）；
  (c) 反向： 物化降级的标记 token ``to_severity`` 不得出现在 `core/pipeline/`。

⚠️ **正向对照**：`to_severity` 必须确实存在于唯一实现点里，否则 (c) 是
**空断言**（"扫不到"不等于"没问题" —— 项目在护栏上踩过这类坑）。
⚠️ 断言用 AST（`ast.Constant` 精确等值）而非文本搜索：pipeline 里
`｜` 另有合法用途（错误文案），文本搜索会产生假红。

判别力（变异验证）：把重建逻辑抄回 stage2 ⇒ (a) 与 (c) 都必须变红。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PIPELINE_DIR = ROOT / "core" / "pipeline"
GUARD_SRC = ROOT / "core" / "rules" / "llm_finding_guard.py"

# 物化降级的标记 token：它只属于 llm_finding_guard（唯一实现点）
_MATERIALIZE_TOKEN = "to_severity"
# 唯一入口 / 底层引擎
_ENTRY = "apply_review"
_LOW_LEVEL = "review_llm_findings"


def _read(p: Path) -> str:
    # ⚠️ 项目硬约束：源码可能带 BOM ⇒ 一律 utf-8-sig
    return p.read_text(encoding="utf-8-sig")


def _parse(p: Path) -> ast.Module:
    return ast.parse(_read(p))


def _called_names(tree: ast.Module) -> set[str]:
    """所有调用点被调用的名字（`f()` 与 `m.f()` 都取末段）。"""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _string_constants(tree: ast.Module) -> set[str]:
    """源码里出现过的**字符串字面量精确值**（评论/文档串不计入精确等值）。"""
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _pipeline_files() -> list[Path]:
    return sorted(p for p in PIPELINE_DIR.rglob("*.py")
                  if "__pycache__" not in p.parts)


# ── 前置：护栏必须真的扫到目标 + 正向对照 ────────────────────────────────

def test_guard_scan_targets_exist():
    """前提：扫描面存在，且两个调用方都在 —— 否则下面的断言全是空转。"""
    files = _pipeline_files()
    assert len(files) >= 3, f"core/pipeline 下只扫到 {len(files)} 个文件，扫描面可疑"
    for name in ("stage2.py", "stage3.py"):
        assert (PIPELINE_DIR / name).is_file(), f"缺 core/pipeline/{name}，护栏前提不成立"
    assert GUARD_SRC.is_file(), "缺唯一实现点 core/rules/llm_finding_guard.py"


def test_positive_control_token_exists_in_source_of_truth():
    """正向对照：标记 token 必须存在于唯一实现点。

    否则 (c) 会**恒真**（"core/pipeline 里没有它"只是因为全仓都没有它），
    护栏退化成空断言。若将来重构掉了这个 token，本测试会先红，提醒同步更新。
    """
    found = _string_constants(_parse(GUARD_SRC))
    assert _MATERIALIZE_TOKEN in found, (
        f"{GUARD_SRC.name} 里找不到字符串常量 {_MATERIALIZE_TOKEN!r} —— "
        "反向断言失去对照对象，护栏已退化，须同步更新本测试"
    )


# ── (a) 正向：写路径必须调用唯一入口 ────────────────────────────────────

@pytest.mark.parametrize("name", ["stage2.py", "stage3.py"])
def test_stage_calls_single_entry(name):
    calls = _called_names(_parse(PIPELINE_DIR / name))
    assert _ENTRY in calls, (
        f"core/pipeline/{name} 未调用 {_ENTRY} —— 写路径必须经唯一入口（B3-4），"
        "否则「重建 + 降级物化」会被再次复制"
    )


# ── (b) 反向：不得绕过唯一入口直接调底层引擎 ───────────────────────────

def test_pipeline_never_calls_low_level_engine():
    offenders = [p.name for p in _pipeline_files()
                 if _LOW_LEVEL in _called_names(_parse(p))]
    assert not offenders, (
        f"{offenders} 直接调用了 {_LOW_LEVEL} —— 写路径必须走 {_ENTRY}（B3-4）；"
        "直接调底层会重新引入「同一段逻辑抄多份、迟早漂移」的同型缺陷"
    )


# ── (c) 反向：不得在调用方物化降级 ─────────────────────────────────────

def test_pipeline_never_materializes_downgrade():
    offenders = [p.name for p in _pipeline_files()
                 if _MATERIALIZE_TOKEN in _string_constants(_parse(p))]
    assert not offenders, (
        f"{offenders} 出现 {_MATERIALIZE_TOKEN!r} —— 疑似把「重建 + 降级物化」"
        f"抄回了调用方（B3-4 已统一到 {_ENTRY}）"
    )
