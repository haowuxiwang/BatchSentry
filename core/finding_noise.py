"""finding 降噪（M2 + R1）—— 可度量、可审计、不丢信息。

本模块治理**两类**不同的噪声，各有独立的聚合粒度：

R1 自指元噪声（`reduce_self_referential_noise`）
------------------------------------------------
讲的是"工具看不清"，不是"记录有问题"：`handwritten`（"本页含 N 条手写内容"）
与 `整体识别置信度较低`。真实 51 页轮次实测 72/442 = **16%**。它们是**文档属性**，
故整份文档聚合为 1 条（携带条数与完整页清单）。

completeness 结构性缺失（`reduce_completeness_noise`）
------------------------------------------------------
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
- 不改非目标类型的 finding（高价值信号原样保留）。
- 不删除信息：摘要条携带 `page_list` 与条数，逐页可追溯。
- 阈值是**显式参数**（可测、可调），不写死在逻辑里。

与 `docs/FINDING_GROUND_TRUTH.json` 的 `noise_types=[completeness]` 对齐：
聚合后噪声 KPI 应显著下降，而 `scripts/eval_findings.py` 的 P/R/F1 不应下降。
"""
from __future__ import annotations

import re
from datetime import datetime

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


# ---------------------------------------------------------------------------
# R1 自指元噪声：讲的是"工具看不清"，不是"记录有问题"
# ---------------------------------------------------------------------------
# 定位依据（真实 51 页轮次实测）：`handwritten` 65 条 + `整体识别置信度较低`
# 7 条 = 72 条（占 442 条的 16%），文案分别是"本页含 N 条手写内容…请人工核对"
# 与"整体识别置信度较低…建议人工核对 OCR 结果"。对 QA 复核员这是纯噪声
# （"这里有手写"人眼一秒可见），却**计入 finding 计数**，把真正的异常淹没。
#
# 治理：整份文档聚合成 1 条（携带完整页清单与条数），信息不丢失。
# 这些类型是**工具能力陈述**，不是记录缺陷 —— 与结构缺失（time/operator/…）
# 分开治理，因为聚合粒度不同（前者是文档属性，后者有 kind 细分）。
SELF_REFERENTIAL_TYPES: frozenset[str] = frozenset({"handwritten"})
# `completeness` 里的自指子类：由 R8 产出，文案固定（`rule_doc.py:370`）。
# 一旦该文案改动，`test_finding_noise.py` 的自指契约用例立即失败。
_SELF_REF_MARKERS: tuple[str, ...] = ("整体识别置信度较低",)
# 少于此条数不聚合（1 条不构成噪声，原样保留）。
_SELF_REF_MIN = 2

_SELF_REF_LABEL = {
    "handwritten": "含手写内容（手写体 OCR 易误读）",
    "ocr_confidence": "整体识别置信度较低",
}


def self_referential_kind(finding: dict) -> str | None:
    """识别"自指元噪声"条目的分组键；None = 不是自指噪声。"""
    ftype = finding.get("type") or ""
    if ftype in SELF_REFERENTIAL_TYPES:
        return ftype
    if ftype == "completeness":
        desc = finding.get("description") or ""
        if any(m in desc for m in _SELF_REF_MARKERS):
            return "ocr_confidence"
    return None


def _self_ref_summary(kind: str, items: list[dict]) -> dict:
    """构造文档级自指噪声摘要（保留条数与页清单 —— 信息不丢失）。"""
    pages = sorted({f.get("page") for f in items if f.get("page") is not None})
    label = _SELF_REF_LABEL.get(kind, kind)
    sample = "、".join(str(p) for p in pages[:_PAGE_SAMPLE])
    more = f" 等 {len(pages)} 页" if len(pages) > _PAGE_SAMPLE else ""
    return {
        "page": pages[0] if pages else 1,
        "type": kind if kind in SELF_REFERENTIAL_TYPES else "completeness",
        "severity": "info",
        "description": (
            f"全份记录共 {len(items)} 处「{label}」提示（涉及 {len(pages)} 页："
            f"第 {sample} 页{more}）。这是**工具可读性**提示而非记录缺陷，"
            f"已聚合为一条供整体参考；逐页明细见附加信息。"
        ),
        "ocr_text": f"aggregated kind={kind} count={len(items)} pages={pages}",
        "operator": "",
        "source": "rule",
        "aggregated": True,
        "page_list": pages,
    }


def reduce_self_referential_noise(
    findings: list[dict],
    *,
    min_group: int = _SELF_REF_MIN,
) -> tuple[list[dict], dict]:
    """把自指元噪声聚合为文档级摘要（R1）。

    Args:
        findings: 规则/LLM 产出的 finding 列表（原地不修改）。
        min_group: 触发聚合的最小条数（低于此值原样保留 —— 1 条不是噪声）。

    Returns:
        (kept, report)。report 的 `aggregated` 给出每类的条数与页清单，
        供审计回答"为什么这几百条变成了几条"。
    """
    kept: list[dict] = []
    groups: dict[str, list[dict]] = {}
    for f in findings:
        kind = self_referential_kind(f)
        if kind is None:
            kept.append(f)
        else:
            groups.setdefault(kind, []).append(f)

    aggregated: dict[str, dict] = {}
    for kind in sorted(groups):
        items = groups[kind]
        if len(items) < max(2, min_group):
            kept.extend(items)
            continue
        summary = _self_ref_summary(kind, items)
        kept.append(summary)
        aggregated[kind] = {
            "count": len(items),
            "pages": summary["page_list"],
        }

    report = {
        "aggregated": aggregated,
        "aggregated_total": sum(v["count"] for v in aggregated.values()),
        "kept": len(kept),
    }
    return kept, report


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


#: 聚合摘要里的**类目名**。⚠️ 只用于显示 —— 匹配靠 `STRUCTURAL_MARKERS`
#: 命中的 `ocr_text`（`operator=空` 等），改这里的字串**不会**影响聚合。
#: R95/A7：措辞与明细行对齐（「未识别到…」而非「缺少…」）。明细行说"缺失"、
#: 摘要说"缺少"，等于把 OCR 漏识别写成记录缺陷 —— 用户会去改一份本来合规的记录。
_KIND_LABEL = {
    "time": "未识别到执行时间记录",
    "operator": "未识别到操作人签名",
    "reviewer": "未识别到复核人签名",
    "qa": "未识别到 QA/批准签名",
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


# ---------------------------------------------------------------------------
# 文档级事实（CR-10 / CR-11）—— 同一件事被逐页重复，且写法本身有错
# ---------------------------------------------------------------------------
# 与上面两类的区别：这里治理的不是"噪声多"，而是**同一件文档级事实被按页复述**，
# 且复述时把"系统时钟"写进了结论。
#
# CR-10（实测 10 页语料）：p5、p10 各一条「批生产记录缺少生产日期信息。」
#   —— 同一条文档级事实说了两遍；51 页 p10 还有「页面信息缺失：版本号、批号、
#   生产日期。」。缺的是**页眉字段本身**（模板级），不是"某几页漏填"。
#
# CR-11（实测 51 页语料 12 条）：`suspicious_date` 全部以「距今/当前年份」为基准
#   ——「生产日期（2015.01.20）距今（2026-09-24）已超过10年」。
#   2026-09-24 是**跑批那天的系统时间**，不是记录里的任何事实。把它写进结论，
#   等于让结论随运行日期漂移；而且同一份文档里 2015 是 **2025 的 OCR 误读**，
#   真正该说的是"年份读数不可信"，不是"距今十年"。
#
# 治理：按 kind 收敛为**一条文档级陈述**，逐页明细进 page_list（信息不丢失）。
_MISSING_DATE_MARKERS: tuple[str, ...] = ("缺少生产日期", "生产日期信息", "无生产日期")

#: 与「生产日期」同句出现的缺失语 —— 覆盖「页面信息缺失：版本号、批号、生产日期。」
#: 这种把三件事写在一句里的写法（51 页 p10 实测）。**必须**同时出现"生产日期"，
#: 否则会把"缺少 QA 签名"之类的条目也卷进来。
_MISSING_WORDS: tuple[str, ...] = ("缺少", "缺失", "未填", "为空")

#: 以系统时钟为基准的措辞 —— 出现即判为"拿今天当尺子"。
_TODAY_BASELINE_MARKERS: tuple[str, ...] = ("距今", "当前年份", "当前日期")

_YEAR_RE = re.compile(r"(?:19|20)\d{2}")

#: 每种文档级事实的聚合门槛。`missing_production_date` 单条即已是文档级口吻，
#: 不必强聚合；`year_baseline_today` 即使只有 1 条**也必须重写** —— 它含系统时钟。
_DOC_FACT_MIN = {"missing_production_date": 2, "year_baseline_today": 1}


def document_fact_kind(finding: dict) -> str | None:
    """识别"文档级事实"的分组键；None = 不是。

    ⚠️ `aggregated` 的条目一律跳过：聚合后的摘要**本身**就带着
    「无生产日期字段」「距今/当前年份」这些词，不设这道闸门会二次聚合
    （`year_baseline_today` 的门槛是 1，第二次会把年份清单洗掉）。
    """
    if finding.get("aggregated"):
        return None
    ftype = str(finding.get("type") or "")
    desc = str(finding.get("description") or "")
    if ftype == "completeness":
        if any(m in desc for m in _MISSING_DATE_MARKERS):
            return "missing_production_date"
        if "生产日期" in desc and any(m in desc for m in _MISSING_WORDS):
            return "missing_production_date"
    if ftype == "suspicious_date" and any(m in desc for m in _TODAY_BASELINE_MARKERS):
        return "year_baseline_today"
    return None


def _doc_fact_summary(kind: str, items: list[dict], *, current_year: int) -> dict:
    pages = sorted({f.get("page") for f in items if f.get("page") is not None})
    sample = "、".join(str(p) for p in pages[:_PAGE_SAMPLE])
    more = f" 等 {len(pages)} 页" if len(pages) > _PAGE_SAMPLE else ""
    if kind == "missing_production_date":
        desc = (
            f"全份记录的记录页**页眉/页面信息栏无生产日期字段**"
            f"（{len(items)} 处提及，涉及 {len(pages)} 页：第 {sample} 页{more}）。"
            f"日期只出现在签名栏，属**模板级**事实而非某几页漏填；"
            f"已聚合为一条，逐页明细见附加信息。"
        )
        ocr = f"aggregated kind={kind} count={len(items)} pages={pages}"
    else:
        years: set[int] = set()
        for f in items:
            for y in _YEAR_RE.findall(str(f.get("description") or "")):
                years.add(int(y))
        # ⚠️ 系统时钟年份**不得**进结论（CR-11）：它来自运行环境，不是记录内容。
        years.discard(int(current_year))
        shown = "、".join(str(y) for y in sorted(years)) or "（未能提取）"
        # 例子只在**确实**出现 2015 时给 —— 否则会凭空举一个不存在的读法。
        example = "，如 2025 读成 2015" if 2015 in years else ""
        desc = (
            f"本文档的日期年份出现 {len(years)} 种读法（{shown}{example}），"
            f"其中包含明显的 OCR 年份误读 ⇒ **日期类判定不可信**。"
            f"原结论以「距今/当前年份」为基准（基准取自系统时钟，不是记录内容），"
            f"已聚合为一条，逐页明细见附加信息。"
        )
        ocr = f"aggregated kind={kind} count={len(items)} years={sorted(years)} pages={pages}"
    return {
        "page": pages[0] if pages else 1,
        "type": "completeness" if kind == "missing_production_date" else "suspicious_date",
        "severity": "info",
        "description": desc,
        "ocr_text": ocr,
        "operator": "",
        "source": "rule",
        "aggregated": True,
        "page_list": pages,
    }


def reduce_document_level_facts(
    findings: list[dict],
    *,
    current_year: int | None = None,
) -> tuple[list[dict], dict]:
    """把"文档级事实"收敛为一条陈述（CR-10 / CR-11）。

    Returns:
        (kept, report)。report 的 `aggregated` 给出每类的条数与页清单。
    """
    cy = int(current_year) if current_year is not None else datetime.now().year
    kept: list[dict] = []
    groups: dict[str, list[dict]] = {}
    for f in findings:
        kind = document_fact_kind(f)
        if kind is None:
            kept.append(f)
        else:
            groups.setdefault(kind, []).append(f)

    aggregated: dict[str, dict] = {}
    total = 0
    for kind, items in groups.items():
        if len(items) < _DOC_FACT_MIN.get(kind, 2):
            kept.extend(items)
            continue
        kept.append(_doc_fact_summary(kind, items, current_year=cy))
        pages = sorted({f.get("page") for f in items if f.get("page") is not None})
        aggregated[kind] = {"count": len(items), "pages": pages}
        total += len(items) - 1
    return kept, {"aggregated": aggregated, "aggregated_total": total}



def plan_document_level_collapse(
    rows: list[dict],
    *,
    current_year: int | None = None,
) -> dict:
    """把**已落库**的文档级事实收敛为一条的「计划」（纯函数，不碰 DB）。

    为什么需要这一层（TODO 0-64）
    ----------------------------
    `reduce_document_level_facts` 只跑在 Stage 3 的**内存** findings 上，而
    `llm_page` 在 Stage 2 每页完成时就已经落库、**不在那个列表里** ⇒ 页级 LLM
    在 p5 与 p10 各报一条「批生产记录缺少生产日期信息」，两条谁也收不到。
    实测（`4cffb358`，10 页）复核页上就是两条一模一样的卡片。

    判据**复用** `document_fact_kind`（CR-10/CR-11 已在用的那个分类器），
    不新造一套词表 —— 两套词表必然漂移。

    与内存路径的两处**刻意不同**
    ----------------------------
    1. 要求**跨页**（≥2 个不同页）。内存路径不要求，因为它手里只有待写入的行；
       而这里若同页两条相同文案也合并，会把「同一页重复插入」这个**另一个缺陷**
       （历史数据，A9 之前产生）静默掩盖掉 —— 那个应当被看见、被单独修。
    2. 只动 `pending` 的 LLM 来源行。已裁决（confirmed/rejected/corrected）的
       行是**复核结论**，不能因为后来做了聚合就消失；规则层的行是确定性生成，
       已有 `reduce_completeness_noise` 的阈值治理，不在这里动。

    Returns:
        {
          "update": {finding_id: {...要覆盖的列...}},
          "delete_ids": [...],
          "groups": {kind: {"count": n, "pages": [...], "kept_id": id}},
        }
        空计划 = `{"update": {}, "delete_ids": [], "groups": {}}`。
    """
    cy = int(current_year) if current_year is not None else datetime.now().year
    plan: dict = {"update": {}, "delete_ids": [], "groups": {}}

    eligible: list[dict] = []
    for r in rows:
        if str(r.get("status") or "pending") != "pending":
            continue
        if str(r.get("source") or "") not in ("llm_page", "llm_cross"):
            continue
        eligible.append(r)

    groups: dict[str, list[dict]] = {}
    for r in eligible:
        kind = document_fact_kind(r)
        if kind is None:
            continue
        groups.setdefault(kind, []).append(r)

    for kind, items in sorted(groups.items()):
        pages = sorted({f.get("page") for f in items if f.get("page") is not None})
        if len(items) < _DOC_FACT_MIN.get(kind, 2) or len(pages) < 2:
            continue
        summary = _doc_fact_summary(kind, items, current_year=cy)
        keep = min(items, key=lambda f: (f.get("page") or 0, f.get("id") or 0))
        keep_id = keep.get("id")
        others = [f for f in items if f.get("id") != keep_id]
        merge_note = (
            f"与第 {'、'.join(str(p) for p in pages)} 页同属一条文档级事实，"
            f"原 {len(items)} 条已合并为 1 条（模板级事实，不是某几页漏填）"
        )
        # **追加**而不是覆盖：保留行可能已有一条 R97 的降级理由（如"文案为推测性
        # 表述 ⇒ 降级为待人工核对"）。覆盖会把那条解释抹掉 —— 复核员看到 info
        # 却不知道为什么，正是 R120 要消灭的形态。
        prior = str(keep.get("guard_reason") or "").strip()
        plan["update"][keep_id] = {
            "description": summary["description"],
            "severity": summary["severity"],
            "ocr_text": summary["ocr_text"],
            "guard_reason": f"{prior}；{merge_note}" if prior else merge_note,
        }
        plan["delete_ids"].extend(f.get("id") for f in others)
        plan["groups"][kind] = {
            "count": len(items), "pages": pages, "kept_id": keep_id,
        }
    return plan
