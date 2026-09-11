"""类型单源同步不变式（M4/T4.9+T4.10）。

新增一个 finding 类型时，至少有 5 个"知识面"必须同步，漏一处就会出现
"规则跑了但前端显英文 / 无 GMP 依据 / 检索不到条款"：

  1. core.finding_quality.CANONICAL_TYPES     （类型白名单，权威）
  2. core.zh_map.FINDING_TYPE_ZH              （后端中文，report/notify 用）
  3. templates/review.html 的 type_zh         （服务端渲染前端）
  4. static/review.js 的 typeZh               （SPA 前端）
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
REVIEW_JS = REPO_ROOT / "static" / "review.js"

# 无依据/检索语义的类型（内部键/技术噪音）：不强制映射。
_NO_BASIS = {"user_rule", "ocr_noise", "uncategorized"}


def _template_type_map() -> dict:
    text = TEMPLATE.read_text(encoding="utf-8")
    m = re.search(r"type_zh\s*=\s*(\{.*?\})\s*%\}", text, re.S)
    assert m, "review.html 未找到 type_zh 映射"
    return ast.literal_eval(m.group(1))


def _js_type_map() -> dict:
    text = REVIEW_JS.read_text(encoding="utf-8")
    m = re.search(r"const\s+typeZh\s*=\s*(\{.*?\n\s*\})\s*;", text, re.S)
    assert m, "review.js 未找到 typeZh 映射"
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
