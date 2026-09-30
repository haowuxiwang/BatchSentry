"""core.rules — Stage 3 cross-page rule engine（**包的导出面**）。

模块划分（2026-08 从 1700 行 `core/cross_page_analyzer.py` 拆出）：

- `parsing.py`           : spec/time/unit 解析原语（`SpecBounds`, `_parse_*`）
- `base.py`              : 页结构归一化（`_normalize_pages`, `_collect_per_page_findings`）
- `rule_time.py`         : R1 time_reversal / R2 year_contradiction / R4 suspicious_date /
                           R5 signature_time_anomaly
- `rule_spec.py`         : R3 param_out_of_spec（单元格 + LLM 队列判定）
- `rule_doc.py`          : R6 completeness / R7 batch_consistency / R8 low_confidence /
                           R9 handwritten_notes / R-M1,R-M2 measurement
- `rule_gmp.py`          : R11 mass_balance … R17 alteration（M4 GMP 运营合规）
- `registry.py`          : `RuleSpec` / `RULE_REGISTRY` —— 规则元数据单一来源（M4/T4.0）
- `llm_checks.py`        : LLM 兜底 + 语义检查 + 用户规则提示词装配
- `finding_aggregate.py` : B1-1 同根因聚合
- `engine.py`            : **编排入口 `analyze_cross_page`**（2026-09 从本文件迁出）

M4 (T4.0)：规则执行顺序、元数据（id/type/severity/basis）与逐条开关**全部**来自
`registry.RULE_REGISTRY` / `enabled_rule_specs()` —— 新增规则 = 注册表追加一行，
**不需要**改 `engine.py` 的编排体。

`core/cross_page_analyzer.py` 仍是向后兼容垫片。

⚠️ **patch 兼容契约（改动前必读）**
下面"调用期解析契约"那 7 个名字**必须**留在本模块命名空间：
`engine.analyze_cross_page` 在**调用期**用 `from core.rules import ...` 取它们，
因为多处脚本/测试按**包路径**打桩：

    patch("core.rules._llm_fallback_check", new=AsyncMock(return_value=[]))
    patch("core.rules._llm_based_check",     new=AsyncMock(return_value=[]))
    patch.object(core.rules, "enabled_rule_specs", new=lambda: [...])

调用方：`scripts/replay_rules.py`（三个目标全用）、`scripts/eval_findings.py`、
`tests/unit/test_cross_page_entry_coverage.py`、`tests/unit/test_rule_registry.py`。

删掉其中任何一个，或把它们改成 `engine.py` 的**模块级** from-import，
都会让这些桩**静默失效** —— 测试转而**调用真 LLM**、跑**全部规则**，
不报错、只是变慢变贵或失败。护栏：`tests/unit/test_rules_engine_patchability.py`。

（历史说明：本文件此前还导入了 15 个 `_check_*` 规则函数，但编排体走的是注册表
`spec.check(pages, ctx)`，那些名字**一个都没用** —— 是死导入，且让
`from core.rules import _check_time_reversal_in_page` **意外可用**（误导性 API）。
2026-09 已删除；删除依据见 `devlogs/_verify/probe_rules_init_dead_imports.py`。
需要它们请直接从 `core.rules.rule_time` / `rule_doc` / `rule_spec` 导入。）
"""
from __future__ import annotations

# ── 公开 API ────────────────────────────────────────────────────────────
from core.rules.engine import analyze_cross_page
from core.rules.parsing import SpecBounds as SpecBounds  # re-export for shim/tests

# ── 调用期解析契约（engine.py 按包路径取这 7 个；勿删，勿下沉）───────────
from core.rules.base import (  # noqa: F401
    _collect_per_page_findings,
    _normalize_pages,
)
from core.rules.llm_checks import (  # noqa: F401
    _build_summary,
    _llm_based_check,
    _llm_fallback_check,
)
from core.rules.registry import (  # noqa: F401
    RuleContext,
    enabled_rule_specs,
)
