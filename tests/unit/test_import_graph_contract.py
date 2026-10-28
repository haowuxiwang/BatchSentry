"""导入图契约：**顶层无环** + **分层方向** + **`core.pipeline` 扇出不得静默变大**。

## 为什么有这个文件（#22 的落地形态）

报告 §3.2 的标题是"190 处函数级 import 不是设计，是循环依赖的补偿"。本轮用
`devlogs/_verify/probe_pipeline_hoistability.py` 做了**可证伪的核验**：

  · 把 `A` 里的函数级 `import B` 提到顶层，**当且仅当**在顶层导入图里 `B` 能到达
    `A` 时才会成环 ⇒ "承重"与"只是风格"可以精确分开。
  · 实测：全仓 1013 处本仓模块的函数级 import 里，**只有 25 处承重**
    （24 处指向 `core.pipeline`、1 处指向 `core.rules`），其余 988 处不成环。
  · 全仓顶层导入图当时**只有 1 个真环**：`core.kb ⇄ core.kb.retriever`
    —— 与 `core.pipeline` 扇出**无关**，本轮已修（`retriever.py` 改为直连子模块）。

⇒ 原计划"删掉 `__init__.py` 里 34 个 re-export"被**否证**：它修不了任何环，
   却要改 **217 处** patch 锚点（12 个符号，实测），收益为负。
   本文件替代原计划，把**真正有约束力的三条不变式**钉住。

## 钉住的三条

1. **顶层导入图必须无环**（SCC 全部 size==1）。这一条同时把
   `core/pipeline/*.py` 里那 24 处**调用期** `from core.pipeline import X` 变成
   "承重的"：谁把它提到顶层，就会形成 `包 ⇄ 子模块` 环，本文件立刻变红。
2. **分层方向**：`core` 不得在顶层依赖 `api`（当前 0 条）；`db` 是叶子
   （不依赖 `core`/`api`，当前 0 条）；`api → core` 必须存在（证明扫描器确实
   在"两个方向"都工作，否则第 1 条可能是"什么都没扫到"）。
3. **`core.pipeline` 的 re-export 面是显式清单**，新增/改名必须显式改本文件
   —— 扇出**不能静默变大**（这正是 §3.2 真正要防的东西）。

口径说明：只看**顶层**语句里的 import（函数体内的不算，那才是"延迟导入"）；
`if TYPE_CHECKING:` 块不算（不执行）；`try:`/`if:` 块算（会执行）；
不含 `tests/`（测试不是产品导入图的一部分）。
"""
from __future__ import annotations

import ast
import sys
import warnings
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# 参与导入图的产品目录（**不含 tests**：测试模块不是产品导入图的一部分）
GRAPH_ROOTS = ["core", "api", "db", "scripts"]
GRAPH_TOP_FILES = ["main.py", "server.py", "config.py"]
SKIP_DIRS = {"__pycache__", ".git", "node_modules", "output", "devlogs", ".venv", "venv"}


# ─────────────────────────────────────────────────────────────────────
# 扫描器（纯函数，便于用合成源码自测）
# ─────────────────────────────────────────────────────────────────────
def _read(p: Path) -> str:
    """`utf-8-sig`：仓库里有 5 个带 BOM 的文件，用 `utf-8` 读会让 `ast.parse` 抛错，
    而"抛错就跳过"会让图**静默缺边**（缺边 ⇒ 假的无环）。"""
    return p.read_text(encoding="utf-8-sig")


def _module_of(p: Path) -> str:
    rel = p.relative_to(REPO).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _resolve_relative(module: str | None, level: int, cur: str, is_pkg: bool) -> str | None:
    """`from .state import X` → `core.pipeline.state`。"""
    if level == 0:
        return module
    base = cur.split(".")
    if not is_pkg:
        base = base[:-1]
    keep = len(base) - (level - 1)
    if keep <= 0:
        return module
    head = ".".join(base[:keep])
    return f"{head}.{module}" if module else head


def _top_level_imports(src: str, cur: str, is_pkg: bool, local: set[str]) -> set[str]:
    """返回该源码**顶层会执行**的 import 目标模块名（只保留本仓模块）。"""
    tree = ast.parse(src)
    found: set[str] = set()

    def visit(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # ⚠️ 真正的机制是**下面只对 If/Try 递归** —— `visit` 从不进入
                # 函数/类体，所以函数体内的 import 天然扫不到。这个 `continue`
                # 只是双保险（变异 M12 实测：只关掉它，函数体 import 照样扫不到；
                # 必须**显式加** `visit(node.body)` 才能复现"下探函数体"的缺陷）。
                # 谁要改成递归遍历，务必先读 `test_scanner_ignores_function_body_*`。
                continue
            if isinstance(node, ast.If):
                if _is_type_checking(node.test):
                    continue  # 不执行
                visit(node.body)
                visit(node.orelse)
                continue
            if isinstance(node, ast.Try):
                visit(node.body)
                for h in node.handlers:
                    visit(h.body)
                visit(node.orelse)
                visit(node.finalbody)
                continue
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom):
                if node.module is None and node.level > 0:
                    # `from . import X`（没有 module 名）要分两种情况，**不能一律算成父包**：
                    #
                    #   · X 是本仓已知的**子模块** ⇒ 真正的目标是 `pkg.X`。导入系统会去
                    #     `sys.modules` 取/新建这个子模块，父包处于"半初始化"也不影响它。
                    #     若在这里额外记一条"子模块 → 父包"的边，**每个**包都会成环
                    #     （子模块依赖父包是 Python 的普遍事实），护栏立刻退化成噪音。
                    #   · X 只是父包 `__init__` 里 re-export 的**符号** ⇒ 目标只能是父包：
                    #     `from . import db_lock` 会先 `getattr(pkg, "db_lock")`，此时父包
                    #     可能才执行到一半、该符号尚未定义，再去 import 子模块 `pkg.db_lock`
                    #     就 ModuleNotFoundError。**这是真隐患**，必须记成指向父包的边，
                    #     好让"包 ⇄ 子模块"环被下面的无环用例抓住。
                    head = _resolve_relative(None, node.level, cur, is_pkg)
                    if head:
                        for a in node.names:
                            sub = f"{head}.{a.name}"
                            targets.append(sub if sub in local else head)
                else:
                    m = _resolve_relative(node.module, node.level, cur, is_pkg)
                    if m:
                        targets.append(m)
            elif isinstance(node, ast.Import):
                targets += [a.name for a in node.names]
            for t in targets:
                if t.split(".")[0] in local:
                    found.add(t)

    visit(tree.body)
    return found


def _iter_py() -> list[Path]:
    out: list[Path] = []
    for root in GRAPH_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        out += [
            p for p in base.rglob("*.py")
            if not any(part in SKIP_DIRS for part in p.parts)
        ]
    for f in GRAPH_TOP_FILES:
        p = REPO / f
        if p.exists():
            out.append(p)
    return sorted(set(out))


def _build_graph() -> tuple[dict[str, set[str]], set[str]]:
    files = _iter_py()
    local = {_module_of(p) for p in files}
    local |= {m.split(".")[0] for m in local}
    graph: dict[str, set[str]] = defaultdict(set)
    for p in files:
        cur = _module_of(p)
        graph[cur] |= _top_level_imports(_read(p), cur, p.name == "__init__.py", local)
    return graph, local


def _scc_of(edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan，只返回 size>1 的强连通分量。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = [0]
    nodes = set(edges) | {t for ts in edges.values() for t in ts}

    def strongconnect(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in sorted(edges.get(v, ())):
            if w not in index:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(sorted(comp))

    old = sys.getrecursionlimit()
    sys.setrecursionlimit(20000)
    try:
        for v in sorted(nodes):
            if v not in index:
                strongconnect(v)
    finally:
        sys.setrecursionlimit(old)
    return sorted(out)


# ─────────────────────────────────────────────────────────────────────
# 0. 防空转：先证明扫描器/检测器真的在工作
# ─────────────────────────────────────────────────────────────────────
class TestHarnessIsNotVacuous:
    def test_scc_detector_finds_a_synthetic_cycle(self):
        """检测器自证：**不能**用"仓库里应当有环"当自证 —— 环被修好之后，
        那种断言会把"检测器坏了"与"环修好了"混为一谈。故用合成图。"""
        assert _scc_of({"a": {"b"}, "b": {"a"}, "c": {"d"}, "d": set()}) == [["a", "b"]]
        assert _scc_of({"x": {"y"}, "y": {"z"}, "z": set()}) == []

    def test_scanner_ignores_function_body_and_type_checking(self):
        """扫描器自证：函数体内与 `TYPE_CHECKING` 块的 import 都**不算**顶层；
        `try:` / 普通 `if:` 块**算**（它们会执行）。"""
        src = (
            "from __future__ import annotations\n"
            "import typing\n"
            "from typing import TYPE_CHECKING\n"
            "from core.aaa import x\n"
            "if TYPE_CHECKING:\n"
            "    from core.hidden import y\n"
            "try:\n"
            "    from core.tried import z\n"
            "except ImportError:\n"
            "    pass\n"
            "def f():\n"
            "    from core.lazy import w\n"
            "    return w\n"
        )
        local = {"core", "core.aaa", "core.hidden", "core.tried", "core.lazy"}
        got = _top_level_imports(src, "core.mod", False, local)
        assert got == {"core.aaa", "core.tried"}, f"扫描口径错了：{got}"

    def test_scanner_resolves_relative_imports(self):
        """`from .state import x` 与 `from . import state` 都应落到**子模块**上。

        ⚠️ 本仓 `core/ api/ db/ scripts/` 里**一处相对导入都没有**（实测全为绝对
        导入），所以这个分支**只由本用例钉住** —— 它是纯防御性的，一旦口径漂移，
        没有任何真实代码会把它暴露出来。故这里把两种形态都断言清楚。
        """
        local = {"core", "core.pipeline", "core.pipeline.state"}
        got = _top_level_imports(
            "from .state import x\nfrom . import state\n",
            "core.pipeline.engine",
            False,
            local,
        )
        assert got == {"core.pipeline.state"}, f"相对导入解析错了：{got}"

    def test_scanner_maps_attribute_only_relative_import_to_the_package(self):
        """`from . import <非子模块>` 必须落到**父包**上（这是真隐患，不是噪音）。

        若把这种写法也解析成子模块 `core.pipeline.db_lock`，那条"包 ⇄ 子模块"环
        就永远抓不到；若把 `from . import state`（子模块）也算成父包，则每个包都成环。
        本用例钉住"两种情况必须区分对待"。
        """
        local = {"core", "core.pipeline", "core.pipeline.state"}
        got = _top_level_imports(
            "from . import db_lock\n",
            "core.pipeline.engine",
            False,
            local,
        )
        assert got == {"core.pipeline"}, (
            f"非子模块的 `from . import X` 应指向父包，得到：{got}"
        )

    def test_graph_is_actually_populated(self):
        """图必须"够大"。否则下面的"无环"只是"什么都没扫到"。"""
        graph, local = _build_graph()
        edges = sum(len(v) for v in graph.values())
        assert len(graph) >= 25, f"只扫到 {len(graph)} 个模块 —— 扫描器坏了"
        assert edges >= 50, f"只扫到 {edges} 条边 —— 扫描器坏了"
        assert "core.pipeline" in graph, "core.pipeline 不在图里 —— 根目录/命名口径变了"


# ─────────────────────────────────────────────────────────────────────
# 0b. 前置条件：每个产品源都必须**无警告地**解析
# ─────────────────────────────────────────────────────────────────────
class TestSourcesParseCleanly:
    def test_no_invalid_escape_sequences(self):
        """非法转义序列（如 docstring 里的 ``\\``` ）必须为 0。

        为什么要在这里守：上面的导入图是"把每个文件 `ast.parse` 一遍"得来的，
        **解析失败 = 静默缺边 = 假的无环**。而 `\\`` 这类非法转义在 Python 3.11
        只是 DeprecationWarning（照常解析成功），到 3.12+ 变 SyntaxWarning，
        将来会变成错误 —— 也就是说它是一个"现在全绿、升级即炸"的定时器。

        本用例是上一轮真实缺陷的回归护栏：`core/md_render.py` 的模块 docstring
        里曾写 `` ``\\`行内代码\\``` ``（既触发 Python 警告，Markdown 也是错的
        —— 代码段里的反斜杠是字面量，正确写法是 `` `` `行内代码` `` ``）。
        """
        offenders: list[str] = []
        for p in _iter_py():
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                try:
                    compile(_read(p), str(p), "exec", ast.PyCF_ONLY_AST)
                except SyntaxError as e:  # pragma: no cover - 真出现就是硬失败
                    offenders.append(f"{p.relative_to(REPO)}: SyntaxError {e}")
                    continue
            offenders += [
                f"{p.relative_to(REPO)}: {w.message}"
                for w in caught
                if "escape" in str(w.message)
            ]
        assert offenders == [], (
            "产品源里有非法转义序列（会在 Python 3.12+ 变成 SyntaxWarning）：\n  "
            + "\n  ".join(offenders)
        )


# ─────────────────────────────────────────────────────────────────────
# 1. 顶层无环
# ─────────────────────────────────────────────────────────────────────
class TestNoTopLevelImportCycles:
    def test_top_level_import_graph_is_acyclic(self):
        """**本文件的核心不变式**：全仓顶层导入图必须是 DAG。

        为什么重要：`core/pipeline/*.py` 里那 24 处调用期
        `from core.pipeline import X`（`db_lock` / `_is_cancelled` / …）之所以
        必须留在调用期，就是因为提到顶层会形成 `包 ⇄ 子模块` 环。有人把它们
        "整理"到顶层时，本用例会立刻变红 —— 而不是等到某天某个导入顺序变了
        才炸（CPython 对部分初始化模块有 sys.modules 回退，所以这类错误
        **平时是静默的**）。
        """
        graph, _ = _build_graph()
        cycles = _scc_of(graph)
        assert cycles == [], (
            "顶层导入图出现环（会形成「顺序恰好成立」的脆弱导入）：\n  "
            + "\n  ".join(" ⇄ ".join(c) for c in cycles)
            + "\n修法：把其中一个方向改成「直连子模块」"
            "（`from pkg.sub import X`）或改到调用期（函数体内 import）。"
        )


# ─────────────────────────────────────────────────────────────────────
# 2. 分层方向
# ─────────────────────────────────────────────────────────────────────
class TestLayering:
    def test_core_does_not_depend_on_api(self):
        """`core` 是领域层，不得在顶层依赖 `api`（传输/编排层）。"""
        graph, _ = _build_graph()
        bad = sorted(
            f"{m} -> {t}"
            for m, ts in graph.items()
            for t in ts
            if m.split(".")[0] == "core" and t.split(".")[0] == "api"
        )
        assert bad == [], "core 顶层依赖了 api（分层倒置）：\n  " + "\n  ".join(bad)

    def test_db_is_a_leaf(self):
        """`db` 只依赖自己（+ 第三方），不得反向依赖 `core`/`api`。"""
        graph, _ = _build_graph()
        bad = sorted(
            f"{m} -> {t}"
            for m, ts in graph.items()
            for t in ts
            if m.split(".")[0] == "db" and t.split(".")[0] in ("core", "api")
        )
        assert bad == [], "db 顶层依赖了 core/api：\n  " + "\n  ".join(bad)

    def test_api_does_depend_on_core(self):
        """反向对照：`api → core` 必须存在。

        否则上面的"0 条"可能只是"扫描器根本没看到任何边"（空断言的经典形态）。
        """
        graph, _ = _build_graph()
        n = sum(
            1
            for m, ts in graph.items()
            for t in ts
            if m.split(".")[0] == "api" and t.split(".")[0] == "core"
        )
        assert n >= 5, f"只扫到 {n} 条 api→core 边 —— 扫描器可疑，上面的 0 条不可信"


# ─────────────────────────────────────────────────────────────────────
# 3. `core.pipeline` 扇出：显式清单，不得静默变大
# ─────────────────────────────────────────────────────────────────────
# 当前 re-export 面（39 个）。**新增/改名必须显式改这里** —— 这条就是 §3.2
# 真正要防的"扇出悄悄变大"。改动时请先问：调用方能否直接 import 子模块？
_EXPORT_SURFACE = {
    "asyncio": "asyncio",
    "config": "config",
    "analyze_page": "core.page_analyzer",
    "AnalysisCancelled": "core.page_analyzer",
    "analyze_cross_page": "core.cross_page_analyzer",
    "_pipeline_locks": "core.pipeline.locks",
    "_pipeline_tasks": "core.pipeline.locks",
    "_locks_guard": "core.pipeline.locks",
    "db_lock": "core.pipeline.locks",
    "_SLICE_QUEUE_TIMEOUT": "core.pipeline.locks",
    "live_tasks_for": "core.pipeline.locks",
    "register_pipeline_task": "core.pipeline.locks",
    "unregister_pipeline_task": "core.pipeline.locks",
    "InvalidTransitionError": "core.pipeline.state",
    "VALID_TRANSITIONS": "core.pipeline.state",
    "_STUCK_STATUSES": "core.pipeline.state",
    "_audit_log": "core.pipeline.state",
    "_transition_status_unlocked": "core.pipeline.state",
    "transition_status": "core.pipeline.state",
    "recover_stuck_jobs": "core.pipeline.state",
    "_is_cancelled": "core.pipeline.state",
    "_update_ocr_progress": "core.pipeline.state",
    "_update_self_heal_progress": "core.pipeline.state",
    "_get_ocr_backend": "core.pipeline.ocr_support",
    "_get_ocr_chain": "core.pipeline.ocr_support",
    "_sanitize_ocr_text": "core.pipeline.ocr_support",
    "_pdf_page_count": "core.pipeline.ocr_support",
    "_run_ocr_with_failover": "core.pipeline.ocr_support",
    "_self_heal_empty_pages": "core.pipeline.self_heal",
    "_run_stage1_full": "core.pipeline.stage1",
    "_get_existing_pages": "core.pipeline.stage1",
    "_run_stage2_analysis": "core.pipeline.stage2",
    "_analyze_one": "core.pipeline.stage2",
    "_get_analyzed_pages": "core.pipeline.stage2",
    "_run_stage3_cross_analysis": "core.pipeline.stage3",
    "launch_pipeline": "core.pipeline.engine",
    "run_pipeline": "core.pipeline.engine",
    "_run_pipeline_impl": "core.pipeline.engine",
    "_run_sliced_stage1_2": "core.pipeline.engine",
}


def _declared_surface() -> dict[str, str]:
    """从 `core/pipeline/__init__.py` 顶层解析出 {名字: 来源模块}。"""
    init = REPO / "core" / "pipeline" / "__init__.py"
    tree = ast.parse(_read(init))
    surface: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                surface[a.asname or a.name] = node.module
        elif isinstance(node, ast.Import):
            for a in node.names:
                surface[a.asname or a.name.split(".")[0]] = a.name
    return surface


class TestPipelineFanoutBudget:
    def test_export_surface_matches_the_pinned_list(self):
        declared = _declared_surface()
        added = sorted(set(declared) - set(_EXPORT_SURFACE))
        removed = sorted(set(_EXPORT_SURFACE) - set(declared))
        assert not added and not removed, (
            f"`core.pipeline` 的 re-export 面变了。\n"
            f"  新增：{added}\n  移除：{removed}\n"
            f"新增 re-export 会让扇出变大：请先确认调用方**不能**直接 import 子模块；"
            f"确需导出时，显式更新本文件的 _EXPORT_SURFACE（并在报告中记录理由）。"
        )

    def test_exported_names_resolve_to_their_declared_home(self):
        """每个导出名的**来源模块**也要钉住 —— 否则可以把某符号"搬家"后
        仍留在清单里，清单就退化成"名字集合"而不是"契约"。"""
        declared = _declared_surface()
        mismatched = {
            k: (v, _EXPORT_SURFACE[k])
            for k, v in declared.items()
            if k in _EXPORT_SURFACE and v != _EXPORT_SURFACE[k]
        }
        assert not mismatched, f"导出名的来源模块变了：{mismatched}"

    def test_all_exported_names_are_actually_importable(self):
        """清单必须与运行期一致（防"清单里有、实际导不出来"的漂移）。"""
        import core.pipeline as pkg

        missing = sorted(n for n in _EXPORT_SURFACE if not hasattr(pkg, n))
        assert missing == [], f"清单里有但包上没有：{missing}"

    def test_package_does_not_import_api_at_top_level(self):
        """`core.pipeline` 的子模块不得在顶层 import `api.*`（否则 core→api 倒置）。"""
        graph, _ = _build_graph()
        bad = sorted(
            f"{m} -> {t}"
            for m, ts in graph.items()
            for t in ts
            if m.startswith("core.pipeline") and t.split(".")[0] == "api"
        )
        assert bad == [], "core.pipeline 顶层依赖了 api：\n  " + "\n  ".join(bad)
