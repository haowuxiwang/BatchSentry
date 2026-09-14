"""Finding 质量单源（M2）—— 置信度评分 + 类型白名单规范化。

设计原则
--------
1. **置信度来自客观信号**，而非 LLM 自报：来源确定性（规则层 > LLM 结构化
   提取 > LLM 自然语言生成）+ 所在页完整性标记（OCR 警告/稀疏/截断/
   grounding）。同一套评分在**写入期**（stage3 落库 findings.confidence）与
   **读取期**（旧 job 无该列时兜底现算）共用，避免两处口径漂移。
2. **类型白名单**：LLM 可能产出规范外的 type。规范外的类型会污染前端
   `type_zh` 映射、按类型统计口径与评测分解，故此处统一归一到最近的规范
   类型；无法归一的落到 `uncategorized`（而非混入 `completeness`，避免
   继续膨胀最大的噪声桶）。原文 type 由调用方保留在 `raw_type`。
3. **无 DB / 无网络 / 无副作用**：纯函数模块，可在 core 与 api 两侧自由引用，
   不会引入 api→core 的反向依赖。

本模块是 `api/review.py` 原有 `_confidence_for` 的迁出目标（T2.4）：逻辑下沉到
core 后，stage3 写库与 api 读库共享同一实现。
"""
from __future__ import annotations

# ── 类型白名单（T2.6）────────────────────────────────────────────────────────
# 取值与实际产出对齐（经全库 DISTINCT type 实测 + 规则/分析器字面量扫描确认）。
CANONICAL_TYPES: tuple[str, ...] = (
    "completeness",
    "handwritten",
    "param_out_of_spec",
    "signature_time_anomaly",
    "signature_mismatch",
    "suspicious_date",
    "time_reversal",
    "year_contradiction",
    "batch_inconsistency",
    "step_gap",
    "ocr_noise",
    "spec_unverifiable",  # M2：实测值与规格单位不一致且无换算规则 → 需人工核定
    # ── M4：R11–R17 运营合规检查（GMP 高频空洞）──
    "mass_balance",     # R11 物料平衡/收率：已声明但未记录实测值 / 无可核定范围
    "self_review",      # R12 操作人=复核人（ALCOA+ Attributable 独立复核失效）
    "equipment_state",  # R13 设备/清洁状态：确认项未通过或声明未填写
    "env_monitor",      # R14 环境监测：压差/洁净/温湿度项缺失或未通过
    "doc_version",      # R15 文件版本：同一文件编号出现多个版本
    "deviation_link",   # R16 超限偏差关联：有异常/超限但未记录偏差编号
    "alteration",       # R17 涂改规范：疑似划改但缺签名/日期
    "user_rule",
    "uncategorized",
)

# 未知类型 → 规范类型的显式别名（含 LLM 常见变体与中文名）。
TYPE_SYNONYMS: dict[str, str] = {
    "time_anomaly": "signature_time_anomaly",
    "signature_anomaly": "signature_time_anomaly",
    "signature_time": "signature_time_anomaly",
    "date_anomaly": "suspicious_date",
    "suspicious_time": "suspicious_date",
    "time_order": "time_reversal",
    "reverse_time": "time_reversal",
    "year_conflict": "year_contradiction",
    "year_inconsistency": "year_contradiction",
    "param_out_of_range": "param_out_of_spec",
    "out_of_spec": "param_out_of_spec",
    "parameter_violation": "param_out_of_spec",
    "batch_number_inconsistency": "batch_inconsistency",
    "batch_mismatch": "batch_inconsistency",
    "handwriting": "handwritten",
    "manual_entry": "handwritten",
    "missing_step": "step_gap",
    "step_missing": "step_gap",
    "missing_content": "completeness",
    "incomplete": "completeness",
    "ocr_sparse": "ocr_noise",
    "sparse_text": "ocr_noise",
    "user_defined": "user_rule",
    # ── M4/R11–R17 变体 ──
    "yield": "mass_balance",
    "material_balance": "mass_balance",
    "balance_rate": "mass_balance",
    "operator_reviewer_same": "self_review",
    "self_check": "self_review",
    "equipment_cleanliness": "equipment_state",
    "cleaning_state": "equipment_state",
    "cleanliness": "equipment_state",
    "env_monitoring": "env_monitor",
    "environment": "env_monitor",
    "version_mismatch": "doc_version",
    "document_version": "doc_version",
    "deviation": "deviation_link",
    "oos_link": "deviation_link",
    "tamper": "alteration",
    "correction": "alteration",
    # 中文名（LLM 偶尔直接回中文 type）
    "时间倒序": "time_reversal",
    "时间异常": "signature_time_anomaly",
    "年份矛盾": "year_contradiction",
    "可疑日期": "suspicious_date",
    "参数越界": "param_out_of_spec",
    "内容不完整": "completeness",
    "批号不一致": "batch_inconsistency",
    "手写": "handwritten",
    "步骤缺失": "step_gap",
    "收率": "mass_balance",
    "物料平衡": "mass_balance",
    "自检自核": "self_review",
    "设备状态": "equipment_state",
    "清洁状态": "equipment_state",
    "环境监测": "env_monitor",
    "文件版本": "doc_version",
    "偏差关联": "deviation_link",
    "涂改": "alteration",
    "划改": "alteration",
}

# 关键词兜底（按序匹配，先到先得）—— 覆盖未登记的变体。
# 顺序敏感：M4 新类型（R11–R17）关键词更具体，置于通用词之前，避免被
# "spec"/"missing" 等宽泛词提前截获（如 equipment_spec_mismatch 应归
# equipment_state 而非 param_out_of_spec）。
_TYPE_KEYWORDS: tuple[tuple[str, str], ...] = (
    # ── M4：R11–R17 ──
    ("mass_balance", "mass_balance"),
    ("material_balance", "mass_balance"),
    ("yield", "mass_balance"),
    ("收率", "mass_balance"),
    ("物料平衡", "mass_balance"),
    ("self_review", "self_review"),
    ("自检", "self_review"),
    ("equipment", "equipment_state"),
    ("clean", "equipment_state"),
    ("设备", "equipment_state"),
    ("清洁", "equipment_state"),
    ("清洗", "equipment_state"),
    ("env_monitor", "env_monitor"),
    ("environment", "env_monitor"),
    ("环境", "env_monitor"),
    ("压差", "env_monitor"),
    ("doc_version", "doc_version"),
    ("version", "doc_version"),
    ("文件版本", "doc_version"),
    ("deviation", "deviation_link"),
    ("偏差", "deviation_link"),
    ("alter", "alteration"),
    ("tamper", "alteration"),
    ("涂改", "alteration"),
    ("划改", "alteration"),
    # ── 既有 ──
    ("batch", "batch_inconsistency"),
    ("year", "year_contradiction"),
    ("sign", "signature_time_anomaly"),
    ("revers", "time_reversal"),
    ("date", "suspicious_date"),
    ("param", "param_out_of_spec"),
    ("spec", "param_out_of_spec"),
    ("hand", "handwritten"),
    ("step", "step_gap"),
    ("gap", "step_gap"),
    ("ocr", "ocr_noise"),
    ("noise", "ocr_noise"),
    ("user", "user_rule"),
    ("complet", "completeness"),
    ("missing", "completeness"),
)

UNCATEGORIZED = "uncategorized"


def normalize_finding_type(raw: str | None) -> str:
    """把任意 type 归一到 `CANONICAL_TYPES` 之一（T2.6）。

    归一顺序：已是规范类型 → 显式别名 → 关键词兜底 → `uncategorized`。
    绝不抛异常：LLM 输出不可信，此处必须 fail-safe。
    """
    if not raw:
        return UNCATEGORIZED
    t = str(raw).strip().lower()
    if t in CANONICAL_TYPES:
        return t
    if t in TYPE_SYNONYMS:
        return TYPE_SYNONYMS[t]
    for needle, canonical in _TYPE_KEYWORDS:
        if needle in t:
            return canonical
    return UNCATEGORIZED


def is_canonical(raw: str | None) -> bool:
    """原始 type 是否已在白名单内（无需归一）。"""
    return bool(raw) and str(raw).strip().lower() in CANONICAL_TYPES


# ── 复核分级（三色，M6/T6.4）────────────────────────────────────────────────
# 按**检出来源**分层，而非按严重度：严重度回答"多严重"（排序用），来源回答
# "谁判定的、可信度如何"（复核方式用）。复核者据此分配注意力 —— 规则层是
# 确定性判据（红：必须逐条看，机器已确认事实）、LLM 层是辅助提示（蓝：可
# 快速批量扫），未命中任何规则的类型计入"系统校验通过"（绿：覆盖面证明，
# 见 `core/rules/registry.py::rule_coverage`）。
#
# 取值来源经实测扫描（core/rules/*、core/pipeline/stage2|stage3 的写入点）：
#   rule / user_rule / llm_page / llm_fallback / llm_cross。
REVIEW_TIER_BY_SOURCE: dict[str, str] = {
    "rule": "rule",
    "user_rule": "rule",   # 用户自定义规则：同样是确定性判据（可复现）
    "llm_page": "llm",
    "llm_fallback": "llm",
    "llm_cross": "llm",
}
# 三色层级（顺序即面板展示顺序）。
REVIEW_TIERS: tuple[str, ...] = ("rule", "llm", "pass")
REVIEW_TIER_ZH: dict[str, str] = {
    "rule": "规则命中",
    "llm": "LLM 辅助",
    "pass": "系统校验通过",
}
# 由 finding 承载的分级（去重保序）—— `pass` 不在其中：它是覆盖面概念，
# 不来自单条 finding，见 rule_coverage()。
FINDING_TIERS: tuple[str, ...] = tuple(dict.fromkeys(REVIEW_TIER_BY_SOURCE.values()))


def review_tier(source: str | None) -> str:
    """finding.source → 复核分级（rule/llm/pass 三色中的前两色）。

    - 缺省（空）来源 → `rule`：与写入路径一致（stage3 落库时
      `f.get("source", "rule")`，置信度评分同样 `or "rule"`）。
    - 未知非空来源 → `llm`（保守）：视为需人工判读的辅助提示，绝不
      冒充确定性判据 —— 误标成"规则"会让复核者放松警惕。
    """
    src = str(source or "").strip().lower()
    if src in REVIEW_TIER_BY_SOURCE:
        return REVIEW_TIER_BY_SOURCE[src]
    return "llm" if src else "rule"


def attach_review_tier(findings: list[dict]) -> list[dict]:
    """就地给每条 finding 附 `tier` 字段，并返回同一列表。

    SSR（main.py 复核页）与 AJAX（api/review.py list_findings）共用此函数，
    前端只读 `f.tier`，不再各持一份 source→颜色映射 —— 双端映射漂移是
    `type_zh` 历史上踩过的坑（见 tests/unit/test_type_sync.py）。
    """
    for f in findings:
        if isinstance(f, dict):
            f["tier"] = review_tier(f.get("source"))
    return findings


def tier_counts(findings: list[dict]) -> dict[str, int]:
    """按分级统计条数（面板三色计数条的前两色）。

    只统计"由 finding 承载"的分级（rule/llm）；`pass` 是覆盖面概念、不来自
    finding，由 `rule_coverage()` 提供，故不在此处硬编码。
    """
    counts = {t: 0 for t in FINDING_TIERS}
    for f in findings:
        if not isinstance(f, dict):
            continue  # 与 attach_review_tier 同款容错（上游可能混入非 dict）
        t = f.get("tier") or review_tier(f.get("source"))
        if t in counts:
            counts[t] += 1
    return counts


# ── 置信度评分（T2.4 迁出 api/review.py）─────────────────────────────────────
_CONF_BASE = 0.85
_CONF_LLM_GEN_PENALTY = 0.15   # llm_cross/llm_fallback/user_rule（自然语言生成）
_CONF_LLM_PAGE_PENALTY = 0.10  # llm_page（结构化提取，稍可靠）
_CONF_PAGE_FLAG_PENALTY = 0.20  # 所在页带 OCR 警告/稀疏/截断/grounding 横幅
_CONF_MIN, _CONF_MAX = 0.30, 0.95


# 页面级完整性告警键（OCR 侧链 + 结构化裁剪 + schema 修复）。任一中招即视为
# "该页证据不完整"，置信度下调并建议人工复核。
PAGE_FLAG_KEYS: tuple[str, ...] = (
    "_ocr_warning",
    "_ocr_sparse",
    "_ocr_truncated",
    "_grounding_warn",
    "_truncated_warn",
    "_schema_warn",
)


def page_is_flagged(page_data: dict | None) -> bool:
    """该页结构化结果是否带完整性告警（写/读两侧共用同一判据）。"""
    if not isinstance(page_data, dict):
        return False
    return any(page_data.get(k) for k in PAGE_FLAG_KEYS)


def confidence_for(finding: dict, page_flagged: bool = False) -> float:
    """单条 finding 的置信度评分 [0.30, 0.95]。

    信号：来源确定性（规则层 > LLM 结构化提取 > LLM 自然语言生成）+ 所在页
    完整性标记。评分范围有上下界，防止单信号把置信度推到极端。
    """
    score = _CONF_BASE
    src = finding.get("source") or "rule"
    if src in ("llm_cross", "llm_fallback", "user_rule"):
        score -= _CONF_LLM_GEN_PENALTY
    elif src == "llm_page":
        score -= _CONF_LLM_PAGE_PENALTY
    if page_flagged:
        score -= _CONF_PAGE_FLAG_PENALTY
    return round(max(_CONF_MIN, min(_CONF_MAX, score)), 2)
