"""core.rules.engine — Stage 3 跨页分析**编排入口**（2026-09 从 `core/rules/__init__.py` 迁出）。

为什么单独成文件
----------------
`core/rules/__init__.py` 的职责是**包的导出面**；把 145 行编排体放在那里，
等于让"包"同时承担两件事（SRP）。本文件只放编排，导出面留在 `__init__`。

⚠️ **patch 兼容契约（改动前必读）**
编排体原先住在 `core.rules` 里，因此它解析的 7 个协作者都是**包命名空间的全局名**。
多处脚本/测试正是按**包路径**打桩：

    patch("core.rules._llm_fallback_check", new=AsyncMock(return_value=[]))
    patch("core.rules._llm_based_check",     new=AsyncMock(return_value=[]))
    patch.object(core.rules, "enabled_rule_specs", new=lambda: [...])

调用方（实测）：
  · `scripts/replay_rules.py`      —— 三个目标全用
  · `scripts/eval_findings.py`     —— `_llm_*`
  · `tests/unit/test_cross_page_entry_coverage.py` —— `_llm_*`
  · `tests/unit/test_rule_registry.py`             —— `_llm_*`

⇒ 这 7 个名字**必须**在 `analyze_cross_page` 内部**调用期**从 `core.rules` 解析
（函数内 `from core.rules import ...`），**不得**改成模块级 from-import：
模块级绑定发生在导入期，之后对包属性的打桩**看不到** ⇒ 桩**静默失效**，
测试会转而**调用真 LLM**、跑**全部规则**（不报错、只是变慢变贵或失败）。

这是本仓库既有约定（见 `core/pipeline/__init__.py` 模块 docstring：
"Sub-modules resolve patchable collaborators at call time"）。

护栏：`tests/unit/test_rules_engine_patchability.py`
（对 7 个目标逐一断言"打桩后行为确实改变"）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def analyze_cross_page(
    page_structures: list[dict], job_id: str = "", progress_cb=None
) -> list[dict]:
    """Analyze all pages and return findings list.

    Args:
        page_structures: list of {page, data} dicts from page_cache.
        job_id: passed through to LLM audit_ctx for GMP traceability.
        progress_cb: async (done, total, label) — Stage 3 子进度上报
            （SSE"跨页分析"文案），4 个里程碑：规则校验 / LLM 兜底 /
            LLM 语义 / 完成。None 时静默。

    Rule layer is driven by `registry.RULE_REGISTRY` (M4/T4.0): each enabled
    RuleSpec runs in registry order, its findings are logged under the rule id,
    and cross-rule signals (OOS pages) accumulate in the shared RuleContext.
    """
    # ── 调用期解析（patch 兼容契约，见模块 docstring；勿改成模块级 import）──
    from core.rules import (
        RuleContext as _run_rule_context,
        _build_summary as _run_build_summary,
        _collect_per_page_findings as _run_collect_per_page,
        _llm_based_check as _run_llm_based,
        _llm_fallback_check as _run_llm_fallback,
        _normalize_pages as _run_normalize_pages,
        enabled_rule_specs as _run_enabled_specs,
    )

    if not page_structures:
        return []

    _CROSS_TOTAL = 3
    if progress_cb:
        try:
            await progress_cb(0, _CROSS_TOTAL, "规则校验")
        except Exception:
            pass

    pages = _run_normalize_pages(page_structures)
    logger.info(
        f"[{job_id}] Cross-page analysis start: {len(pages)} pages "
        f"(from {len(page_structures)} structures)"
    )
    # value_source 覆盖率统计（手写体存疑原则的可观测性）：LLM 语义标注
    # 优先，_backfill_value_source 按列名启发式兜底；覆盖率过低说明 OCR
    # 列名识别质量差，会影响降噪路径（边缘超差 → info）的可信度。
    n_param = 0
    n_param_vs = 0
    n_cell = 0
    n_cell_vs = 0
    for pg in pages:
        for s in pg["steps"]:
            for p in s.get("parameters") or []:
                n_param += 1
                if p.get("value_source"):
                    n_param_vs += 1
            for m in s.get("measurements") or []:
                for v in (m.get("values") or {}).values():
                    n_cell += 1
                    if v.get("value_source"):
                        n_cell_vs += 1
    if n_param or n_cell:
        logger.info(
            f"[{job_id}] value_source coverage: parameters {n_param_vs}/{n_param}, "
            f"cells {n_cell_vs}/{n_cell}"
        )
    # Round 7: OCR 结构化手写信号覆盖统计 — 携带 _ocr_low_conf_cols 的页数。
    # 信号页少（或全无）说明 OCR 未产出低置信度占位符 → value_source 依赖
    # LLM 标注/关键词兜底；信号页多则手写判定有机器事实支撑。
    n_sig_pages = sum(1 for pg in pages if (pg["data"].get("_ocr_low_conf_cols")))
    if n_sig_pages:
        logger.info(
            f"[{job_id}] OCR handwriting signal: {n_sig_pages}/{len(pages)} pages "
            f"carry low-confidence column/label tokens"
        )

    # ── 规则层：由注册表驱动（M4/T4.0）──────────────────────────────────
    ctx = _run_rule_context(job_id=job_id)
    rule_findings: list[dict] = []
    specs = _run_enabled_specs()
    for spec in specs:
        found = spec.check(pages, ctx)
        rule_findings.extend(found)
        ctx.record(found)
        logger.info(f"[{job_id}] {spec.id} {spec.type}: {len(found)}")
    logger.info(
        f"[{job_id}] rule layer: {len(rule_findings)} findings across "
        f"{len(specs)} rules, {len(ctx.llm_queue)} queued for LLM fallback"
    )

    # Per-page LLM findings pass-through:
    # When per-page LLM (v3) produces findings directly (e.g. page2 time_reversal
    # when steps=[]), surface them in the cross-page output so they reach the
    # review page. The rule layer and per-page LLM are complementary: rules run
    # on structured steps/measurements; per-page LLM catches what didn't make
    # it into structured fields. Correct page number to the actual page.
    per_page_findings = _run_collect_per_page(pages)
    rule_findings.extend(per_page_findings)

    # LLM fallback: judge params that rules could not
    if progress_cb:
        try:
            await progress_cb(1, _CROSS_TOTAL, "LLM 兜底判定")
        except Exception:
            pass
    llm_fallback_findings = await _run_llm_fallback(ctx.llm_queue, job_id=job_id)
    rule_findings.extend(llm_fallback_findings)

    # LLM semantic check (catches what rules missed)
    # 用户规则每 job 只读一次 config.json，避免 _user_rules_section /
    # enabled_rule_ids 各读一遍（每次文件 IO + JSON 解析，51 页大文件时浪费）。
    from config import config as _app_config, load_user_rules
    user_rules = [r for r in load_user_rules() if r.get("active")]
    # Round 7: 跨页摘要按模型上下文窗口推导预算（config.app.llm_context_window，
    # LLM_CONTEXT_WINDOW env / config.json 顶层键；缺失时 llm_checks 用默认值）。
    summary = _run_build_summary(
        pages,
        context_window=getattr(_app_config["app"], "llm_context_window", None),
    )
    if progress_cb:
        try:
            await progress_cb(2, _CROSS_TOTAL, "LLM 语义分析")
        except Exception:
            pass
    llm_findings = await _run_llm_based(summary, job_id=job_id, user_rules=user_rules)

    # B1-1 同根因聚合（降噪）：单个 OCR 误读会在每个时间点各产 1 条 finding
    # （真实 p08「进料压力」7 个时间点 7 条同根因）。聚合**只对**携带派生键
    # （`finding_aggregate.AGG_KEY`，由判定点 `make_oos_finding` 挂上）的条目生效，
    # 其余逐字节原样通过。合并留痕：明细而非计数（见 aggregate_by_root_cause）。
    from core.rules.finding_aggregate import aggregate_by_root_cause
    n_before = len(rule_findings)
    rule_findings, merged_rows = aggregate_by_root_cause(rule_findings)
    if merged_rows:
        for row in merged_rows:
            logger.info(
                f"[{job_id}] B1-1 aggregated {row['count']} same-root-cause findings "
                f"→ 1: p{row['page']} {row['type']} {row['subject']} "
                f"(cause={row['cause']}, severities={row['severities']})"
            )
        logger.info(
            f"[{job_id}] B1-1 root-cause aggregation: {n_before} → {len(rule_findings)} "
            f"rule-layer findings ({len(merged_rows)} groups merged)"
        )

    all_findings = rule_findings + llm_findings
    if progress_cb:
        try:
            await progress_cb(_CROSS_TOTAL, _CROSS_TOTAL, "完成")
        except Exception:
            pass
    logger.info(
        f"[{job_id}] Cross-page analysis done: {len(rule_findings)} rule + {len(llm_findings)} LLM "
        f"({len(llm_fallback_findings)} from LLM fallback, "
        f"{len(per_page_findings)} from per-page LLM pass-through) = {len(all_findings)} total"
    )
    return all_findings
