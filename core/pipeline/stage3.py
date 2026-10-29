"""Stage 3 — cross-page analysis + findings persistence (module refactor)"""
from __future__ import annotations

import json
import logging
import re
import time

from core.pipeline.state import _audit_log, transition_status

logger = logging.getLogger(__name__)

#: 同一族内的类型视为同一类缺陷（**只用于跨源去重**，不改变落库 type）：
#: 实测（试用批记录 p3）同一处"签名姓名不一致"被页级 LLM 标成
#: `signature_time_anomaly`、跨页 LLM 标成 `signature_mismatch` ⇒ 纯按 type
#: 去重会漏掉这一对重复。
_TYPE_FAMILY: dict[str, str] = {
    "signature_mismatch": "signature",
    "signature_time_anomaly": "signature",
}

#: 允许被跨源去重抑制的来源。`user_rule` **豁免**：用户显式规则与规则层撞
#: (page, type) 是正常共存，不是重复。
_DEDUP_SOURCES = ("llm_cross", "llm_fallback")


#: R132 / TODO 0-75：**同一日期字面量**上的日期类主张（跨源/同源去重）。
#:
#: 实测（试用批记录 p8）：页级 LLM 报 `signature_time_anomaly`（"复核人签名
#: 日期 2015.6.03 早于生产日期 2026.06.30"），跨页 LLM 又把同一个 2015.6.03
#: 说成 `suspicious_date`（"年份早于 2000……篡改嫌疑"）。两条的**证据字面量
#: 完全相同**（那个 2015 是 OCR 把 2025 读错），只是标签不同 ⇒ 复核页看到
#: 两张卡片指向同一处、其中一张还升级成指控。
#:
#: 判据刻意只认**字面量集合相交**，不认"日期类"泛化。而且**只限两类**：
#: ``suspicious_date`` 与 ``signature_time_anomaly`` —— 它们是"同一处签名日期
#: 读数异常"的两种说法（实测 p8 正是如此）。**排除** ``year_contradiction`` /
#: ``time_reversal``：它们断言的是**关系**（两个审批年份不一致 / 结束早于开始），
#: 即使文案里出现了同一个字面量也是另一件事。反例实测（51 页 p2）：
#: #5 suspicious_date 与 #7 year_contradiction 共享 2025.01.30 的 OCR 文本，
#: 但 #7 讲的是 review 事件年份不一致 —— 按字面量合并会**漏报**。
_DATE_DEDUP_TYPES = frozenset({"suspicious_date", "signature_time_anomaly"})
_DATE_LITERAL_RE = re.compile(
    r"(\d{4})\s*[.\-/年]\s*(\d{1,2})(?:\s*[.\-/月]\s*(\d{1,2})\s*日?)?"
)


def _date_claim_literals(f: dict) -> set[str]:
    """finding 文案里出现的日期字面量（去空白归一，如 ``2015.6.03``）。"""
    text = f"{f.get('description') or ''} {f.get('ocr_text') or ''}"
    return {re.sub(r"\s+", "", m.group(0)) for m in _DATE_LITERAL_RE.finditer(text)}


def _same_literal_covered_by(
    f: dict,
    ftype: str,
    src: str,
    covered: dict[tuple[int, str], str],
) -> str | None:
    """同页 + **同一日期字面量**已被其他 LLM 条目报告 ⇒ 返回覆盖者。

    ``covered`` 由 ``(page, literal) -> "llm_page"/"llm_cross"`` 组成。
    只对 :data:`_DEDUP_SOURCES` 与 :data:`_DATE_DEDUP_TYPES` 生效；
    规则层的日期条目走 :func:`_overlap_covered_by` 的精确类型判据，不在此处。
    """
    # 规则层/用户规则的日期条目**豁免**：规则层用精确类型判据
    # （`_overlap_covered_by`）。若在这里按字面量吞规则发现，等于让一条
    # LLM 误读（2015 实为 2025）去删掉确定性判据 —— 方向反了。
    if src not in _DEDUP_SOURCES or ftype not in _DATE_DEDUP_TYPES:
        return None
    for lit in _date_claim_literals(f):
        owner = covered.get((f["page"], lit))
        if owner:
            return f"{owner}_same_literal"
    return None


def _overlap_covered_by(
    page: int,
    ftype: str,
    src: str,
    rule_covered: set[tuple],
    llm_page_covered: set[tuple],
) -> str | None:
    """跨源去重：返回覆盖者（``"rule"`` / ``"llm_page"``），不重复则 ``None``。

    ⚠️ **两侧口径刻意不对称**（R95/A9，护栏
    `tests/unit/test_r95_manual_labels.py::TestCrossSourceDedup`）：

    - `rule_covered` 用**精确类型**。规则层是权威版本，一旦允许"同族"覆盖，
      一条「签名日期倒置」的规则发现就会**静默吞掉** LLM 独立观察到的
      「签名人姓名不符」—— 那是两个不同缺陷，属于**漏报**，比噪声严重。
    - `llm_page_covered` 套族。两者是同一个生成器对同一页证据的两次表述，
      只是类型标签不同（实测 p3），合并才对。
    """
    if src not in _DEDUP_SOURCES:
        return None
    if (page, ftype) in rule_covered:
        return "rule"
    if (page, _TYPE_FAMILY.get(ftype, ftype)) in llm_page_covered:
        return "llm_page"
    return None


def _unreadable_signature_pages(findings: list[dict]) -> set[int]:
    """规则层判定「整页签名辨认不可靠」的页码集合（R104 / P1-4）。

    ⚠️ **刻意不调用 `normalize_finding_type`**：`test_type_sync.py` 的
    「归一某集合 ⇒ 该集合必须先富集」护栏会把**任何**对 `findings` 的归一
    当成"改写了集合"，而本函数只读不写。更本质的理由是：这个类型是
    **规则层自己写死的规范值**（`rule_gmp._check_self_review` 里就是字面量），
    不存在需要归一的自由枚举 ⇒ 一次大小写无关的比较足够，不需要归一。
    """
    return {
        f["page"] for f in findings
        if isinstance(f, dict)
        and f.get("source") == "rule"
        and str(f.get("type") or "").strip().lower() == "signature_unreadable"
    }


def _is_unfounded_self_review(
    page: int, ftype: str, src: str, unreadable_pages: set[int],
) -> bool:
    """LLM 的"同一人复核"断言是否**建立在规则层已判为不可靠的读数上**。

    只拦 `self_review`：别的类型不依赖"两个名字读得准"这个前提。
    来源限制与跨源去重同一份（`_DEDUP_SOURCES`）—— 规则层与 `user_rule`
    的产出是判据来源，不复核。
    """
    return (
        src in _DEDUP_SOURCES
        and ftype == "self_review"
        and page in unreadable_pages
    )


async def _collapse_persisted_document_facts(db, job_id: str) -> dict:
    """把**已落库**的页级 LLM「文档级事实」收敛为一条（TODO 0-64）。

    为什么必须单独一层：`reduce_document_level_facts` 只跑在 Stage 3 的**内存**
    findings 上，而 `llm_page` 在 Stage 2 每页完成时就已落库、**不在那个列表里**
    ⇒ 页级 LLM 在 p5 与 p10 各报一条「批生产记录缺少生产日期信息」，两条谁也
    收不到（实测 `4cffb358`，10 页，复核页上就是两张一模一样的卡片）。

    判据在 `core.finding_noise.plan_document_level_collapse`（纯函数，复用 CR-10
    的 `document_fact_kind` 分类器 —— 不新造词表，两套词表必然漂移）。这里只
    执行计划：改保留行 → 删多余行 → 落审计。
    """
    from core.finding_noise import plan_document_level_collapse
    from core.pipeline import db_lock

    cursor = await db.execute(
        "SELECT id, page, type, severity, description, source, status, "
        "guard_reason FROM findings WHERE job_id = ? "
        "AND source IN ('llm_page', 'llm_cross') "
        "AND type IN ('completeness', 'suspicious_date')",
        (job_id,),
    )
    plan = plan_document_level_collapse([dict(r) for r in await cursor.fetchall()])
    if not plan["update"]:
        return plan
    async with db_lock:
        for fid, cols in plan["update"].items():
            await db.execute(
                "UPDATE findings SET description = ?, severity = ?, ocr_text = ?, "
                "guard_reason = ? WHERE id = ? AND job_id = ?",
                (cols["description"], cols["severity"], cols["ocr_text"],
                 cols["guard_reason"], fid, job_id),
            )
        if plan["delete_ids"]:
            marks = ",".join("?" * len(plan["delete_ids"]))
            # 计划与执行之间可能插入复核动作（人工刚好确认了那条）⇒ SQL 再挡一次：
            # 只删 pending。判据在计划里，这是**兜底围栏**，不是第二套判据。
            await db.execute(
                f"DELETE FROM findings WHERE job_id = ? AND status = 'pending' "
                f"AND id IN ({marks})",
                (job_id, *plan["delete_ids"]),
            )
        await db.commit()
    await _audit_log(
        db, job_id, "findings_doc_level_collapsed",
        f"merged={len(plan['delete_ids'])} "
        f"groups={json.dumps(plan['groups'], ensure_ascii=False)} — 文档级事实"
        f"（模板级）原逐页重复，已收敛为 1 条并标注页清单",
    )
    logger.info(
        f"[{job_id}] doc-level facts collapsed: "
        f"{sum(g['count'] for g in plan['groups'].values())} rows → "
        f"{len(plan['groups'])} row(s)"
    )
    return plan


async def _run_stage3_cross_analysis(
    db, job_id: str, stage1_ms: int, stage2_ms: int,
    failed_pages: list[int], pipeline_start: float,
    dual_diff: list[dict] | None = None,
) -> None:
    """Cross-page analysis + findings persistence + final status. (refactor)

    dual_diff: 门禁 3 双后端对比差异列表（[{page, reason}, ...]，整份路径
    传入）。非空时逐页写入 completeness findings 并强制 partial_review —
    双后端结果不一致本身就是"需人工对照原图"的证据，不得自动通过。
    """
    # Runtime resolution — tests rebuild core.pipeline.db_lock.
    from core.pipeline import db_lock
    # Runtime resolution — tests patch core.pipeline.{_is_cancelled,
    # analyze_cross_page}.
    from core.pipeline import (
        _is_cancelled as _run_is_cancelled,
        analyze_cross_page as _run_analyze_cross,
    )
    # Check cancellation
    if await _run_is_cancelled(job_id):
        return None

    # ── Stage 3: Cross-page analysis ────────────────────────
    logger.info(f"[{job_id}] Stage 3: Cross-page analysis...")
    stage3_start = time.time()

    cursor = await db.execute(
        "SELECT page, structured_json, raw_html FROM page_cache "
        "WHERE job_id = ? ORDER BY page",
        (job_id,),
    )
    page_structures = []
    # B1-6：收权复核需要页级结构化数据 + OCR 原文（按页索引）
    _struct_by_page: dict = {}
    _raw_by_page: dict = {}
    empty_pages_count = 0
    # M2/T2.4：写入期置信度需要"该页是否带完整性告警"——与 api/review.py
    # 读取期判据共用 core.finding_quality.page_is_flagged（单一来源）。
    from core.finding_quality import page_is_flagged
    flagged_pages: set[int] = set()
    for row in await cursor.fetchall():
        if row["structured_json"]:
            try:
                data = json.loads(row["structured_json"])
                _struct_by_page[row["page"]] = data
                _raw_by_page[row["page"]] = row["raw_html"] or ""
                if page_is_flagged(data):
                    flagged_pages.add(row["page"])
                # Skip pages with parse errors
                if not data.get("_parse_error") and not data.get("_ocr_empty"):
                    # R119：`raw_html` 也一起带下去 —— 规则层原先只拿结构化
                    # `signatures[]`，而"签名栏只填日期/只填姓名"这类判据必须看
                    # **原文栏名**（`core/signature_pairing.py`）。这一列 SQL 早就
                    # 查出来了，只是没往下传。
                    page_structures.append({
                        "page": row["page"],
                        "data": data,
                        "raw_html": row["raw_html"] or "",
                    })
                else:
                    empty_pages_count += 1
            except json.JSONDecodeError:
                logger.warning(f"[{job_id}] Stage 3: Failed to parse page_cache page={row['page']}")
                empty_pages_count += 1

    # P-C3 修复：analyze_cross_page 调用前检查取消状态，
    # 避免取消后仍进入跨页分析（cancelling → review 非法转换）
    if await _run_is_cancelled(job_id):
        return

    # Stage 3 子进度上报（SSE"跨页分析"文案）— 规则校验/LLM 兜底/
    # LLM 语义三里程碑写入 ocr_progress.cross，结束后清除。
    from core.pipeline.state import _update_cross_progress

    async def _cross_progress_cb(done: int, total: int, label: str):
        await _update_cross_progress(job_id, done, total, label)

    findings = await _run_analyze_cross(
        page_structures, job_id=job_id, progress_cb=_cross_progress_cb
    )
    # B1-6 收权（Round 43）：跨页 LLM findings（llm_cross / llm_fallback）与
    # 页级同一把尺子 —— 落库前过确定性复核。两条路径同源同病（同一 LLM、
    # 同一 prompt 家族）：51 页实测里 llm_cross 贡献了 24 条假 critical，
    # 与 llm_page 的 25 条机理完全一致（方向反 / 串位 / 凭空数字）。
    # ⚠️ B3-4：与 stage2 共用**同一个**实现点（重建 + 降级物化只在
    # apply_review 里）。此前两处各自手抄，且已与之漂移。
    #
    # R100（CR-13）：**规格可判性复核此前只接了 stage2** ——
    # `drop_unfounded_spec_findings` 只在 llm_page 路径调用，于是 llm_cross
    # 完全绕开它：51 页实测 p14「37.6°C 超出 40±3°C」（37.6 就在 [37,43] 内）
    # 与 p24「1424」两条假超差一路落库，而规则层对同一三元组已判"无法核定"。
    # 这里按页补上同一条复核，抑制明细并入同一张台账。
    from core.rules.spec_guard import drop_unfounded_spec_findings
    _spec_sup: list[dict] = []
    if _struct_by_page:
        _by_page: dict = {}
        for _f in findings:
            if isinstance(_f, dict):
                _by_page.setdefault(_f.get("page") or 0, []).append(_f)
        _dropped: set[int] = set()
        for _pg, _items in _by_page.items():
            _kept_pg, _sup_pg = drop_unfounded_spec_findings(
                _items, _struct_by_page.get(_pg) or {})
            for _s in _sup_pg:
                _dropped.add(id(_s["finding"]))
            _spec_sup.extend(_sup_pg)
        if _dropped:
            # 按原序过滤（不按页重组 —— 顺序是下游聚合/展示的输入）
            findings = [f for f in findings if id(f) not in _dropped]
            logger.info(
                f"[{job_id}] Stage 3: 规格可判性复核抑制 {len(_spec_sup)} 条"
                f"跨页超差结论（规则层对同一三元组已判无法核定/合规）"
            )
    from core.rules.llm_finding_guard import apply_review
    findings, _g_sup = apply_review(
        findings, structured_by_page=_struct_by_page, raw_by_page=_raw_by_page,
    )
    if _spec_sup:
        _g_sup = list(_spec_sup) + list(_g_sup or [])
    if _g_sup:
        logger.info(
            f"[{job_id}] Stage 3: 收权复核抑制 {len(_g_sup)} 条 LLM 跨页结论"
            f"（判定不成立；明细写 finding_suppressions 台账）"
        )
    # R1 降噪：自指元噪声（"本页含手写内容"/"整体识别置信度较低"）是**工具可读性**
    # 提示而非记录缺陷，整份文档聚合为一条。先于 completeness 降噪执行 —— 两者
    # 治理对象不重叠（前者按文档属性聚合，后者按结构缺失 kind 细分）。
    from core.finding_noise import (
        reduce_completeness_noise,
        reduce_document_level_facts,
        reduce_self_referential_noise,
    )
    findings, _self_ref_report = reduce_self_referential_noise(findings)
    # CR-10 / CR-11：文档级事实（"页眉无生产日期字段"、"日期年份读数不可信"）
    # 收敛为一条陈述。**必须早于** completeness 降噪 —— 否则那两条会先被
    # 通用结构性缺失桶吞掉，文案就退回"某类缺失共 N 处"（丢失"模板级"语义）。
    findings, _doc_fact_report = reduce_document_level_facts(findings)
    # M2 降噪：completeness 结构性缺失的"抽取不确定降级 + 文档级聚合"。
    # 在 gmp_basis/kb_refs 富集**之前**执行 —— 只对精简后的集合做检索/映射，
    # 省掉数百条同质条目的无谓开销。审计可追溯（report 落 audit）。
    findings, _noise_report = reduce_completeness_noise(
        findings,
        flagged_pages=flagged_pages,
        total_pages=len(page_structures),
    )
    if _self_ref_report["aggregated_total"]:
        logger.info(
            f"[{job_id}] self-referential noise reduced: "
            f"aggregated={_self_ref_report['aggregated_total']} "
            f"({list(_self_ref_report['aggregated'])})"
        )
    if _noise_report["aggregated_total"] or \
            _noise_report["downgraded_extraction_uncertain"]:
        logger.info(
            f"[{job_id}] noise reduction: downgraded="
            f"{_noise_report['downgraded_extraction_uncertain']}, "
            f"aggregated={_noise_report['aggregated_total']} "
            f"({list(_noise_report['aggregated'])})"
        )
    if _doc_fact_report["aggregated_total"]:
        logger.info(
            f"[{job_id}] document-level facts collapsed: "
            f"{_doc_fact_report['aggregated_total']} "
            f"({list(_doc_fact_report['aggregated'])})"
        )
    # GMP 依据引用（v7）：按 type 映射法规依据（幂等；无映射不设键，
    # ocr_noise/user_rule 不映射 — user_rule 依据是其自身规则文本）
    from core.rules.gmp_basis import attach_gmp_basis
    attach_gmp_basis(findings)
    # 知识库条文引用（v8）：GMP2010 后置富集 —— 幂等、纯内存 BM25 检索，
    # 零 token 成本；命中则随行落库 findings.kb_refs（JSON）。
    from core.kb.retriever import attach_kb_refs
    import json as _json

    def _refs_json(f: dict):
        refs = f.get("kb_refs")
        if isinstance(refs, list) and refs:
            return _json.dumps(refs, ensure_ascii=False)
        return None

    attach_kb_refs(findings)
    await _update_cross_progress(job_id, 0, 0, "")
    # P-C3 修复：analyze_cross_page 调用后再检查一次取消状态，
    # 避免在跨页分析期间用户点取消后继续写入 findings / 转 review
    if await _run_is_cancelled(job_id):
        return
    stage3_ms = int((time.time() - stage3_start) * 1000)
    logger.info(
        f"[{job_id}] Stage 3: Cross-page analysis done in {stage3_ms}ms "
        f"({len(findings)} findings from {len(page_structures)} pages)"
    )

    # 保存 findings — 按 severity 统计，便于审计和调试
    # 流式输出优化：source='llm_page' 的 findings 已在 Stage 2 每页完成时
    # 写入 DB，此处跳过避免重复。只写入 rule/llm_cross/llm_fallback findings。
    #
    # 对抗审查(cr-1): retry 场景 — B4 指纹查重仅对确定性生成的 rule/
    # llm_fallback 有效；llm_cross/user_rule 的 description 由 LLM 自然
    # 语言生成（temperature=0.1），同一异常两次运行措辞几乎必然不同，
    # 无法指纹判重 → 每次 partial_review/error 重试都会重复插入。方案：
    # Stage 3 重算前删除本 job 待审（pending）的 LLM 生成型 findings
    # （含 user_rule），已人工裁决（confirmed/rejected/corrected）的保留。
    #
    # ⚠️ 同仓**另一处口径**：`api/jobs/actions.py::retry_job` 在 `review → retry`
    # （全量重新分析）时**全量**删除，且删前先归档到 `findings_archive`（TODO 0-44）。
    # 两者**故意不同**：这里只重算 LLM 层（rule 行是确定性生成，靠 UNIQUE 去重即可），
    # 那边是整份分析作废重来。合并会顺手改掉本处口径 ⇒ **改任一处都要先看另一处**。
    await db.execute(
        "DELETE FROM findings WHERE job_id = ? "
        "AND source IN ('llm_cross', 'llm_fallback', 'user_rule') AND status = 'pending'",
        (job_id,),
    )
    await db.commit()
    severity_counts = {"critical": 0, "warning": 0, "info": 0}
    skipped_llm_page = 0
    inserted = 0
    # 批量写入：rule/llm_cross 是确定性生成，内存指纹去重 + INSERT OR IGNORE
    # 双保险（idx_findings_dedup UNIQUE 索引兜底，v5）。避免逐条 select-then-
    # insert 的 2×N 次 DB 往返（51 页真实文件 400+ findings → 1000+ await）。
    dedup_seen: set[tuple] = set()
    # 降噪 N1（本轮）：规则层已覆盖的同 (page, type) 不再重复接受
    # llm_cross/llm_fallback 的语义重复报告 —— 同一问题在复核 UI 出现
    # rule+LLM 两三份是用户可见噪声的主要来源之一。rule 为权威版本；
    # 被抑制数量写入日志与审计（GMP 可追溯"为什么少了一条"）。
    # M2/T2.6：类型白名单 —— 归一后的 type 才是落库/去重/统计口径；原始 type
    # 仅在发生归一时留痕到 raw_type（GMP 可追溯 LLM 实际输出）。
    from core.finding_quality import (
        confidence_for, has_business_text, normalize_finding, normalize_finding_type,
    )

    def _norm(f: dict) -> str:
        return normalize_finding_type(f.get("type"))

    rule_covered: set[tuple] = {
        (f["page"], _norm(f)) for f in findings
        if f.get("source") == "rule"
    }
    # R95/A9：页级 LLM（`llm_page`，Stage 2 已落库）已报过的 (page, type)。
    # 跨页 LLM 再把同一件事说一遍就是纯重复 —— 实测（试用批记录 p3）同一处
    # "签名姓名不一致" 被 llm_page 与 llm_cross 各报一次，复核页看到两张
    # 几乎一样的卡片。页级视图更贴近原页证据，保留它、抑制跨页那条。
    cursor = await db.execute(
        "SELECT page, type, description, ocr_text FROM findings "
        "WHERE job_id = ? AND source = 'llm_page'",
        (job_id,),
    )
    _llm_page_rows = await cursor.fetchall()
    llm_page_covered: set[tuple] = {
        (row["page"], row["type"]) for row in _llm_page_rows
    }
    llm_page_covered = {
        (pg, _TYPE_FAMILY.get(t, t)) for pg, t in llm_page_covered
    }
    # R132 / 0-75：页级 LLM 在**同一日期字面量**上已有的日期类主张。
    # 例：p8 llm_page 已把 2015.6.03 报为 signature_time_anomaly ⇒ 跨页 LLM
    # 再把它报成 suspicious_date 属同源重复（那个 2015 本身就是 OCR 误读）。
    llm_page_literals: dict[tuple[int, str], str] = {}
    for row in _llm_page_rows:
        if str(row["type"] or "") not in _DATE_DEDUP_TYPES:
            continue
        for lit in _date_claim_literals({
            "description": row["description"], "ocr_text": row["ocr_text"],
        }):
            llm_page_literals.setdefault((row["page"], lit), "llm_page")
    # R104/P1-4：**规则层已判定"该页签名辨认不可靠"的页**（`signature_unreadable`）。
    #
    # CR-4（R98）的口径是：同一人的工序对**全部**至少有一侧 `confidence: low`
    # 时，「真的同一人复核」与「OCR 把两个人读成同一个名字」**无法区分** ⇒
    # 规则层只发 info 并请复核员看原页笔迹，**不把"同一人"当结论**。
    #
    # 但跨页 LLM 没有这个约束：实测 `8df2f568` p4 —— 规则层 #1654 说
    # "签名辨认不可靠，无法排除同一人复核，请核对原页笔迹"，
    # 同页 llm_cross #1655 却硬断言"操作人'王小雨'与复核人'王小雨'为同一人，
    # 违反职责分离"。**同一条不可靠的 OCR 读数，一边说不可判、一边当铁证**，
    # 复核员看到的是系统自相矛盾。
    #
    # 判据是**页级**的：规则层判"整页没有一对可靠签名"时，该页上任何
    # "同一人"断言都建立在同一份不可靠读数上 ⇒ 抑制 LLM 那条（保留规则层的
    # info —— 它才是诚实的那条）。
    # 为什么不用"姓名相同"再筛一遍：规则层若真有一对可靠签名，它自己就会发
    # `self_review`，那时 `rule_covered` 的精确类型去重已经拦住了，轮不到这里。
    rule_unreadable_pages: set[int] = _unreadable_signature_pages(findings)
    suppressed_overlap = 0
    #: 其中"前提不可判"那一类（P1-4）—— 与"同页同类型重复"是不同理由，
    #: 审计里分开计数，否则回看时会把两种抑制混成一句"已被规则层覆盖"。
    unfounded_self_review = 0
    #: 同一日期字面量上的重复（0-75）—— 与上面两类理由都不同，单独计数。
    suppressed_same_literal = 0
    cross_date_literals: dict[tuple[int, str], str] = {}
    suppressed_detail: list[dict] = []
    skipped_empty_desc = 0
    batch_rows: list[tuple] = []
    for f in findings:
        # 跳过已在 Stage 2 写入的 page-level LLM findings
        if f.get("source") == "llm_page":
            skipped_llm_page += 1
            continue
        # CR-1 ③（R97）：业务正文为空 ⇒ **不写行**。空文案的 finding 没有可复核
        # 内容，落库只会让复核员看到一张空卡片；R97 之前它还会被护栏理由填满，
        # 变成一句后端日志（实测 p22 `1563`：整条只剩「LLM 独断 critical…」）。
        # 计数进日志（非静默丢弃）。
        if not has_business_text(f):
            skipped_empty_desc += 1
            continue
        src = f.get("source", "rule")
        # R95：写入口径先按文案纠正过宽类型（rule_covered 仍用纯归一口径，
        # 否则规则层的覆盖判定会被 LLM 文案影响）
        ftype, ftype_raw = normalize_finding(f)
        # R104/P1-4：前提不可判 —— 见上面 `rule_unreadable_pages` 的注释。
        if _is_unfounded_self_review(
            f["page"], ftype, src, rule_unreadable_pages,
        ):
            suppressed_overlap += 1
            unfounded_self_review += 1
            suppressed_detail.append({
                "page": f["page"], "type": ftype, "source": src,
                "description": (f.get("description") or "")[:120],
                "covered_by": "rule_unreadable",
            })
            logger.debug(
                f"[{job_id}] unfounded self_review suppressed: p{f['page']} "
                f"({src}) — 规则层已判该页签名辨认不可靠"
            )
            continue
        _covered_by = _overlap_covered_by(
            f["page"], ftype, src, rule_covered, llm_page_covered,
        )
        if _covered_by:
            suppressed_overlap += 1
            # M2/T2.8：抑制可解释 —— 记录"哪条被谁覆盖"的明细（审计可回看）
            suppressed_detail.append({
                "page": f["page"], "type": ftype, "source": src,
                "description": (f.get("description") or "")[:120],
                "covered_by": _covered_by,
            })
            logger.debug(
                f"[{job_id}] overlap suppressed: p{f['page']} {ftype} "
                f"({src}) — {_covered_by} already covers this page/type"
            )
            continue
        # R132 / 0-75：同一页 + 同一日期字面量的日期类主张只留一条。
        # 先看页级 LLM（Stage 2 已落库），再看本轮的跨页条目。
        _lit_by = _same_literal_covered_by(f, ftype, src, llm_page_literals)
        if not _lit_by:
            _lit_by = _same_literal_covered_by(f, ftype, src, cross_date_literals)
        if _lit_by:
            suppressed_same_literal += 1
            suppressed_detail.append({
                "page": f["page"], "type": ftype, "source": src,
                "description": (f.get("description") or "")[:120],
                "covered_by": _lit_by,
            })
            logger.debug(
                f"[{job_id}] same-literals suppressed: p{f['page']} {ftype} "
                f"({src}) — {_lit_by} already cites the same date literal"
            )
            continue
        if src in _DEDUP_SOURCES and ftype in _DATE_DEDUP_TYPES:
            for _lit in _date_claim_literals(f):
                cross_date_literals.setdefault((f["page"], _lit), "llm_cross")
        # robustness-B4: retry 会重新执行 Stage 3，确定性生成的 findings
        # 按 (job_id, source, page, type, description) 指纹去重。
        fingerprint = (
            job_id, f.get("source", "rule"), f["page"], ftype, f["description"],
        )
        if fingerprint in dedup_seen:
            logger.debug(
                f"[{job_id}] findings dup skipped (page={f['page']} "
                f"source={f.get('source')} type={f['type']})"
            )
            continue
        dedup_seen.add(fingerprint)
        # A3 修复（Round 3）：severity 计数改为在去重/跳过之后统计
        # 实际写入行 — 原实现先计数后跳过，llm_page（Stage 2 已入库）
        # 和重复行被重复计入，日志与真实数据不符（GMP 审计误导）。
        sev = f.get("severity", "info")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
        batch_rows.append((
            job_id, f["page"], ftype, f["severity"], f["description"],
            f.get("ocr_text"), f.get("operator"), f.get("source", "rule"),
            f.get("rule_id") if f.get("source") == "user_rule" else None,
            f.get("gmp_basis"), _refs_json(f),
            confidence_for(f, f["page"] in flagged_pages),
            ftype_raw, f.get("guard_reason"),
        ))
        inserted += 1
    if batch_rows:
        # 对抗审查 T3.2：批量写 findings 与其他写事务（transition/audit/
        # 进度更新）串行化 —— 多 job 并行进入 Stage 3 时防止 executemany+
        # commit 与其他写穿插交错事务边界。
        async with db_lock:
            await db.executemany(
                "INSERT OR IGNORE INTO findings "
                "(job_id, page, type, severity, description, ocr_text, operator, source, user_rule_id, gmp_basis, kb_refs, confidence, raw_type, guard_reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))",
                batch_rows,
            )
            await db.commit()
    logger.info(
        f"[{job_id}] DB: findings inserted ({inserted} new + {skipped_llm_page} "
        f"llm_page skipped + {suppressed_overlap} LLM-overlap suppressed "
        f"(unfounded_self_review={unfounded_self_review}), "
        f"{suppressed_same_literal} same-literal suppressed, "
        f"{skipped_empty_desc} empty-description dropped, "
        f"severity={severity_counts})"
    )
    # TODO 0-64：页级 LLM 已落库的「文档级事实」逐页重复 ⇒ 收敛为 1 条。
    # 必须在这里（写入之后）做：p5/p10 那两条都是 `llm_page`，Stage 2 就进库了，
    # 上面那条 `reduce_document_level_facts` 的内存链路根本看不见它们。
    await _collapse_persisted_document_facts(db, job_id)
    if _g_sup:
        # B1-6 收权留痕：抑制 ≠ 删除。复用 finding_suppressions 台账
        # （reason + evidence + 可回退），与 stage2 的 spec_guard 同一张表、
        # 同一构造点 —— 复核页可查、可一键回退、可抽检（GMP 可追溯）。
        from core.rules.spec_guard import (
            SUPPRESSION_INSERT_SQL,
            suppression_rows,
        )
        _sup_by_page: dict = {}
        for _s in _g_sup:
            _sup_by_page.setdefault(_s["finding"].get("page") or 0, []).append(_s)
        _ledger_rows: list[tuple] = []
        for _pg, _items in _sup_by_page.items():
            _ledger_rows.extend(suppression_rows(job_id, _pg, _items))
        async with db_lock:
            await db.executemany(SUPPRESSION_INSERT_SQL, _ledger_rows)
            await db.commit()
        await _audit_log(
            db, job_id, "llm_guard_suppressed",
            f"count={len(_ledger_rows)} — B1-6 收权：跨页 LLM 结论被规则层"
            f"确定性复核否定（判定不成立），明细入 finding_suppressions 台账",
        )
        logger.info(
            f"[{job_id}] DB: {len(_ledger_rows)} 条收权抑制写入台账（可回退）"
        )
    if suppressed_overlap or suppressed_same_literal:
        # M2/T2.8 抑制可解释：除计数外落明细（哪页/哪类/被谁覆盖），
        # 让"为什么少了一条"在审计里可逐条回看（GMP 可追溯）。
        await _audit_log(
            db, job_id, "findings_overlap_suppressed",
            f"count={suppressed_overlap + suppressed_same_literal} "
            f"(其中前提不可判 unfounded_self_review={unfounded_self_review}, "
            f"同日期字面量重复 same_literal={suppressed_same_literal}) — "
            f"llm findings whose (page,type) already covered by deterministic "
            f"rule layer, 或该页签名被规则层判为辨认不可靠（covered_by=rule_unreadable）; "
            f"detail={json.dumps(suppressed_detail[:50], ensure_ascii=False)}",
        )

    # 门禁 3：双后端差异页逐页写入 completeness finding（复用 rule 链路的
    # 去重索引 / 复核 UI / 报告导出）。INSERT OR IGNORE + UNIQUE 指纹保证
    # retry 幂等。
    dual_diff = dual_diff or []
    if dual_diff:
        from core.rules.gmp_basis import GMP_BASIS_MAP

        dual_dicts = [
            {
                "page": d["page"], "type": "completeness", "severity": "warning",
                "description": (
                    f"第{d['page']}页 双后端 OCR 结果存在显著差异"
                    f"（{d['reason']}），请对照 PDF 原图人工核对"
                ),
                "ocr_text": f"dual_compare: primary vs secondary — {d['reason']}",
                "operator": "", "source": "rule",
                "gmp_basis": GMP_BASIS_MAP.get("completeness"),
            }
            for d in dual_diff
        ]
        attach_kb_refs(dual_dicts)
        dual_rows = [
            (
                job_id, x["page"], x["type"], x["severity"], x["description"],
                x["ocr_text"], x["operator"], x["source"], None,
                x.get("gmp_basis"), _refs_json(x),
                confidence_for(x, x["page"] in flagged_pages), None,
            )
            for x in dual_dicts
        ]
        async with db_lock:
            await db.executemany(
                "INSERT OR IGNORE INTO findings "
                "(job_id, page, type, severity, description, ocr_text, operator, source, user_rule_id, gmp_basis, kb_refs, confidence, raw_type, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))",
                dual_rows,
            )
            await db.commit()
        logger.info(
            f"[{job_id}] DB: dual-compare findings inserted ({len(dual_rows)} rows)"
        )

    # Determine final status
    total_cost_ms = int((time.time() - pipeline_start) * 1000)
    dual_diff = dual_diff or []
    # 门禁 3：双后端差异页 / 解析错误页强制人工复核
    # 空页（_ocr_empty）不强制 partial_review — 空页不是失败，只是无内容
    # （如封面/目录页），review UI 会显示 _ocr_empty 横幅供人工确认。
    final_status = (
        "partial_review" if (failed_pages or dual_diff) else "review"
    )

    # ── #127：零页产出 ≠ 「部分可复核」 ────────────────────────────────
    # partial_review 的语义是"有部分内容可供复核"。当**没有任何一页**完成
    # 分析时（典型：模型 API Key 失效 → 每页 Stage 2 都以 401 失败），
    # 报 partial_review 会让"0 条 finding"与"记录确实无异常"不可区分 ——
    # GMP 场景下这是**假阴性**。此时正确的终态是 error：这次分析没有产出。
    #
    # 判据**从数据派生**（复用 _get_analyzed_pages 的"排除 _parse_error"
    # 口径），因此不依赖具体故障原因：上游 OCR 整体不可用等同理归类。
    fallback_error_message = None
    if failed_pages:
        from core.pipeline.stage2 import _get_analyzed_pages
        if not await _get_analyzed_pages(db, job_id):
            final_status = "error"
            cursor = await db.execute(
                "SELECT error_message FROM jobs WHERE id = ?", (job_id,)
            )
            row = await cursor.fetchone()
            if not (row and row["error_message"]):
                # 页级只留了 _parse_error（原因分散各页），这里补一条 job 级
                # 说明。Stage 2 的配置级路径已写过更精确的文案 → 不覆盖。
                fallback_error_message = (
                    f"全部分析页失败（0 页产出可用结果，共 {len(failed_pages)} 页）"
                    "｜已产出内容不足以复核，请检查模型凭据或重试"
                )

    update_sql = (
        "UPDATE jobs SET finished_at = datetime('now','localtime'), "
        "stage1_ms = ?, stage2_ms = ?, stage3_ms = ?, failed_pages = ? "
    )
    update_params: list = [stage1_ms, stage2_ms, stage3_ms,
                           json.dumps(failed_pages) if failed_pages else None]
    if fallback_error_message:
        update_sql += ", error_message = ? "
        update_params.append(fallback_error_message)
    update_sql += "WHERE id = ?"
    update_params.append(job_id)
    await db.execute(update_sql, tuple(update_params))
    status_detail = (
        f"流水线完成：{len(findings)} 条问题，{len(failed_pages)} 页失败"
        + (f"，双后端差异 {len(dual_diff)} 页" if dual_diff else "")
        + (f"，{empty_pages_count} 页内容为空/解析错误" if empty_pages_count else "")
        + ("（0 页产出可用结果 → error）" if final_status == "error" else "")
    )
    await transition_status(db, job_id, final_status, status_detail)

    # 飞书通知（旁路：失败不影响主流程；成功/部分完成均推送）
    try:
        from core.notify import notify_job
        await notify_job(job_id, final_status)
    except Exception:
        pass  # notify_job 自身已兜底，此处双保险防异常逃逸

    logger.info(f"[{job_id}] Pipeline complete: status={final_status}, "
                 f"{len(findings)} findings, {len(failed_pages)} failed pages, "
                 f"{len(dual_diff)} dual-compare diffs, "
                 f"total={total_cost_ms}ms (OCR={stage1_ms} LLM={stage2_ms} Cross={stage3_ms})")
    await _audit_log(db, job_id, "pipeline_complete",
                     f"status={final_status} findings={len(findings)} "
                     f"failed={len(failed_pages)} total={total_cost_ms}ms")
