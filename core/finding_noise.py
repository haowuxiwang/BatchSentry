"""completeness 噪声治理（M2 降噪，可度量）。

问题（实测）
------------
真实 51 页 job 的 784 条 findings 中 **completeness 占 678 条（86.5%）**，其中
525 条为 warning。最小复现：一个"干净页"（编号连续/时间有序/操作复核齐全）
仍产出 `completeness[info] 缺少 QA 签名`。复核界面被同质条目淹没，真正的异常
（批号不一致、时间倒序、参数越界等 ~100 条）被埋没。

两条治理策略（保守、可解释、可度量）
------------------------------------
1. **抽取不确定降级**：结构性缺失中依赖 OCR 抽取的项（`time=空`）若发生在
   **带 OCR 完整性告警的页**上，多半是"没抽出来"而非"记录没写" —— 由
   warning 降为 info 并打 `extraction_uncertain`。这是把"机器没读到"与
   "真的缺失"分开，避免把抽取缺陷当 GMP 违规报。
2. **文档级聚合**：同一类结构性缺失若**既多又广**（条数 ≥ min_group 且涉及
   页数 ≥ total_pages × min_page_ratio），它反映的是**整份记录的模板属性**，
   而非逐页异常 → 折叠为 1 条摘要（携带完整页清单），其余逐条删除。

不做的事
--------
- 不改非 completeness 类型的 finding（高价值信号原样保留）。
- 不删除信息：摘要条携带 `page_list` 与条数，逐页可追溯。
- 阈值是**显式参数**（可测、可调），不写死在逻辑里。

与 `docs/FINDING_GROUND_TRUTH.json` 的 `noise_types=[completeness]` 对齐：
聚合后噪声 KPI 应显著下降，而 `scripts/eval_findings.py` 的 P/R/F1 不应下降。
"""
from __future__ import annotations

# 结构性缺失的机器可读标记（来自 core/rules/rule_doc.py 的 ocr_text）。
# tests/unit/test_finding_noise.py 锁定该契约：rule_doc 一旦改标记，测试立即失败。
MARKER_TIME = "time=空"
MARKER_OPERATOR = "operator=空"
MARKER_REVIEWER = "reviewer=空"
MARKER_QA = "qa=空"
STRUCTURAL_MARKERS: tuple[str, ...] = (
    MARKER_TIME, MARKER_OPERATOR, MARKER_REVIEWER, MARKER_QA,
)

# 依赖 OCR 抽取的 kind：抽取失败 ≠ 记录缺失，故可降级。
EXTRACTION_KINDS: frozenset[str] = frozenset({"time"})

# 可按"整份文档聚合"的其他类型（非结构性缺失，但同样会海量重复）。
# spec_unverifiable：实测值与规格单位/量纲不一致且无换算规则 —— 逐页报会
# 淹没真正的越界项，聚合为一条更利于复核。
AGGREGATABLE_TYPES: frozenset[str] = frozenset({"spec_unverifiable"})

# 摘要条目展示的页清单上限（描述文案里的样例）。
_PAGE_SAMPLE = 12


def structural_kind(finding: dict) -> str | None:
    """识别"结构性缺失"completeness 条目的 kind（time/operator/reviewer/qa）。"""
    if (finding.get("type") or "") != "completeness":
        return None
    txt = finding.get("ocr_text") or ""
    for marker in STRUCTURAL_MARKERS:
        if marker in txt:
            return marker.split("=", 1)[0]
    return None


def noise_group_key(finding: dict) -> str | None:
    """该 finding 归属的降噪分组键；None = 不参与降噪（高价值信号原样保留）。

    结构性缺失按 kind 细分（time/qa/...）；其余可聚合类型按 type 归组。
    """
    kind = structural_kind(finding)
    if kind is not None:
        return kind
    ftype = finding.get("type") or ""
    return ftype if ftype in AGGREGATABLE_TYPES else None


def reduce_completeness_noise(
    findings: list[dict],
    *,
    flagged_pages: set[int] | None = None,
    total_pages: int | None = None,
    min_group: int = 8,
    min_page_ratio: float = 0.3,
) -> tuple[list[dict], dict]:
    """对 completeness 结构性缺失做降级 + 聚合。

    Args:
        findings: 规则/LLM 产出的 finding 列表（原地不修改）。
        flagged_pages: 带 OCR 完整性告警的页号集合（来自 page_is_flagged）。
        total_pages: 参与统计的总页数（用于广度阈值）；缺省用出现的页数。
        min_group: 触发聚合的最小条数。
        min_page_ratio: 触发聚合的最小页覆盖率。

    Returns:
        (kept, report)。report 含 downgraded / aggregated / per-kind 计数，
        供审计与测试断言（GMP 可追溯"为什么少了几百条"）。
    """
    flagged = set(flagged_pages or ())
    kept: list[dict] = []
    groups: dict[str, list[dict]] = {}
    downgraded = 0

    for f in findings:
        kind = noise_group_key(f)
        if kind is None:
            kept.append(f)
            continue
        # 策略 1：抽取依赖项 + OCR 告警页 → 降级为 info（不删）
        if kind in EXTRACTION_KINDS and f.get("severity") == "warning" \
                and f.get("page") in flagged:
            f = {**f, "severity": "info", "extraction_uncertain": True}
            downgraded += 1
        groups.setdefault(kind, []).append(f)

    pages_seen = {f.get("page") for f in findings if f.get("page") is not None}
    denom = total_pages if total_pages else max(1, len(pages_seen))

    aggregated: dict[str, dict] = {}
    for kind in sorted(groups):
        items = groups[kind]
        pages = sorted({f.get("page") for f in items if f.get("page") is not None})
        pervasive = len(items) >= min_group and len(pages) >= max(1, denom * min_page_ratio)
        if pervasive:
            summary = _summary_for(kind, len(items), pages)
            kept.append(summary)  # 摘要必须进入结果集（否则信息丢失）
            aggregated[kind] = {
                "count": len(items),
                "pages": pages,
                "summary": summary,
            }
        else:
            kept.extend(items)

    report = {
        "downgraded_extraction_uncertain": downgraded,
        "aggregated": aggregated,
        "aggregated_total": sum(v["count"] for v in aggregated.values()),
        "kept": len(kept),
    }
    return kept, report


_KIND_LABEL = {
    "time": "缺少执行时间记录",
    "operator": "缺少操作人签名",
    "reviewer": "缺少复核人签名",
    "qa": "缺少 QA/批准签名",
    "spec_unverifiable": "实测值与规格单位不一致且无换算规则",
}

# 结构性缺失的 kind 属于 completeness 类型；其余分组键即其 type。
_STRUCTURAL_KINDS = frozenset({"time", "operator", "reviewer", "qa"})


def _summary_for(kind: str, count: int, pages: list[int]) -> dict:
    """构造文档级摘要 finding（page 取最小页，便于在复核页可见）。"""
    label = _KIND_LABEL.get(kind, kind)
    ftype = "completeness" if kind in _STRUCTURAL_KINDS else kind
    sample = "、".join(str(p) for p in pages[:_PAGE_SAMPLE])
    more = f" 等 {len(pages)} 页" if len(pages) > _PAGE_SAMPLE else ""
    return {
        "page": pages[0] if pages else 1,
        "type": ftype,
        "severity": "info",
        "description": (
            f"全份记录共 {count} 处同类问题：{label}"
            f"（涉及 {len(pages)} 页：第 {sample} 页{more}）。"
            f"该类问题在本份记录中广泛出现，属模板/结构化抽取层面的共性问题，"
            f"已聚合为一条供整体核查，明细页清单见附加信息。"
        ),
        "ocr_text": f"aggregated kind={kind} count={count} pages={pages}",
        "operator": "",
        "source": "rule",
        "aggregated": True,
        "page_list": pages,
    }
