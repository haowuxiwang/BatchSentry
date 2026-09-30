"""文档 ↔ 代码一致性机检：`CLAUDE.md` 里的「release gate 清单」不得与
`scripts/release_gate.py::run_all()` 实际跑的检查项脱节。

## 为什么需要这个文件

R66 对抗性审查发现：`CLAUDE.md` 的 gate 清单写成 **"9 items"**，而
`run_all()` 实际跑 **11** 项 —— 漏掉的是 `dependency_vulns` 与 `runtime_eol`
（B11-2 加的供应链 / 运行时检查）。

这两项**恰恰是**最有理由被写进文档的两项：Round 58 正是靠它们查出
**`pip-audit` 50 条公告 / 26 条 CVE**、以及**产物内 Electron 33 EOL 17 个月**。
把"我们依赖的东西还安不安全"从文档里写没了，等于让后来者以为没人在看这一面。

**这是"只靠注释约束的不变式等于没有约束"的又一例**：文档与代码之间没有任何
强制点，于是文档悄悄落后 2 项、无人发现（`grep` 不报、门禁不看、用例不红）。

## 锁住的不变式

A. **条数一致**：文档声明的 `N items` == `run_all()` 实际调用的检查函数个数；
B. **名字不漏**：`release_gate.py` 里所有 `CheckResult("<name>", ...)` 的
   `<name>` 都必须在文档清单里出现（漏一个即红）；
C. **不空转**：文档块与代码两边都必须真的解析出内容（解析失败 = 空集 = 假绿）；
D. **无悬空调用**：`run_all()` 里调用的每个 `check_*` 都必须在模块里**有定义**
   （防重命名/笔误让某项检查**静默消失** —— 检查消失不会让任何用例变红）。

## 口径

只做 **AST 静态解析**，不 import `release_gate`（避免副作用、也不受其依赖影响）。
文档名取的是 `CheckResult` 的 **name 字段**（如 `no_build_outputs`），
**不是**函数名（`check_no_build_outputs_tracked`）—— 两者不同，别混。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_RG = REPO / "scripts" / "release_gate.py"
_CLAUDE = REPO / "CLAUDE.md"

# 文档清单块的起始行形态：`# 11 items = 10 structural + tests_coverage, ...`
_ITEMS_RE = re.compile(r"^#\s*(\d+)\s+items\b")


def _parse_gate_module() -> tuple[list[str], set[str], set[str]]:
    """返回 (run_all 调用的 check_* 列表, CheckResult 的 name 字面量集合, 模块内函数名集合)。"""
    tree = ast.parse(_RG.read_text(encoding="utf-8"))

    run_all = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == "run_all"),
        None,
    )
    assert run_all is not None, "release_gate.py 里找不到 run_all() —— 编排入口改名了？"

    called = [
        c.func.id
        for c in ast.walk(run_all)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Name)
        and c.func.id.startswith("check_")
    ]

    emitted = {
        n.args[0].value
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "CheckResult"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and isinstance(n.args[0].value, str)
    }

    defined = {
        n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return called, emitted, defined


def _parse_doc_inventory() -> tuple[int, str]:
    """从 CLAUDE.md 取 (声明的条数, 清单块原文)。块 = items 行起、到首个 `⚠️` 行或非注释行止。"""
    lines = _CLAUDE.read_text(encoding="utf-8").splitlines()
    for i, ln in enumerate(lines):
        m = _ITEMS_RE.match(ln)
        if not m:
            continue
        block: list[str] = []
        for ln2 in lines[i:]:
            if not ln2.startswith("#") or "⚠️" in ln2:
                break
            block.append(ln2)
        return int(m.group(1)), "\n".join(block)
    raise AssertionError(
        "CLAUDE.md 里找不到 `# <N> items` 形态的 release gate 清单块 —— "
        "是本检查的锚点失效了，不是文档没问题"
    )


# 解析一次，供各用例复用（纯读文件 + AST，无副作用）。
_CALLED, _EMITTED, _DEFINED = _parse_gate_module()
_DECLARED, _BLOCK = _parse_doc_inventory()


class TestAntiVacuity:
    """先把"提取器没坏"钉住 —— 否则下面的断言可能只是空断言。"""

    def test_gate_module_parsed_something(self):
        assert len(_CALLED) >= 10, f"run_all 只解析出 {len(_CALLED)} 个 check_* 调用：{_CALLED}"
        assert len(_EMITTED) >= 10, f"只解析出 {len(_EMITTED)} 个 CheckResult 名字：{sorted(_EMITTED)}"

    def test_doc_block_is_non_trivial(self):
        assert _DECLARED >= 10, f"文档声明条数 {_DECLARED} 太小，疑似锚点抓错了行"
        # 清单块必须真的含检查名，而不是只抓到一行标题
        assert sum(1 for n in _EMITTED if n in _BLOCK) >= 10, (
            "文档清单块里几乎找不到检查名 —— 块边界抓错了"
        )


class TestInventoryMatchesCode:
    def test_declared_count_equals_actual_checks(self):
        """R66 的真缺陷就是这条：文档写 9、实际 11。"""
        assert _DECLARED == len(_CALLED), (
            f"CLAUDE.md 声明 {_DECLARED} 项，而 run_all() 实际调用 {len(_CALLED)} 项：\n"
            f"  实际 = {_CALLED}\n"
            "  → 文档漏写或多写了检查项，请同步 CLAUDE.md 的 gate 清单"
        )

    def test_every_emitted_check_name_is_documented(self):
        missing = sorted(n for n in _EMITTED if n not in _BLOCK)
        assert not missing, (
            f"这些检查名在 release_gate.py 里有 CheckResult 发射，却没写进 CLAUDE.md：{missing}"
        )

    def test_every_check_called_by_run_all_is_defined(self):
        """防"检查静默消失"：调用点存在、定义却被删/改名。"""
        dangling = sorted(n for n in _CALLED if n not in _DEFINED)
        assert not dangling, (
            f"run_all() 调用了未定义的函数：{dangling} —— 该项检查会直接 NameError 或已消失"
        )
