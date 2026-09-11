"""GMP 法规依据映射 — 为 findings 附加知识库引用（借鉴参考产品"7 类知识库检索"）。

设计决策（Round 14）：
- 引用「规范名 + 原则名」而非硬编码具体条款号 —— 错误的条款号在 GMP
  审计场景比无依据更糟；条款级精确引用留给人工复核或后续 RAG 升级。
- 覆盖来源：中国 GMP 2010（批记录管理）、2023 版 GMP 指南（质量控制
  实验室与物料系统）、ALCOA+ 数据可靠性、偏差管理、江苏省药品生产
  记录填写规范、EU GMP Chapter 4（文档）—— 与参考产品的 7 类知识库
  对齐，当前以内置映射表形式落地（最小可行版）。
- 技术噪音类（ocr_noise）与用户自定义规则不映射（user_rule 的依据是
  用户规则本身，见 user_rule_id 溯源）。
"""
from __future__ import annotations

# finding type → 法规依据（一条或多类知识库合并陈述）
GMP_BASIS_MAP: dict[str, str] = {
    # ── 时间/时序类 ──
    "time_reversal": (
        "《药品生产质量管理规范(2010修订)》批记录应及时填写（时序一致性）；"
        "ALCOA+ Contemporaneous（同步性）"
    ),
    "time_anomaly": (
        "《药品生产质量管理规范(2010修订)》批记录应及时填写（时序一致性）；"
        "ALCOA+ Contemporaneous（同步性）"
    ),
    "year_contradiction": (
        "《药品生产质量管理规范(2010修订)》批记录应真实、准确；"
        "ALCOA+ Accurate（准确性）"
    ),
    "suspicious_date": (
        "《药品生产质量管理规范(2010修订)》批记录填写规范（日期真实性）；"
        "江苏省药品生产记录填写规范"
    ),
    # ── 签名类 ──
    "signature_time_anomaly": (
        "《药品生产质量管理规范(2010修订)》操作应及时签名并注明日期；"
        "EU GMP Chapter 4 文档管理；ALCOA+ Attributable（可归因性）"
    ),
    "signature_mismatch": (
        "《药品生产质量管理规范(2010修订)》签名与操作人一致性；"
        "EU GMP Chapter 4 文档管理；ALCOA+ Attributable（可归因性）"
    ),
    # ── 完整性/一致性类 ──
    "completeness": (
        "《药品生产质量管理规范(2010修订)》批生产记录应完整（不得留空/缺项）；"
        "ALCOA+ Complete（完整性）"
    ),
    "step_gap": (
        "《药品生产质量管理规范(2010修订)》批生产记录应完整覆盖全部工序；"
        "ALCOA+ Complete（完整性）"
    ),
    "spec_unverifiable": (
        "《药品生产质量管理规范(2010修订)》批记录数据应可审核、"
        "单位与量纲须一致（无法换算时不得自动判定合格）；"
        "ALCOA+ Accurate（准确性）与数据完整性"
    ),
    "batch_inconsistency": (
        "《药品生产质量管理规范(2010修订)》批号管理与物料平衡；"
        "ALCOA+ Consistent（一致性）"
    ),
    # ── 参数/规格类 ──
    "param_out_of_spec": (
        "《药品生产质量管理规范(2010修订)》工艺参数应在规定范围内；"
        "偏差管理规程（超限应走偏差流程）"
    ),
    # ── 可读性/置信度类 ──
    "low_confidence": (
        "《药品生产质量管理规范(2010修订)》批记录应字迹清晰、不易擦除；"
        "江苏省药品生产记录填写规范；ALCOA+ Legible（清晰性）"
    ),
    "handwritten": (
        "《药品生产质量管理规范(2010修订)》批记录应字迹清晰、不易擦除；"
        "江苏省药品生产记录填写规范；ALCOA+ Legible（清晰性）"
    ),
    # LLM 自由产出的变体 type（prompt 未枚举，e2e 实测出现）
    "batch_logic": (
        "《药品生产质量管理规范(2010修订)》批号管理与物料平衡；"
        "ALCOA+ Consistent（一致性）"
    ),
}

# 不映射依据的 type（技术噪音/用户规则自含依据/内部键）
_UNMAPPED = {"ocr_noise", "user_rule"}

# 关键词兜底：LLM 可能产出任意 type 字符串（e2e 实测 batch_logic 等
# 未枚举变体）。按 type 关键词归类到语义最近的依据，避免整类 finding
# 无依据可引用。顺序敏感：先具体后宽泛。
_KEYWORD_FALLBACK: list[tuple[tuple[str, ...], str]] = [
    (("batch", "批号", "批logic"), GMP_BASIS_MAP["batch_inconsistency"]),
    (("signature", "签名"), GMP_BASIS_MAP["signature_time_anomaly"]),
    (("time", "date", "时间", "日期"), GMP_BASIS_MAP["time_reversal"]),
    (("param", "spec", "参数", "规格"), GMP_BASIS_MAP["param_out_of_spec"]),
    (("sign",), GMP_BASIS_MAP["signature_mismatch"]),
]


def _lookup(f: dict) -> str | None:
    """精确 type 命中 → 关键词兜底 → None。"""
    ftype = str(f.get("type", ""))
    if ftype in _UNMAPPED:
        return None
    if ftype in GMP_BASIS_MAP:
        return GMP_BASIS_MAP[ftype]
    low = ftype.lower()
    for keywords, basis in _KEYWORD_FALLBACK:
        if any(k in low or k in ftype for k in keywords):
            return basis
    return None


def attach_gmp_basis(findings: list[dict]) -> list[dict]:
    """给每条 finding 原地附加 gmp_basis 键（无映射时不设键）。

    幂等：已有非空 gmp_basis 的 finding 不覆盖（保留 LLM 可能给出的
    更精确引用）。
    查找顺序：精确 type → 关键词兜底（Round 15 设计却漏接线 — 2026-08-24
    覆盖率核查发现 _lookup 从未被调用，LLM 变体 type（如 batch_number_
    mismatch）此前拿不到依据；接线后经 _KEYWORD_FALLBACK 归类）。
    """
    for f in findings:
        if not isinstance(f, dict) or f.get("gmp_basis"):
            continue
        basis = _lookup(f)
        if basis:
            f["gmp_basis"] = basis
    return findings
