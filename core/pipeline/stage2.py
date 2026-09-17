"""Stage 2 — concurrent per-page LLM analysis (module refactor)."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from config import config
from core.page_analyzer import AnalysisCancelled
from core.pipeline.state import _audit_log, touch_activity, transition_status
from llm.client import LLMConfigError, _mask_secrets

logger = logging.getLogger(__name__)


def config_error_job_message(reason: str) -> str:
    """配置级故障的 job 级原因文案（**单一来源**：UI / 通知 / 测试共用）。

    #127：这句话是"0 条 finding 到底是记录没问题、还是压根没分析"的
    唯一分界线，所以必须同时满足三件事 —— 说明**性质**（重试无效）、
    给出**首因**（脱敏后）、给出**可执行的动作**。
    """
    return (
        f"LLM 配置级故障（重试无效）：{reason}"
        "｜请检查「设置」中的模型 API Key 是否有效且未过期"
    )


async def _handle_page_failure(
    db, job_id: str, page_num: int, exc: Exception,
    failed_pages: list[int], state_lock: asyncio.Lock,
    config_error: dict | None = None,
) -> None:
    """单页分析失败的统一处置（Stage 2 各错误路径的**唯一出口**）。

    ``config_error`` 非 None 表示这是**配置级**故障（凭据/权限/请求非法，
    重试无效）。此时除页级留痕外，额外把**首因提升到 job 级**：

    此前 ``jobs.error_message`` 只在硬 error 路径写入，Stage 2 失败一律
    只留页级 ``_parse_error`` → 前端在 partial_review 下无原因可显，最终
    呈现绿点 + 0 条 finding，与"记录确实无异常"不可区分（GMP 假阴性）。

    ``config_error`` 用调用方传入的 dict 既暂存首因、又充当"只写一次"的
    守卫（并发页共用同一实例）。写入复用同一把 ``db_lock``，**不嵌套加锁**
    （asyncio.Lock 不可重入）。
    """
    logger.error(
        f"[{job_id}] Stage 2: Page {page_num} failed: {exc}", exc_info=True
    )
    async with state_lock:
        failed_pages.append(page_num)

    # 判定与暂存在同一"无 await 区间"内完成 → 并发页只会有一个拿到 True
    escalate = False
    if config_error is not None and not config_error:
        config_error["reason"] = _mask_secrets(str(exc))[:200]
        config_error["page"] = page_num
        escalate = True

    error_data = {
        "page_number": page_num,
        "_parse_error": True,
        "_error": str(exc)[:200],
        "overall_confidence": "low",
    }
    # Runtime resolution — tests rebuild core.pipeline.db_lock.
    from core.pipeline import db_lock
    async with db_lock:
        # 对抗审查(cr-2): 异常可能发生在 llm_page findings 循环中
        # （NOT NULL 约束/类型错误），部分 INSERT 未提交。若不回滚，
        # 下方 UPDATE + commit 会把残留的半个事务一并提交，页面被标
        # 失败的同时留下半套 findings（retry 后与重分析结果重复）。
        try:
            await db.rollback()
        except Exception as rb_err:
            logger.warning(
                f"[{job_id}] Stage 2: rollback failed (page={page_num}): {rb_err}"
            )
        await db.execute(
            "UPDATE page_cache SET structured_json = ?, analyzed_at = datetime('now','localtime') "
            "WHERE job_id = ? AND page = ?",
            (json.dumps(error_data, ensure_ascii=False), job_id, page_num),
        )
        if escalate:
            await db.execute(
                "UPDATE jobs SET error_message = ? WHERE id = ?",
                (config_error_job_message(config_error["reason"]), job_id),
            )
        await touch_activity(db, job_id)  # 前进（即使该页最终失败）
        await db.commit()
        logger.warning(
            f"[{job_id}] DB: page_cache updated with _parse_error (page={page_num})"
            + (
                " + job-level config error escalated"
                if escalate else ""
            )
        )


async def _run_stage2_analysis(
    db, job_id: str, pages: list[dict], failed_pages: list[int],
) -> int:
    """Concurrent per-page LLM analysis; returns stage2_ms. (refactor)"""
    # Runtime resolution — tests patch core.pipeline._is_cancelled.
    from core.pipeline import _is_cancelled as _run_is_cancelled
    # Check cancellation
    if await _run_is_cancelled(job_id):
        return 0

    # ── Stage 2: Per-page LLM analysis (concurrent) ─────────
    await transition_status(db, job_id, "analyzing", "开始逐页分析")
    concurrency = config["app"].llm_concurrency
    logger.info(
        f"[{job_id}] Stage 2: Analyzing {len(pages)} pages (concurrency={concurrency})..."
    )

    stage2_start = time.time()

    # Get already-analyzed pages for resume
    analyzed_pages = await _get_analyzed_pages(db, job_id)
    logger.info(
        f"[{job_id}] Stage 2: {len(analyzed_pages)} pages already analyzed, "
        f"resuming from {len(analyzed_pages) + 1}"
    )

    # Build list of pages that still need analysis
    todo: list[tuple[int, dict]] = []
    for i, page in enumerate(pages):
        page_num = i + 1
        if page_num in analyzed_pages:
            continue
        todo.append((page_num, page))

    # Shared state guards — aiosqlite single connection does NOT support
    # concurrent execute, so all DB writes must be serialized via the
    # module-level db_lock (also used by _is_cancelled).
    state_lock = asyncio.Lock()
    completed = {"n": 0}
    total_pages = len(pages)
    sem = asyncio.Semaphore(concurrency)
    # 配置级故障暂存（#127）：并发页共用同一 dict，首因由 _handle_page_failure
    # 写入并同时用作"只写一次"的守卫。
    config_error: dict = {}

    # Run all page analyses concurrently
    tasks = [
        asyncio.create_task(
            _analyze_one(db, job_id, pn, pg, sem, failed_pages,
                         state_lock, completed, total_pages,
                         config_error=config_error)
        )
        for pn, pg in todo
    ]
    # 取消时中止 in-flight 分析任务：与 sliced 路径（1275-1283）对齐 —
    # 否则取消后孤儿协程继续跑 LLM（单页最长 240s），应用退出时抛
    # "Task was destroyed"（对抗审查同款问题，整份路径此前未修）。
    # 结构化并发原则：任务不得游离于父作用域之外无观察者地运行。
    while tasks:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        tasks = list(pending)
        if await _run_is_cancelled(job_id):
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            break
        if config_error:
            # ── #127 早停 ────────────────────────────────────────────
            # 配置级故障下剩余的每一次调用都必然以同样方式失败：
            # 继续跑只是把同一个错误重复 N 遍（并让 sem 上的排队白等）。
            # 取消未完成任务；已 in-flight 的 ≤concurrency 个自然收尾。
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # 未及尝试的页同样计入 failed_pages：它们没有任何产出，
            # 不计数会让复核者以为"页数齐了"（与 Stage 1 缺页同口径）。
            # 成功集取自 _get_analyzed_pages（排除 _parse_error），单一来源。
            succeeded = await _get_analyzed_pages(db, job_id)
            async with state_lock:
                for pn, _pg in todo:
                    if pn not in failed_pages and pn not in succeeded:
                        failed_pages.append(pn)
            logger.warning(
                f"[{job_id}] Stage 2: early-stop on config error "
                f"(first cause at page {config_error.get('page')}): "
                f"{config_error.get('reason')}"
            )
            break

    stage2_ms = int((time.time() - stage2_start) * 1000)
    logger.info(
        f"[{job_id}] Stage 2: Complete in {stage2_ms}ms, "
        f"{len(failed_pages)} pages failed"
    )
    await _audit_log(db, job_id, "stage2_complete",
                     f"duration={stage2_ms}ms failed={failed_pages}"
                     + (f" config_error={config_error.get('reason')}"
                        if config_error else ""))
    return stage2_ms


async def _analyze_one(
    db, job_id: str, page_num: int, page: dict,
    sem: asyncio.Semaphore, failed_pages: list[int],
    state_lock: asyncio.Lock, completed: dict, total_pages: int,
    config_error: dict | None = None,
) -> None:
    """并发 LLM 分析单页（Stage 2 的原子单元，整份/分片路径共用）。

    LLM call 在 semaphore 限制下并发；DB 写入经 db_lock 串行化，避免
    aiosqlite 共享连接上的 "Recursive use of cursors" 错误。

    流式输出：每页分析完成后立即把该页 findings 写入 findings 表
    （source='llm_page'），用户在 Stage 2 进行中就能在 review 页看到
    已分析页的结果，无需等 Stage 3 完成。Stage 3 的
    _collect_per_page_findings 会跳过已写入的 llm_page findings。

    Args:
        completed: 可变容器 {"n": 已完成页数}（跨路径共享统计）
        config_error: 配置级故障暂存 dict（#127）。非 None 时，遇到
            LLMConfigError 会把首因提升到 job 级；不入池的调用方传 None，
            行为与改动前完全一致。
    """
    # Runtime resolution — tests rebuild core.pipeline.db_lock.
    from core.pipeline import db_lock
    # Cancellation check: skip further LLM calls for later pages
    # 取消检查：跳过后续页的 LLM 调用；已在运行的调用会自然结束
    # （HTTP 请求无法中途打断）。
    # Runtime resolution — tests patch core.pipeline.{_is_cancelled,
    # analyze_page}.
    from core.pipeline import (
        _is_cancelled as _run_is_cancelled,
        analyze_page as _run_analyze_page,
    )
    if await _run_is_cancelled(job_id):
        logger.info(f"[{job_id}] Stage 2: Skipped page {page_num} (cancelled)")
        return

    raw_html = page.get("markdown", {}).get("text", "")
    async with sem:
        # #127：配置级故障已在别处确诊 → 不再发起新的 LLM 调用。
        # 不设这道闸门的话，sem 上排队的页会在确诊后陆续"补刀"：同一个
        # 确定性失败被重复 N 遍（浪费并发额度与费用）。与
        # _run_stage2_analysis 的取消早停形成两层防护 —— 那层负责尽快
        # 收尾，这层负责"一个都不许多打"。
        if config_error:
            logger.info(
                f"[{job_id}] Stage 2: Page {page_num} skipped "
                f"(config error already diagnosed)"
            )
            return
        try:
            try:
                page_start = time.time()
                # C1: Stage 2 单页开始日志 — long-running job（如 50 页 ×
                # 40s/页 ≈ 30min）需要能从 pipeline.log 实时定位当前分析页。
                logger.info(
                    f"[{job_id}] Stage 2: Page {page_num}/{total_pages} analyzing "
                    f"(len={len(raw_html)})"
                )
                # P1-2: 取消检查点注入 — analyze_page 在 LLM 调用之间（chat_json
                # 后 / schema 修复重试前）轮询 cancel_check，取消后不再发起新的
                # LLM 调用（此前重试链最长 ~12 分钟，cancel 后 job 迟迟不终态，
                # delete/archive 被拒且文案"数秒"严重不符）。
                structured = await _run_analyze_page(
                    raw_html, page_num=page_num, job_id=job_id,
                    cancel_check=lambda: _run_is_cancelled(job_id),
                )
                page_ms = int((time.time() - page_start) * 1000)
            except AnalysisCancelled:
                # 用户取消：该页不计 failed_pages（取消是用户动作，不是分析缺陷）
                logger.info(f"[{job_id}] Stage 2: Page {page_num} analysis cancelled")
                return

            # 标记 dict（见 page_analyzer.analyze_page），此前此类页既不进
            # failed_pages 也不触发 partial_review，job 显示"成功"但实际
            # 缺页。此处与异常路径一致地归入 failed_pages（不 completed++）。
            if structured.get("_parse_error"):
                async with state_lock:
                    failed_pages.append(page_num)
                error_data = {
                    "page_number": page_num,
                    "_parse_error": True,
                    "_error": str(structured.get("_raw", ""))[:200],
                    "overall_confidence": "low",
                }
                async with db_lock:
                    await db.execute(
                        "UPDATE page_cache SET structured_json = ?, analyzed_at = datetime('now','localtime') "
                        "WHERE job_id = ? AND page = ?",
                        (json.dumps(error_data, ensure_ascii=False), job_id, page_num),
                    )
                    # 心跳绑定"前进"：该页已处理完（哪怕解析失败也算推进），
                    # 看门狗据此知道 analyzing 阶段的 job 仍在动。
                    await touch_activity(db, job_id)
                    await db.commit()
                logger.warning(
                    f"[{job_id}] Stage 2: Page {page_num} JSON parse failure "
                    f"(counted as failed page)"
                )
                return

            payload = json.dumps(structured, ensure_ascii=False)
            confidence = structured.get("overall_confidence", "unknown")
            measurements_count = len(structured.get("measurements", []))
            page_findings = structured.get("findings", []) or []
            logger.info(
                f"[{job_id}] Stage 2: Page {page_num}/{total_pages} LLM done in {page_ms}ms "
                f"(confidence={confidence}, measurements={measurements_count}, "
                f"findings={len(page_findings)}, payload={len(payload)} bytes)"
            )
            async with db_lock:
                await db.execute(
                    "UPDATE page_cache SET structured_json = ?, analyzed_at = datetime('now','localtime') "
                    "WHERE job_id = ? AND page = ?",
                    (payload, job_id, page_num),
                )
                await touch_activity(db, job_id)  # 单页分析完成 = 前进
                # 对抗审查 P2：重分析页先清该页待审（pending）llm_page 旧行 —
                # 自愈/重试后 raw_html 变化 → 新 findings 指纹不同，
                # idx_findings_dedup UNIQUE 挡不住，新旧两套结论并存误导复核。
                # 只删 pending：confirmed/rejected/corrected 是人工裁决记录
                # （GMP 审计证据），必须保留。
                await db.execute(
                    "DELETE FROM findings WHERE job_id = ? AND page = ? "
                    "AND source = 'llm_page' AND status = 'pending'",
                    (job_id, page_num),
                )
                # P0-2：同页的抑制台账也要与本次分析结果对齐 —— 否则重分析后
                # 新一套结论已变，旧台账行仍在，复核页会展示"已不存在的抑制"。
                # 只清未回退行：已回退（reverted_at 非空）的是人工动作的审计
                # 证据，改写它等于篡改审计追踪。
                await db.execute(
                    "DELETE FROM finding_suppressions WHERE job_id = ? AND page = ? "
                    "AND reverted_at IS NULL",
                    (job_id, page_num),
                )
                # 流式输出：立即把该页 LLM 产生的 findings 写入 findings 表。
                # 对抗审查(cr-3): llm_page 路径同样依赖 idx_findings_dedup UNIQUE
                # 索引（v5）做原子去重，防御"部分提交残留 + retry"组合路径下的重复行。
                llm_page_rows = []
                # GMP 依据引用（v7）：按 type 映射法规依据（幂等，无映射不设键）
                from core.rules.gmp_basis import attach_gmp_basis
                dict_findings = [f for f in page_findings if isinstance(f, dict)]
                # M8/P0：LLM 自报的 param_out_of_spec 此前直接落库、不经可判性
                # 校验，OCR 把 "40±3°C" 读成 "40 3°C" 时成片误报（真实 p14 实测
                # 6 条中 5 条误报）。改用规则层同一解析器复核，判为合规则者抑制；
                # 定位不到/不可判者保留（fail-closed）。
                # P0-2：抑制**必须留痕**（抑制 ≠ 删除）——第二返回值是明细列表
                # 而非计数，落 finding_suppressions 台账（带非空 reason + 证据），
                # 复核页可查、可一键回退。只记计数等于"不可查、不可回退、不可抽检"。
                from core.rules.spec_guard import (
                    SUPPRESSION_INSERT_SQL,
                    drop_unfounded_spec_findings,
                    suppression_rows,
                )
                dict_findings, _suppressed = drop_unfounded_spec_findings(
                    dict_findings, structured
                )
                _supp_rows = suppression_rows(job_id, page_num, _suppressed)
                if _supp_rows:
                    logger.info(
                        f"[{job_id}] Stage 2: page {page_num} 抑制 "
                        f"{len(_supp_rows)} 条 LLM 规格误报（规则层复核为合规），"
                        f"已写入抑制台账（可复核页回退）"
                    )
                attach_gmp_basis(dict_findings)
                # 知识库条文引用（v8）：后置富集（幂等，纯内存检索）
                from core.kb.retriever import attach_kb_refs
                import json as _json

                def _refs_json(x: dict):
                    refs = x.get("kb_refs")
                    if isinstance(refs, list) and refs:
                        return _json.dumps(refs, ensure_ascii=False)
                    return None

                attach_kb_refs(dict_findings)
                # M2/T2.4+T2.6：写入期置信度 + 类型白名单归一（含 raw_type 留痕）。
                from core.finding_quality import (
                    confidence_for as _conf, normalize_finding_type as _norm,
                    page_is_flagged as _flagged,
                )
                _flagged_page = _flagged(structured)
                for f in dict_findings:
                    if not isinstance(f, dict):
                        continue
                    if not {"type", "severity", "description"}.issubset(f.keys()):
                        continue
                    _ftype = _norm(f.get("type"))
                    llm_page_rows.append((
                        job_id, page_num, _ftype,
                        f.get("severity", "info"), f.get("description", ""),
                        f.get("ocr_text", ""), f.get("operator", ""),
                        f.get("gmp_basis"), _refs_json(f),
                        _conf({"source": "llm_page"}, _flagged_page),
                        f.get("type") if _ftype != f.get("type") else None,
                    ))
                if llm_page_rows:
                    await db.executemany(
                        "INSERT OR IGNORE INTO findings "
                        "(job_id, page, type, severity, description, ocr_text, operator, source, gmp_basis, kb_refs, confidence, raw_type, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'llm_page', ?, ?, ?, ?, datetime('now','localtime'))",
                        llm_page_rows,
                    )
                # P0-2 抑制台账落库：与 findings 同一事务/同一把 db_lock 内提交
                # —— 抑制记录与正式 finding 必须同进同出，否则重试窗口内会
                # 出现"finding 已回滚但抑制记录已提交"的台账漂移。
                if _supp_rows:
                    await db.executemany(
                        SUPPRESSION_INSERT_SQL, _supp_rows
                    )
                    await db.execute(
                        "INSERT INTO audit_log (job_id, action, detail, created_at) "
                        "VALUES (?, 'spec_guard_dropped', ?, datetime('now','localtime'))",
                        (
                            job_id,
                            f"page={page_num} suppressed={len(_supp_rows)}: "
                            + "; ".join(
                                # 行序：job_id, page, type, severity, description, ...
                                f"{r[2]}: {str(r[4])[:60]}" for r in _supp_rows[:5]
                            ),
                        ),
                    )
                await db.commit()
                logger.info(
                    f"[{job_id}] DB: page_cache updated + {len(llm_page_rows)} "
                    f"page-level findings inserted (page={page_num})"
                )
        except LLMConfigError as e:
            # #127：必须在通用 except Exception **之前**捕获 —— LLMConfigError
            # 继承 RuntimeError，落在后面就会被通用分支吞掉，"整份文档都
            # 分析不了"于是被记成"某几页失败"（job 报 partial_review、无原因、
            # 0 条 finding → 与"记录确实无异常"不可区分）。
            await _handle_page_failure(
                db, job_id, page_num, e, failed_pages, state_lock,
                config_error=config_error,
            )
            return
        except Exception as e:
            await _handle_page_failure(
                db, job_id, page_num, e, failed_pages, state_lock,
            )
            return

    async with state_lock:
        completed["n"] += 1
    logger.info(
        f"[{job_id}] Stage 2: Page {page_num}/{total_pages} analyzed "
        f"({completed['n']} done)"
    )


async def _get_analyzed_pages(db, job_id: str) -> set[int]:
    """Get page numbers that already have *successful* structured_json.

    关键修复：之前用 `structured_json IS NOT NULL` 判定已分析，但 LLM 解析
    失败的页也会写入 structured_json（含 `_parse_error: true` 标记），被
    误认为已分析而跳过 retry。这导致用户点"重试"后失败页不会被重新调用
    LLM，错误状态永久保留。

    修复：排除含 `_parse_error` 标记的页，让 retry 能重新分析它们。
    """
    cursor = await db.execute(
        "SELECT page, structured_json FROM page_cache "
        "WHERE job_id = ? AND structured_json IS NOT NULL",
        (job_id,),
    )
    analyzed = set()
    for row in await cursor.fetchall():
        sj = row["structured_json"] or ""
        # 快速包含 _parse_error 标记的检测（避免完整 JSON parse 开销）
        if '"_parse_error"' in sj and "true" in sj:
            # 精确校验：json_extract 在 SQLite 3.38+ 可用，回退到 Python parse
            try:
                import json as _json
                data = _json.loads(sj)
                if data.get("_parse_error"):
                    continue  # 跳过解析失败的页，retry 时重新分析
            except Exception:
                pass
        analyzed.add(row["page"])
    return analyzed
