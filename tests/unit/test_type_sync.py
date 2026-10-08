"""类型单源同步不变式（M4/T4.9+T4.10）。

新增一个 finding 类型时，至少有 5 个"知识面"必须同步，漏一处就会出现
"规则跑了但前端显英文 / 无 GMP 依据 / 检索不到条款"：

  1. core.finding_quality.CANONICAL_TYPES     （类型白名单，权威）
  2. core.zh_map.FINDING_TYPE_ZH              （后端中文，report/notify 用）
  3. templates/review.html 的 type_zh         （服务端渲染前端）
  4. static/findings-map.js 的 TYPE_ZH        （SPA 前端单一真值）
  5. core.rules.gmp_basis.GMP_BASIS_MAP       （法规依据）
  6. core.kb.retriever.TYPE_QUERIES           （条款检索词）

本模块把这些"隐性契约"变成可机检断言（T4.9 双端一致 + T4.10 依据/检索覆盖）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from core.finding_quality import CANONICAL_TYPES
from core.zh_map import FINDING_TYPE_ZH

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "templates" / "review.html"
# R63 P2：SPA 前端类型中文的单一真值已从 review.js 的局部 typeZh
# 收敛到共享件 findings-map.js 的 TYPE_ZH（renderFindings 消费它）。
REVIEW_JS = REPO_ROOT / "static" / "findings-map.js"

# 无依据/检索语义的类型（内部键/技术噪音）：不强制映射。
_NO_BASIS = {"user_rule", "ocr_noise", "uncategorized"}


def _template_type_map() -> dict:
    text = TEMPLATE.read_text(encoding="utf-8")
    m = re.search(r"type_zh\s*=\s*(\{.*?\})\s*%\}", text, re.S)
    assert m, "review.html 未找到 type_zh 映射"
    return ast.literal_eval(m.group(1))


def _js_type_map() -> dict:
    text = REVIEW_JS.read_text(encoding="utf-8")
    m = re.search(r"(?:const|var)\s+TYPE_ZH\s*=\s*(\{.*?\n\s*\})", text, re.S)
    assert m, "findings-map.js 未找到 TYPE_ZH 映射"
    body = m.group(1)
    return {
        k: v for k, v in re.findall(r"(\w+)\s*:\s*\"([^\"]*)\"", body)
    }


class TestFrontendDualSync:
    def test_template_covers_canonical(self):
        tmpl = _template_type_map()
        missing = [t for t in CANONICAL_TYPES if t not in tmpl]
        assert missing == [], f"review.html type_zh 缺：{missing}"

    def test_js_covers_canonical(self):
        js = _js_type_map()
        missing = [t for t in CANONICAL_TYPES if t not in js]
        assert missing == [], f"review.js typeZh 缺：{missing}"

    def test_two_ends_agree(self):
        """双端（Jinja 模板 vs SPA）中文映射必须逐键一致。"""
        tmpl, js = _template_type_map(), _js_type_map()
        only_tmpl = {k: v for k, v in tmpl.items() if js.get(k) != v}
        only_js = {k: v for k, v in js.items() if tmpl.get(k) != v}
        assert not only_tmpl and not only_js, (
            f"前端双端不一致：template={only_tmpl} js={only_js}"
        )


class TestZhMapCoverage:
    def test_backend_zh_covers_canonical(self):
        missing = [t for t in CANONICAL_TYPES if t not in FINDING_TYPE_ZH]
        assert missing == []

    def test_m4_types_have_zh(self):
        for t in ("mass_balance", "self_review", "equipment_state",
                  "env_monitor", "doc_version", "deviation_link", "alteration"):
            assert t in FINDING_TYPE_ZH and FINDING_TYPE_ZH[t]


class TestGmpBasisCoverage:
    def test_canonical_types_have_basis(self):
        from core.rules.gmp_basis import GMP_BASIS_MAP
        missing = [
            t for t in CANONICAL_TYPES
            if t not in _NO_BASIS and t not in GMP_BASIS_MAP
        ]
        assert missing == [], f"缺 GMP 依据：{missing}"

    def test_rule_types_all_have_basis(self):
        """注册表里每条规则的 type 都必须有依据（避免新规则漏挂依据）。"""
        from core.rules.gmp_basis import GMP_BASIS_MAP
        from core.rules.registry import RULE_REGISTRY
        missing = [s.id for s in RULE_REGISTRY if s.type not in GMP_BASIS_MAP]
        assert missing == [], f"规则 type 缺依据：{missing}"


class TestTypeQueriesCoverage:
    def test_canonical_types_have_queries(self):
        from core.kb.retriever import TYPE_QUERIES
        missing = [
            t for t in CANONICAL_TYPES
            if t not in {"user_rule", "uncategorized"} and t not in TYPE_QUERIES
        ]
        assert missing == [], f"缺检索词：{missing}"

    def test_queries_non_empty(self):
        from core.kb.retriever import TYPE_QUERIES
        empty = [t for t, q in TYPE_QUERIES.items() if not q]
        assert empty == []


# ── LLM 自由枚举的**前置富集**契约（2026-10-08，更正 #165 的"死键"结论）────
# `GMP_BASIS_MAP` / `TYPE_QUERIES` 里各有一批**非规范键**：batch_logic /
# low_confidence / time_anomaly。它们**不是死键** —— 富集在**类型归一之前**
# 执行，所以 LLM 直出的原始 type 会被它们接住。
# 实测（2026-10-08）：raw `batch_logic` ⇒ gmp_basis 非空 + kb_refs=4。
# 归一之后：batch_logic→batch_inconsistency（关键词兜底）、time_anomaly→
# signature_time_anomaly（显式同义词）、low_confidence→uncategorized
# （**策略使然**，非缺陷：见 finding_quality 模块 docstring "无法归一的落到
# uncategorized（而非混入 completeness，避免继续膨胀最大的噪声桶）"）。
# 本类锁住两件**此前无人守**的事：
#   ① 这些键真的能产出富集（不是死键，删掉会**静默降级**）；
#   ② 在源码里，**被归一的那个 finding 集合**必须先被富集。
_ENRICH_FUNCS = {"attach_gmp_basis", "attach_kb_refs"}
_NORM_FUNCS = {"normalize_finding_type", "_norm"}


def _call_name(node: ast.Call):
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def _root_name(node) -> str | None:
    """表达式的最左 Name（`f.get("type")` → `f`；`f` → `f`）。"""
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def _norm_collections(src: str) -> list[tuple[str, int]]:
    """[(被归一的集合名, 归一调用行号)] —— 从循环的 iter 反推集合。

    全局顺序（`max(富集) < min(归一)`）是**错的**：`stage3.py` 另有一条
    `attach_kb_refs(dual_dicts)` 在归一之后，但 `dual_dicts` 的 type 是**写死的
    规范值** `completeness`、**根本不经过归一** ⇒ 不该被这条不变式约束。
    故必须按**集合**配对，而不是全局排序。
    """
    tree = ast.parse(src)
    parents: dict = {}
    for p in ast.walk(tree):
        for ch in ast.iter_child_nodes(p):
            parents[ch] = p
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _call_name(node) in _NORM_FUNCS):
            continue
        coll = None
        cur = node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, (ast.For, ast.AsyncFor)):
                it = cur.iter
                coll = it.id if isinstance(it, ast.Name) else None
                break
            if isinstance(cur, (ast.ListComp, ast.SetComp, ast.DictComp,
                                ast.GeneratorExp)):
                it = cur.generators[0].iter
                coll = it.id if isinstance(it, ast.Name) else None
                break
        if coll:
            out.append((coll, node.lineno))
    return out


def _enrich_lines_for(src: str, collection: str) -> list[int]:
    """富集调用中，实参根名 == `collection` 的行号。"""
    lines: list[int] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and _call_name(node) in _ENRICH_FUNCS:
            if node.args and _root_name(node.args[0]) == collection:
                lines.append(node.lineno)
    return lines


class TestRawEnumEnrichmentContract:
    """非规范（LLM 自由枚举）键的富集契约 —— 两条不变式，均此前无人守。"""

    def test_raw_enum_keys_are_live_not_dead(self):
        """#165 结论更正：这些"非规范键"是**前置富集键**，每条都能真的产出
        依据/引用 ⇒ 删掉它们会**静默降级**（依据为空、检索退回泛词）。"""
        from core.finding_quality import CANONICAL_TYPES
        from core.kb.retriever import TYPE_QUERIES, attach_kb_refs
        from core.rules.gmp_basis import GMP_BASIS_MAP, attach_gmp_basis

        canon = set(CANONICAL_TYPES)
        raw_keys = sorted((set(GMP_BASIS_MAP) | set(TYPE_QUERIES)) - canon)
        assert raw_keys == ["batch_logic", "low_confidence", "time_anomaly"], (
            f"非规范键集变化：{raw_keys} —— 新增/删除都必须同步本护栏与文档"
        )
        for k in raw_keys:
            rows = [{
                "page": 1, "type": k, "severity": "info", "source": "rule",
                "description": "批号记录不一致，手写涂改，参数偏离质量标准",
            }]
            attach_gmp_basis(rows)
            attach_kb_refs(rows)
            if k in GMP_BASIS_MAP:
                assert rows[0].get("gmp_basis"), f"{k} 在 GMP_BASIS_MAP 却拿不到依据"
            if k in TYPE_QUERIES:
                assert rows[0].get("kb_refs"), f"{k} 在 TYPE_QUERIES 却检索不到条款"

    def test_enrichment_precedes_normalization_per_collection(self):
        """源码级不变式：**被归一的集合**必须先被富集。

        一旦把归一提到富集之前，非规范键随即失配 ⇒ batch_logic /
        low_confidence / time_anomaly 三类 finding **静默**失去依据与引用，
        而所有既有用例仍然全绿（它们直接以 raw type 调富集函数）。
        """
        for name in ("stage2.py", "stage3.py"):
            src = (REPO_ROOT / "core" / "pipeline" / name).read_text(
                encoding="utf-8")
            normed = _norm_collections(src)
            assert normed, f"{name}: 未找到「归一某集合」的调用 ⇒ 护栏失去对象"
            for coll, nline in normed:
                elines = _enrich_lines_for(src, coll)
                assert elines, (
                    f"{name}: 集合 {coll} 被归一却从未富集 ⇒ 护栏失去对象"
                )
                assert max(elines) < nline, (
                    f"{name}: 集合 {coll} 的富集(last={max(elines)}) 必须早于"
                    f"归一(line={nline}) —— 否则 LLM 自由枚举的富集静默失效"
                )

    def test_order_detector_is_not_vacuous(self):
        """正/负对照：检测器必须能对**倒序**报警、对正序放行。"""
        bad = (
            "def g(findings):\n"
            "    for f in findings:\n"
            "        t = _norm(f)\n"
            "    attach_kb_refs(findings)\n"
        )
        assert _norm_collections(bad) == [("findings", 3)], _norm_collections(bad)
        assert max(_enrich_lines_for(bad, "findings")) > 3, (
            "检测器对倒序未报警 ⇒ 恒绿"
        )
        good = (
            "def g(findings):\n"
            "    attach_kb_refs(findings)\n"
            "    for f in findings:\n"
            "        t = _norm(f)\n"
        )
        assert _norm_collections(good) == [("findings", 4)], _norm_collections(good)
        assert max(_enrich_lines_for(good, "findings")) < 4
