from __future__ import annotations

import logging

from core.rules.parsing import (
    _interval_after,
    _normalize_batch_no,
    _parse_time,
    _parse_time_interval,
    batch_keys_compatible,
    crosses_midnight,
    literal_has_date,
    page_anchor_date,
)
from core.signature_pairing import pairing_findings as _pairing_findings

# 投票阈值：结构归一后的基准簇须覆盖的有批号页比例。低于此值视为"不决定性"
# → 不做任何吸收（连 Tier 1 结构归一也不落地），按原样全报
# （保守；绝不把真实混批吃掉）。
_BATCH_VOTE_MIN_SHARE = 0.6

logger = logging.getLogger(__name__)


# A step with recorded start/end time but no operator signature is a GMP
# compliance gap (signature missing). Cover/toc pages legitimately lack
# signatures, so we only flag steps that have time data.
# ---------------------------------------------------------------------------


#: 签名角色 → 三个"签名槽"的角色名集合（R95 提取为模块级单一来源）。
#:
#: ⚠️ **R96/CR-2：词表必须覆盖抽取器实际会产出的 role**。实测两份真实批记录
#: （试用批记录 10 页 + 丝裂霉素 51 页）共出现 **22 种** role，旧词表只认 5 种英文名，
#: 于是 `qa_approver`（p1 赵阿巧、p6 孙文莉）、`qc_approval`、`approval`、
#: `review`（19 次）、`checker`（6 次）、`inspector`、`acceptor` 全部落空
#: ⇒ **签了也判"没签"**（10 页 p1/p6 的「缺少 QA 签名」、51 页 12 条
#: 「缺少复核人签名」中的 6 条）。
#:
#: 这是**归一族**，不是精确分类：宁可把同一签批位的不同命名算进来，也不要漏。
#: `负责人`/`supervisor`/`主管` 三个**故意不收录** —— 它们在清洗记录里是"工序负责人
#: 检查"位，与 QA 批准不是同一件事，混进来会把"模板无 QA 栏"整条吞掉；
#: 该情形由 CR-3 以模板级文案单独处理。
_OPERATOR_ROLES_DOC = ("operator", "操作人", "操作员", "执行人")

#: 复核族：`review`/`checker`/`inspector`/`acceptor` 都是抽取器对"检查人/复核人"的
#: 不同命名，实测分别出现 19/6/2/2 次。
_REVIEWER_ROLES_DOC = (
    "reviewer", "review", "checker", "inspector", "acceptor",
    "workshop_reviewer", "复核人", "复核员", "检查人",
)

#: QA/审批族：`qa_approver`（2 次，均带姓名）、`qc_approval`、`qc_approver`、
#: `approval` 都是表单上的 QA/批准签批位。
_QA_ROLES_DOC = (
    "qa", "qa_reviewer", "qa_approver", "qc_approval", "qc_approver",
    "approval", "质量保证", "qa审核", "批准人", "放行人",
)

#: "签名缺失"类文案的**归因后缀**（R95/A7）。
#: 实测（试用批记录 p6）：执行人/检查人栏明明有手写签名与日期，PaddleOCR-VL
#: 整列漏识别 ⇒ 结构化里 signatures=[] ⇒ 规则报"缺少签名"。把"没识别到"
#: 写成"记录没签"是**归因错误**，会让复核员去改一份本来就合规的记录。
_OCR_ATTRIBUTION = "（如原页已签，属 OCR 漏识别）"


def _has_signed(sigs: list, roles: tuple[str, ...]) -> bool:
    """角色集合里是否存在**姓名非空**的签名。

    ⚠️ 只看 role 是不够的（R95/A8）：结构化抽取会保留
    ``{"role": "reviewer", "name": ""}`` 这种"有行无值"的槽位 ——
    试用批记录 p8 第二张环境监测表的复核人栏确实空白，旧实现却因为
    role 命中而判为"有复核人" ⇒ **漏报**。姓名必须非空才算签了名。
    """
    for s in sigs or []:
        if not isinstance(s, dict):
            continue
        role = str(s.get("role") or "").strip().lower()
        if role not in roles:
            continue
        if str(s.get("name") or "").strip():
            return True
    return False


def _blank_slots(sigs: list, roles: tuple[str, ...]) -> int:
    """角色命中但**姓名为空**的签名槽位数（R95/A8）。

    与 :func:`_has_signed` 的分工：那个回答"这一页到底签没签"，这个回答
    "抽取出几个空槽"。实测（试用批记录 p8）：同一 step 里既有
    `reviewer=陈一`（第一张表）又有 `reviewer=""`（第二张表复核人栏确实空白）
    ⇒ `_has_signed` 为 True，"有没有复核人"这个布尔量看不见空槽。
    只有**抽取器显式产出**了该角色的空行才算数 —— 模板里根本没有这一栏的页
    不会凭空多出一个空槽（所以不会像"数检查人 vs 数复核人"那样误报）。
    """
    n = 0
    for s in sigs or []:
        if not isinstance(s, dict):
            continue
        if str(s.get("role") or "").strip().lower() not in roles:
            continue
        if not str(s.get("name") or "").strip():
            n += 1
    return n


def _blank_slot_findings(pno, step_no, sigs, seen) -> list[dict]:
    """同一 step 里"有人签 + 有空槽" ⇒ 逐行核对提示（R95/A8）。

    **只在同一角色确实有人签时才补报**：那证明这一栏在页面上存在，空的那行
    是真空白；模板里根本没有这一栏的页不会凭空多出空槽（见 `_blank_slots`）。

    R112/P8 抽成函数：无执行时间的步骤也要跑这一段 —— 实测 p8 那张称量表
    整行没写时间，旧实现 `continue` 掉之后，复核人空槽**一次都没被检查过**。
    """
    out = []
    if _has_signed(sigs, _REVIEWER_ROLES_DOC):
        n_blank = _blank_slots(sigs, _REVIEWER_ROLES_DOC)
        key = (pno, str(step_no), "reviewer_blank")
        if n_blank and key not in seen:
            seen.add(key)
            out.append({
                "page": pno,
                "type": "completeness",
                "severity": "warning",
                "description": (
                    f"第{pno}页 工序{step_no} 有 {n_blank} 处复核人签名栏"
                    f"为空（同页另有已签署的复核人，属逐行核对）"
                ),
                "ocr_text": f"step_no={step_no} reviewer_blank={n_blank}",
                "operator": "",
                "source": "rule",
            })
    if _has_signed(sigs, _OPERATOR_ROLES_DOC):
        n_blank = _blank_slots(sigs, _OPERATOR_ROLES_DOC)
        key = (pno, str(step_no), "operator_blank")
        if n_blank and key not in seen:
            seen.add(key)
            out.append({
                "page": pno,
                "type": "completeness",
                "severity": "warning",
                "description": (
                    f"第{pno}页 工序{step_no} 有 {n_blank} 处操作人签名栏"
                    f"为空（同页另有已签署的操作人，属逐行核对）"
                ),
                "ocr_text": f"step_no={step_no} operator_blank={n_blank}",
                "operator": "",
                "source": "rule",
            })
    return out


def _check_completeness(pages: list[dict]) -> list[dict]:
    findings = []
    seen: set[tuple] = set()  # dedup by (page, step_no, issue) — LLM may emit dup steps
    for page in pages:
        pno = page["page"]
        # R119 / TODO 0-48（R111 遗留 #146）：签名栏「姓名 + 日期」成对判定。
        #
        # 与下面 `_blank_slot_findings` 的分工：那个说"格全空"，这个说"格填了
        # 一半"。两者是同一族事实（签名位不完整），故同类型同 severity，
        # 靠 `ocr_text` 里的 label 区分，互不吞并。
        #
        # 只在调用方把 OCR 原文带下来时生效（`stage3` 已带，见 `_normalize_pages`）；
        # 不带 ⇒ 整段跳过，既有判据行为不变。
        raw = page.get("raw_html") or ""
        if raw:
            for f in _pairing_findings(pno, raw):
                key = (pno, "pairing", f["ocr_text"])
                if key in seen:
                    continue
                seen.add(key)
                findings.append(f)
        for step in page["steps"]:
            has_time = bool(step.get("start_time") or step.get("end_time"))
            has_operation = bool(step.get("operation"))
            sigs = step.get("signatures", []) or []
            step_no = step.get("step_no", "?")
            if not has_time:
                # 封面/目录/纯标题行：既没时间也没签名 ⇒ 不是"已执行步骤"，不看
                if not has_operation and not sigs:
                    continue
                # Steps with operation but no time — flag as GMP gap
                # (missing date means execution time cannot be traced)
                key = (pno, str(step_no), "no_time")
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "warning",
                        "description": (
                            f"第{pno}页 工序{step_no} "
                            f"{(step.get('operation') or '')[:40]} "
                            f"有操作描述但缺少执行时间记录"
                        ),
                        "ocr_text": f"step_no={step_no} time=空",
                        "operator": "",
                        "source": "rule",
                    })
                # R112/P8：**不整段 continue** —— 签名槽位与"有没有写时间"是
                # 两件事。实测 p8「生产前检查」整行没写时间，而同一行里有
                # `reviewer=""`（复核人栏空白）⇒ 旧实现连签名都不看，用户
                # 反复反馈"复核人没填却没提示"。
                #
                # 这里只补**空槽位**，不补"整类缺失"（未识别到操作人/复核人/
                # QA）：后者依赖"这一步已执行"这个前提，而没有时间记录的标题
                # 行会因此凭空多出三条噪声。空槽位有"同角色另有具名签署"作证，
                # 不依赖该前提。
                findings.extend(_blank_slot_findings(pno, step_no, sigs, seen))
                continue
            step_no = step.get("step_no", "?")
            sigs = step.get("signatures", []) or []
            # R95：三个槽位一律要求**姓名非空**（见 _has_signed docstring）
            has_operator = (bool(str(step.get("operator") or "").strip())
                            or _has_signed(sigs, _OPERATOR_ROLES_DOC))
            has_reviewer = (bool(str(step.get("reviewer") or "").strip())
                            or _has_signed(sigs, _REVIEWER_ROLES_DOC))
            has_qa = _has_signed(sigs, _QA_ROLES_DOC)
            if not has_operator:
                key = (pno, str(step_no), "operator")
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "warning",
                        "description": (
                            f"第{pno}页 工序{step_no} "
                            f"{(step.get('operation') or '')[:40]} "
                            f"已记录操作时间但未识别到操作人签名{_OCR_ATTRIBUTION}"
                        ),
                        "ocr_text": f"step_no={step_no} operator=空",
                        "operator": "",
                        "source": "rule",
                    })
            if not has_reviewer:
                key = (pno, str(step_no), "reviewer")
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "info",
                        "description": (
                            f"第{pno}页 工序{step_no} "
                            f"{(step.get('operation') or '')[:40]} "
                            f"未识别到复核人签名{_OCR_ATTRIBUTION}"
                        ),
                        "ocr_text": f"step_no={step_no} reviewer=空",
                        "operator": "",
                        "source": "rule",
                    })
            if not has_qa:
                # QA signature required for GMP compliance on executed steps
                key = (pno, str(step_no), "qa")
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "info",
                        "description": (
                            f"第{pno}页 工序{step_no} "
                            f"{(step.get('operation') or '')[:40]} "
                            f"未识别到 QA 签名{_OCR_ATTRIBUTION}"
                        ),
                        "ocr_text": f"step_no={step_no} qa=空",
                        "operator": "",
                        "source": "rule",
                    })
            # R95/A8：抽取器**显式产出**的空槽位（同一 step 里既有人签了
            # 又有空槽时，上面三个布尔量都看不见它）。实测 p8：第二张环境
            # 监测表的复核人栏空白，因为同一 step 的第一张表有「陈一」，
            # `has_reviewer` 为 True ⇒ 漏报。只在 has_* 为 True 时补报，
            # 避免与上面的"整类缺失"重复。
            # R112/P8：与"无时间"分支共用同一实现（原先两处手抄，改一处必漏一处）
            findings.extend(_blank_slot_findings(pno, step_no, sigs, seen))
    return findings

# image. Info-level only: never blocks, but tells the reviewer where to look.
# ---------------------------------------------------------------------------


def _check_handwritten_notes(pages: list[dict]) -> list[dict]:
    findings = []
    seen: set[tuple] = set()  # dedup by (page, step_no, first note)
    for page in pages:
        pno = page["page"]
        for step in page["steps"]:
            notes = [str(n).strip() for n in (step.get("handwritten") or []) if str(n).strip()]
            if not notes:
                continue
            step_no = step.get("step_no", "?")
            key = (pno, str(step_no), notes[0])
            if key in seen:
                continue
            seen.add(key)
            preview = notes[0][:30]
            extra = len(notes) - 1
            findings.append({
                "page": pno,
                "type": "handwritten",
                "severity": "info",
                "description": (
                    f"第{pno}页 工序{step_no} 含 {len(notes)} 条手写内容"
                    f"（如「{preview}」{f' 等 {extra} 条' if extra else ''}）"
                    f"，手写体 OCR 易误读，请对照 PDF 原页人工核对"
                ),
                "ocr_text": "；".join(notes)[:200],
                "operator": "",
                "source": "rule",
            })
    return findings

# R7: batch number consistency — all pages should share the same batch_no.
# Mixed batch numbers across pages indicate binding errors or cross-batch
# contamination (serious GMP deviation).
# ---------------------------------------------------------------------------


def _check_batch_consistency(pages: list[dict]) -> list[dict]:
    """Check that all pages with a batch_no use the same value.
    Pages without batch_no (cover, toc, appendix) are skipped.

    OCR variants of the SAME batch number are grouped before comparison:
    separators ("·"/spaces/"°"), symbol noise ("^*"/"/"), full-width glyphs,
    digit confusions ("25010i" → "250101"), sheet-suffix numbers
    ("1127011N250101-04") and prefix-truncated extractions (`112701` — the
    LLM dropped the tail) all collapse onto one group.

    Grouping is **structure-first and order-independent** (B1-10). Each
    normalized core is decomposed into `(prefix, date6)`; two cores may be
    the same batch only when their date segments match exactly. Tiers:

    * **Tier 1 (structural)** — prefix containment / tail truncation. The
      relation is decidable, so it needs no vote.
    * **Tier 2 (weak)** — a single-character prefix difference (`N`↔`1`).
      Only absorbed when the Tier-1 cluster already forms a majority.

    A **vote** covers the whole cluster: unless it reaches
    `_BATCH_VOTE_MIN_SHARE` of the pages carrying a batch no, *nothing* is
    absorbed and every core is reported as-is (fail-open — 宁可多报，不可把
    真实混批吃掉). The absorbed aliases are disclosed in the description."""
    findings = []
    # normalized core (before "-suffix") -> {raw batch_no: [page numbers]}
    cores: dict[str, dict[str, list[int]]] = {}
    for page in pages:
        pno = page["page"]
        # batch_no 由 _normalize_pages 嵌套在 page_info 中（生产 schema）
        bno = ((page.get("page_info") or {}).get("batch_no") or "").strip()
        if not bno:
            continue
        norm = _normalize_batch_no(bno)
        if not norm:
            continue
        # Sheet suffixes ("-02"…"-06" 工序页号) belong to the same batch core
        raw_map = cores.setdefault(norm.split("-")[0], {})
        raw_map.setdefault(bno, []).append(pno)

    if len(cores) <= 1:
        return findings  # all same (or none) — consistent

    counts = {c: sum(len(v) for v in m.values()) for c, m in cores.items()}
    total_pages = sum(counts.values())
    # 锚 = 页数最多的核心串（**不再按长度** —— 旧实现让噪声最重的那串当 host，
    # 组标签因此变成 `1127011N4250101` 这种 OCR 噪声）。
    ranked = sorted(cores, key=lambda c: (-counts[c], c))
    anchor = ranked[0]

    # ── Tier 1：结构归一（对称、无顺序依赖、关系可判定）──────────────────
    # 前缀包含 / 尾段截断属**结构性**证据：一方是另一方的严格前缀时只能是截断读法
    # （实测 14 页只读出 `112701`，完整读法是 `1127011N250101`）；两侧日期段相等
    # 时前缀互相包含同理。
    tier1 = [c for c in ranked[1:]
             if batch_keys_compatible(c, anchor, allow_edit_distance=False)]
    tier2 = [c for c in ranked[1:]
             if c not in tier1
             and batch_keys_compatible(c, anchor, allow_edit_distance=True)]

    # ── 投票闸：Tier 1 簇须构成多数，否则**一条都不吸收**（fail-open）────────
    # 保守性红线：宁可多报，不可把真实混批吃掉。`B202201` vs `B202202`（各 1 页）
    # 既不结构兼容、也不成多数 ⇒ 照原样全报。
    cluster_pages = counts[anchor] + sum(counts[c] for c in tier1)
    decisive = total_pages > 0 and cluster_pages >= (
        _BATCH_VOTE_MIN_SHARE * total_pages
    )

    absorbed: list[str] = []
    survivors: list[str] = []
    if decisive:
        absorbed = list(tier1) + list(tier2)
        survivors = [c for c in ranked[1:] if c not in absorbed]
        grouped: list[tuple[str, dict[str, list[int]]]] = [
            (anchor, cores[anchor]),
            *((c, cores[c]) for c in survivors),
        ]
    else:
        grouped = [(c, cores[c]) for c in ranked]

    if len(grouped) <= 1:
        logger.info(
            f"R7 batch vote: 单一批号（基准 {anchor} 覆盖 "
            f"{cluster_pages}/{total_pages} 页；吸收 {len(absorbed)} 个变体 "
            f"{absorbed}）"
        )
        return findings

    # Report: 基准组 + 存活的可疑组，并明示被归并的变体
    summary_parts = []
    for core, raw_map in grouped:
        pns = sorted(p for pages_ in raw_map.values() for p in pages_)
        summary_parts.append(f"{core}(第{','.join(str(p) for p in pns)}页)")
    all_raw = [raw for m in cores.values() for raw in m]
    n_variants = len(all_raw) - len(grouped)
    vote_note = (
        f"，其中 {len(absorbed)} 个读法按「末尾日期段相同/尾段截断」的结构证据"
        f"并入基准（如 {'、'.join(absorbed[:3])}）"
        if absorbed else ""
    )
    findings.append({
        "page": min(min(v) for v in grouped[0][1].values()),
        "type": "batch_inconsistency",
        "severity": "critical",
        "description": (
            f"跨页批号不一致：检测到 {len(grouped)} 组不同批号 — "
            f"{'；'.join(summary_parts[:3])}"
            f"{'…' if len(summary_parts) > 3 else ''}"
            f"（已归并 {n_variants} 个空格/分隔符/角标/工序后缀/尾段截断等 OCR 变体"
            f"{vote_note}），"
            f"请核对是否装订错误或混批"
        ),
        "ocr_text": (
            f"batch_nos={all_raw}"
            + (f"；merged_aliases={sorted(absorbed)}" if absorbed else "")
        ),
        "operator": "",
        "source": "rule",
    })
    logger.warning(
        f"R7 batch inconsistency: {len(grouped)} distinct batch groups "
        f"({len(all_raw)} raw variants, {n_variants} merged, {len(absorbed)} clustered)"
    )
    return findings


# ---------------------------------------------------------------------------
# R8: check consistency — a QA/review checkbox answered "否" (failed) must
# be traceable to a deviation record; answered "是" on an item that is
# clearly N/A-safe is fine. The check data comes from steps[].checks[]
# (printed "√是/□否" option templates are NOT real checkboxes — the LLM
# prompt filters those; here we only judge what survived extraction).
#
# ⚠️ 文案**刻意中性**（2026-09-21，B1-15 同轮回放发现）：
# 「否」到底是好是坏**取决于题面的极性**，而题面极性我们没有可靠判据：
#   - 「地面是否干净」答“否” ⇒ 真问题；
#   - 「是否发生偏差」「清洗过程是否无异常」答“否” ⇒ **正常答案**（无偏差/无异常）。
# 实测 p51 三条（是否发生偏差 / OOS/OOT / 变更）就是这样被误判方向的。
# 旧文案断言"需确认是否已启动偏差处理并在记录中留痕"，对否定式提问是**反向**的。
# 🔴 **不得**用"题目里有没有『是否发生』『无异常』"这类**措辞词表**去翻转档位 ——
# 那正是 B1-12 已明令禁止并修掉的病（布尔类处置档位不得由措辞决定）。
# 故只做两件事：**陈述事实**（勾选为“否”）+ **请人工确认**，不替用户判断语义。
# 定级仍为 warning：对「地面是否干净」这类题面，否确实需要人看一眼。
# ---------------------------------------------------------------------------

# 「空框字形」= 勾选标记的**空**状态（未勾选）。
#
# B1-15：`selected='否'` 配一个空框是**记录内部自相矛盾** —— 一个"被选中"的
# 选项不可能配空框（p45 `「是否将柱内甲醇压干」selected='否' marker='☐'`）。
# 该条的 `selected` 因此**不可信**，不能据此断言"勾选为否 ⇒ 需确认是否已启动
# 偏差处理"（实测 p45/p39/p46 三页 8 条 warning **全部**是假阳性，原页核验：
# 有 √ 的是「是」框）。
#
# ⚠️ 三条边界（都是"判不了 ≠ 判据确凿"的落地，改前务必读完）：
#   ① **只认这 5 个字形**，不扩大到 `⬜` / `🔲` 等（未实测，不猜）；
#   ② **只认"整串都是空框"**：`√☐` 这类混合里有勾号 ⇒ 说不清 ⇒ 维持原判
#      （fail-open，宁可留着让人看一眼，也不静默吞掉一个可能的真偏差）；
#   ③ **marker 缺失/空白不算矛盾** —— 缺失是"不知道"，不是"矛盾" ⇒ 维持原判。
_EMPTY_BOX_GLYPHS = frozenset("☐□◻○◯")


def _is_empty_box_only(marker: str) -> bool:
    """marker 非空、且其中**每个非空白字符**都是空框字形。

    混合（`√☐`）、含勾号（`√`）、空白、缺失 一律返回 False ⇒ fail-open。
    """
    if not marker:
        return False
    chars = [ch for ch in marker if not ch.isspace()]
    if not chars:
        return False
    return all(ch in _EMPTY_BOX_GLYPHS for ch in chars)


def _check_check_consistency(pages: list[dict]) -> list[dict]:
    findings = []
    for page in pages:
        pno = page["page"]
        for step in page["steps"]:
            for c in step.get("checks", []) or []:
                selected = (c.get("selected") or "").strip()
                item = (c.get("item") or "").strip()
                marker = (c.get("marker") or "").strip()
                if not selected or not item:
                    continue
                if selected == "否":
                    if _is_empty_box_only(marker):
                        # 自相矛盾 ⇒ 降为 info 并**如实**说明矛盾点，不再断言
                        # "勾选为否"（B1-15）。不静默丢弃：矛盾本身仍值得人看一眼。
                        findings.append({
                            "page": pno,
                            "type": "completeness",
                            "severity": "info",
                            "description": (
                                f"第{pno}页 检查项「{item[:40]}」勾选标记与选项不一致"
                                f"（标记为未勾选的空框 {marker}，却读作“否”），"
                                f"该选项不可信，请对照 PDF 原页人工核对"
                            ),
                            "ocr_text": f"item={item[:40]} selected=否 marker={marker}",
                            "operator": "",
                            "source": "rule",
                        })
                        continue
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "warning",
                        "description": (
                            f"第{pno}页 检查项「{item[:40]}」勾选为“否”"
                            f"（{marker or '勾选标记'}），请对照 PDF 原页确认该答案"
                            f"是否为预期"
                        ),
                        "ocr_text": f"item={item[:40]} selected=否",
                        "operator": "",
                        "source": "rule",
                    })
                elif selected == "无法识别":
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "info",
                        "description": (
                            f"第{pno}页 检查项「{item[:40]}」勾选状态无法识别"
                            f"（{marker or '手绘勾选'}），请对照 PDF 原页人工核对"
                        ),
                        "ocr_text": f"item={item[:40]} selected=无法识别",
                        "operator": "",
                        "source": "rule",
                    })
    return findings

# OCR may misread handwritten values (e.g. "0.974" → "0.914"). Low confidence
# values that pass spec check could mask a real out-of-spec result. We flag
# them for human verification rather than silently trusting the OCR output.
# ---------------------------------------------------------------------------


def _check_low_confidence_params(pages: list[dict]) -> list[dict]:
    """Flag parameter values with low OCR confidence for human review.

    A low-confidence value that passes spec check might actually be
    out-of-spec if the OCR misread a handwritten digit. We surface these
    cases so reviewers can verify against the original document.
    """
    findings = []
    seen: set[tuple] = set()
    for page in pages:
        pno = page["page"]
        # 空页短路（_ocr_empty）不触发低置信度规则：空页未执行 LLM
        # 分析，"置信度较低/手写体干扰"的描述与"此页无 OCR 内容"横幅
        # 自相矛盾，且对无可分析内容的页面是误导性提示。空页已有专属
        # 横幅 + 人工复核路径。
        data = page.get("data") or {}
        if page.get("_ocr_empty") or data.get("_ocr_empty"):
            continue
        # overall_confidence is emitted by the per-page LLM (v3 prompt). In
        # _normalize_pages it stays nested under page["data"]; unit tests
        # pass it at the top level. Check both locations so the rule fires
        # in production and in tests.
        overall_conf = (
            page.get("overall_confidence")
            or data.get("overall_confidence")
            or ""
        ).lower()
        # If overall page confidence is low, flag the whole page
        if overall_conf == "low":
            key = (pno, "page_low_confidence")
            if key not in seen:
                seen.add(key)
                findings.append({
                    "page": pno,
                    "type": "completeness",
                    "severity": "info",
                    "description": (
                        f"第{pno}页 整体识别置信度较低，"
                        f"建议人工核对 OCR 结果（可能存在手写体或印章干扰）"
                    ),
                    "ocr_text": f"overall_confidence={overall_conf}",
                    "operator": "",
                    "source": "rule",
                })
    return findings


def _check_measurement_time_sequence(pages: list[dict]) -> list[dict]:
    """R-M1: 同一工序（step_no）的跨页测量时间序列应单调不减。

    批记录的参数矩阵常跨页（大表在 51 页实测文件中跨页出现），R1-a/b 只
    比较 step.start_time/end_time，逐行测量时间（measurements[].time）从未
    被校验——缺行/行序错乱/时间倒序会静默通过。此规则把同 step_no 的所有
    页测量行按 (页序, 行序) 拼接后检查严格倒序（time_prev > time_curr）。
    相等的重复测量（同一分钟多次取样）合理，不报告；解析失败点打断连续
    性（避免把跨无效时间的行误判为倒序）。
    """
    findings: list[dict] = []
    groups: dict[str, list[dict]] = {}
    for page in pages:
        pno = page["page"]
        # CR-5：`production_date` 常为空（51 页实测 22 页空），纯时刻根本解析
        # 不了 ⇒ 该页整段序列静默失效。回落到该页签名日期众数。
        fb_date = page_anchor_date(page)
        for step in page["steps"]:
            key = str(step.get("step_no", "?"))
            for m in step.get("measurements", []) or []:
                if not isinstance(m, dict):
                    continue
                groups.setdefault(key, []).append({
                    "page": pno,
                    "time_raw": m.get("time"),
                    "anchor": fb_date,
                    "t": _parse_time(m.get("time"), fb_date),
                    "iv": _parse_time_interval(m.get("time"), fb_date),
                })
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        prev: dict | None = None
        for row in rows:
            if row["t"] is None:
                prev = None  # 解析失败点打断连续性，避免误报
                continue
            # CR-6：跨页且两侧**锚点不同**（各自由本页推定出的日期）时，两行
            # 根本不属同一天，逐时比较不成立。实测 p22 工序3「T2103 洗脱」
            # 与 p38 工序3「S2101 陶瓷膜清洗」是**不同岗位的同号工序**，旧实现
            # 把 p22 的 12:50 与 p38 的 17:29 拼成"倒序"（#1512）。
            # ⚠️ 只在**两侧都没写日期**时豁免：任一侧自带日期时，事实在字面上，
            # 照比（两页都带日期且锚点不同 = 页面日期抽错了，不是"不该比"）。
            if (prev is not None and prev["page"] != row["page"]
                    and prev["anchor"] and row["anchor"]
                    and prev["anchor"] != row["anchor"]
                    and not literal_has_date(prev["time_raw"])
                    and not literal_has_date(row["time_raw"])):
                prev = row
                continue
            # CR-6：两侧都不带日期且 curr 早于 prev ⇒ 跨零点（跨日生产的正常
            # 写法，如 p11 的 23:04 → 00:04、p33 的 20:21 → 00:17）。
            if prev is not None and crosses_midnight(
                prev["time_raw"], row["time_raw"], prev["t"], row["t"]
            ):
                prev = row
                continue
            # Interval comparison: date-only times span the whole day, so a
            # date-only row that touches the previous datetime row on the
            # same day is not a reversal (e.g. "2024-01-01" vs "2024-01-01 14:30").
            if prev is not None and prev["iv"] and row["iv"] and _interval_after(prev["iv"], row["iv"]):
                findings.append({
                    "page": row["page"],
                    "type": "time_reversal",
                    "severity": "warning",
                    "description": (
                        f"工序{key} 测量时间跨页倒序：第{prev['page']}页 "
                        f"{prev['time_raw']} 晚于 第{row['page']}页 {row['time_raw']}"
                        f" — 可能表格缺行、行序错乱或 OCR 提取错误，请人工核对参数矩阵"
                    ),
                    "ocr_text": f"{prev['time_raw']} > {row['time_raw']}",
                    "operator": "",
                    "source": "rule",
                })
            prev = row
    return findings


def _check_measurement_column_consistency(pages: list[dict]) -> list[dict]:
    """R-M2: 同一工序（step_no）跨页参数矩阵列集合应一致。

    大表跨页时每页表头重印；若某页测量行 values 键缺失（整列丢失），
    R3 的逐格判定无法发现"整列缺失"（它只看有值的格）。此规则只报
    "缺列"（多出来的列可能是新增参数，不误报），且要求该 step 跨 ≥2 页
    且有 ≥2 行数据，避免小样本噪音。
    """
    findings: list[dict] = []
    groups: dict[str, dict] = {}
    for page in pages:
        pno = page["page"]
        for step in page["steps"]:
            key = str(step.get("step_no", "?"))
            g = groups.setdefault(key, {"pages": set(), "rows": 0, "page_cols": {}})
            rows = step.get("measurements", []) or []
            g["rows"] += len(rows)
            cols: set[str] = set()
            for m in rows:
                if isinstance(m, dict) and isinstance(m.get("values"), dict):
                    cols.update(m["values"].keys())
            if cols:
                g["pages"].add(pno)
                g["page_cols"][pno] = cols
    for key, g in groups.items():
        if len(g["pages"]) < 2 or g["rows"] < 2:
            continue
        all_cols: set[str] = set()
        for cols in g["page_cols"].values():
            all_cols.update(cols)
        for pno in sorted(g["page_cols"]):
            missing = all_cols - g["page_cols"][pno]
            if not missing:
                continue
            missing_sorted = sorted(missing)
            findings.append({
                "page": pno,
                "type": "completeness",
                "severity": "info",
                "description": (
                    f"工序{key} 参数矩阵跨页列不一致：第{pno}页缺列 "
                    f"{'、'.join(missing_sorted)}（其余页均有）— "
                    f"可能表格截断或提取丢失，请人工核对"
                ),
                "ocr_text": "missing: " + "、".join(missing_sorted),
                "operator": "",
                "source": "rule",
            })
    return findings
