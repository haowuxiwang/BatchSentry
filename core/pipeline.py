"""Pipeline orchestration — stages 1→2→3 for a single job.

Stage 1: OCR (PaddleOCR-VL 或 MinerU，由 OCR_BACKEND 配置) → save page_cache.raw_html
Stage 2: LLM page-by-page analysis → save page_cache.structured_json
Stage 3: Cross-page analysis → generate findings

Fault tolerance: single page failure does not kill the pipeline.
Resume: skips pages that already have structured_json in page_cache.
Cancel: checks job status before each page, exits if status=cancelling.
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path

from config import config
from db.client import get_db
from core.page_analyzer import analyze_page, AnalysisCancelled
from core.cross_page_analyzer import analyze_cross_page
from core.security import redact_urls
from core.zh_map import zh_job_status

logger = logging.getLogger(__name__)

# Phase 7: per-job async lock — prevents cancel+retry race where two
# pipeline coroutines could run simultaneously on the same job_id.
# Keyed by job_id; entries are removed when pipeline exits.
_pipeline_locks: dict[str, asyncio.Lock] = {}

# 活跃 pipeline task 注册表 — 用于优雅关闭时取消所有运行中的任务
# key=job_id, value=asyncio.Task。task 完成后自动从注册表移除。
_pipeline_tasks: dict[str, asyncio.Task] = {}
_locks_guard = asyncio.Lock()

# Module-level lock serializing all DB writes on the shared aiosqlite
# connection (single connection does NOT support concurrent execute).
# Used by _is_cancelled and _analyze_one to prevent "Recursive use of
# cursors" errors.
db_lock = asyncio.Lock()

# 分片 OCR 队列轮询间隔（run_ocr_sliced 线程回调 → asyncio.Queue 的
# 等待超时）。生产 2s 足够灵敏；测试可 monkeypatch 加速。
_SLICE_QUEUE_TIMEOUT = 2.0


def _get_ocr_backend():
    """根据配置返回 OCR 后端的 run_ocr 函数。

    OCR_BACKEND=paddle (默认): 使用 PaddleOCR-VL
    OCR_BACKEND=mineru:        使用 MinerU 精准解析
    """
    backend = config["app"].ocr_backend.lower()
    if backend == "mineru":
        from core.mineru_client import run_ocr as mineru_run
        logger.info("[Pipeline] OCR 后端: MinerU")
        return mineru_run
    # 默认 PaddleOCR
    from core.ocr_client import run_ocr as paddle_run
    logger.info("[Pipeline] OCR 后端: PaddleOCR-VL")
    return paddle_run


def _get_ocr_chain() -> list[tuple[callable, str]]:
    """返回 OCR 主备链：[(run_ocr, name), ...]，首个为主后端。

    双 OCR 兜底：主后端（OCR_BACKEND 配置）之外的另一个后端若已配置
    token/api_url，则作为 failover 备选。仅当两个后端都可用时链长为 2。
    """
    backend = config["app"].ocr_backend.lower()
    primary = _get_ocr_backend()
    chain = [(primary, backend if backend in ("paddle", "mineru") else "paddle")]
    # 备选：未激活的后端配置完整时才加入 failover 链
    if backend == "mineru":
        paddle_cfg = config["paddle_ocr"]
        if paddle_cfg.api_url and paddle_cfg.token:
            from core.ocr_client import run_ocr as paddle_run
            chain.append((paddle_run, "paddle"))
            logger.info("[Pipeline] OCR failover 备选: PaddleOCR-VL")
    else:
        mineru_cfg = config["mineru"]
        if mineru_cfg.token:
            from core.mineru_client import run_ocr as mineru_run
            chain.append((mineru_run, "mineru"))
            logger.info("[Pipeline] OCR failover 备选: MinerU")
    return chain


def _sanitize_ocr_text(text: str) -> str:
    """清洗 OCR 原始文本（存库前），消除 MinerU/Paddle 产物噪音。

    噪音来源：
    - MinerU 表格 HTML 用字面 "\\n"（反斜杠+n）分隔单元格文本，直出时
      用户看到满屏 "\\n" 而非真实换行；
    - 每个 <td> 都带 style='text-align: center; word-wrap: break-word;'
      行内样式（对 LLM 和 OCR 文本面板都是纯噪音）；
    - img src 是长路径（imgs/img_in_image_box_xxx.jpg），截断为文件名；
    - 伪 LaTeX 残留（$\\text{...}$、{{...}}，公式检测误报），与
      cross_page_analyzer._parse_spec 的剥离规则对齐（F2）；
    - 空单元格（<td> </td>/<td>&nbsp;</td>）与标签间空白（token 浪费）；
    - PDF 控制字符；PaddleOCR-VL 路径无块级页脚过滤（页码整行，
      MinerU 已在后端过滤，此处幂等）。

    清洗后 raw_html 同时服务于 LLM 输入（page_analyzer 仍会二次剥离）
    与 review 页面 OCR 文本面板（htmlToText 展示）。
    """
    if not text:
        return text
    # F2: 伪 LaTeX / OCR 残留符号（$...$、\text/\frac 命令、花括号）——
    # 必须先于下方 \\n/\\t 字面转义，否则 \text 的 \t 会被转成制表符，
    # 子串失配导致命令剥离失效（与 cross_page_analyzer._parse_spec 对齐）。
    s = re.sub(r"\$+", "", text)
    # {2,}：排除 \\n / \\t 单字母字面转义（MinerU 合法分隔符，下方 replace 处理）
    s = re.sub(r"\\[a-zA-Z]{2,}", "", s)
    s = s.replace("{", "").replace("}", "")
    s = s.replace("\\n", "\n").replace("\\t", "\t")
    s = re.sub(r"""\s*style=['"][^'"]*['"]""", "", s)
    s = re.sub(r"""\s*width=['"][^'"]*['"]""", "", s)
    s = re.sub(r"""(src=["'])[^"']*/([^/"']+)(["'>])""", r"\1\2\3", s)
    # F2: 空单元格规整（&nbsp;/空格 → 空），减少 LLM prompt token 浪费
    s = re.sub(r"<td>(?:&nbsp;|\s)*</td>", "<td></td>", s, flags=re.IGNORECASE)
    # F2: HTML 标签间空白压缩（不触碰单元格文本内容）
    s = re.sub(r">\s+<", "><", s)
    # F2: 剥离 PDF 控制字符（保留 \n \t；替换为空格防单词粘连）
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", s)
    s = re.sub(r" {2,}", " ", s)
    # F2: 页码整行过滤（"第 N 页" / "N/M" — 正文表格外的明确页码模式）
    lines = []
    for ln in s.split("\n"):
        t = ln.strip()
        if re.fullmatch(r"第\s*\d+\s*页", t) or re.fullmatch(r"\d+\s*/\s*\d+", t):
            continue
        lines.append(ln)
    s = "\n".join(lines)
    # 折叠 3+ 个连续空行为 2 个（保留段落分隔）
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _pdf_page_count(pdf_path: str) -> int | None:
    """读取 PDF 物理页数（PyMuPDF），失败时返回 None（不阻断 OCR 流程）。

    robustness-A1：用于对比 OCR 结果页数，检测"解析成功但静默缺页"。
    """
    try:
        import fitz  # PyMuPDF — 仅需页数，不渲染

        with fitz.open(pdf_path) as doc:
            return doc.page_count
    except Exception as e:
        logger.warning(f"PDF page count probe failed ({pdf_path}): {e}")
        return None


async def _audit_log(db, job_id: str, action: str, detail: str = ""):
    """Write an entry to the audit_log table.

    P-W1 修复：用 db_lock 序列化写入并即时 commit（审计日志应落盘，
    避免与 _analyze_one 等并发写入触发 aiosqlite 游标错误）。
    """
    try:
        async with db_lock:
            await db.execute(
                "INSERT INTO audit_log (job_id, action, detail) VALUES (?, ?, ?)",
                (job_id, action, detail),
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[{job_id}] Audit log write failed: {e}")


# Valid state transitions — 生产级状态机
# 每个状态只能转换到 allowed 集合中的状态
VALID_TRANSITIONS = {
    "pending":          {"ocr_running", "error", "cancelling", "archived"},
    "ocr_running":      {"ocr_done", "error", "cancelling"},
    "ocr_done":         {"analyzing", "error", "cancelling"},
    "analyzing":        {"review", "partial_review", "error", "cancelling"},
    "cancelling":       {"cancelled", "error"},
    "review":           {"pending", "archived"},  # pending: 重新分析（retry）
    "partial_review":   {"pending", "archived"},  # pending: retry 补分析失败页
    "error":            {"pending", "archived"},
    "cancelled":        {"pending", "archived"},
    "archived":         {"review"},  # unarchive
}


class InvalidTransitionError(Exception):
    """状态转换非法时抛出。"""
    pass


async def _transition_status_unlocked(db, job_id: str, new_status: str, detail: str = "") -> str:
    """transition_status 的内部实现（不加 db_lock）。

    P-C2 修复：调用方已持 db_lock 时调用此函数，避免 asyncio.Lock 不可重入
    导致的死锁。其他场景请使用公开的 transition_status（自动加锁）。

    生产级实现：
    1. 校验当前状态 -> 新状态是否合法
    2. 非法时抛 InvalidTransitionError（阻断操作）
    3. 合法时更新 status + 写 audit_log
    4. 返回新状态
    """
    cursor = await db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if not row:
        raise InvalidTransitionError(f"Job {job_id} not found")

    current = row["status"]
    allowed = VALID_TRANSITIONS.get(current, set())

    if new_status not in allowed:
        # 生产环境：阻断非法转换，而非"仍然更新"
        logger.warning(
            f"[{job_id}] Blocked invalid transition: {current} → {new_status} "
            f"(allowed: {allowed})"
        )
        # 对抗审查 cr-17：被拒转换也写审计 — GMP 追溯要求记录"尝试过但
        # 被拒绝的状态变更"（如对终态 job 重复 cancel/retry），仅日志
        # 不足以防异常路径静态可追踪。
        try:
            await db.execute(
                "INSERT INTO audit_log (job_id, action, detail) "
                "VALUES (?, 'status_transition_blocked', ?)",
                (job_id, f"{current} → {new_status} (allowed: {allowed|set()})"),
            )
            await db.commit()
        except Exception as audit_err:
            logger.warning(f"[{job_id}] Audit log write failed: {audit_err}")
        # 中文化：异常消息直达 HTTP 400 detail + 前端错误提示，英文状态码
        # 对用户无意义（换算 zh_map 中文状态名）
        raise InvalidTransitionError(
            f"不能从「{zh_job_status(current)}」转换到「{zh_job_status(new_status)}」，"
            f"允许的转换：{', '.join(sorted(zh_job_status(s) for s in allowed))}"
        )

    await db.execute("UPDATE jobs SET status = ? WHERE id = ?", (new_status, job_id))
    await db.execute(
        "INSERT INTO audit_log (job_id, action, detail) VALUES (?, ?, ?)",
        (job_id, "status_transition", f"{current} → {new_status}: {detail}" if detail else f"{current} → {new_status}"),
    )
    await db.commit()
    logger.info(f"[{job_id}] Status: {current} → {new_status}" + (f" ({detail})" if detail else ""))
    return new_status


async def transition_status(db, job_id: str, new_status: str, detail: str = "") -> str:
    """强制状态转换 + audit_log 记录（加 db_lock 串行化）。

    P-C2 修复：把 SELECT-check-UPDATE-INSERT-commit 整个序列包在 db_lock 内，
    防止与 _analyze_one 等并发写入触发 aiosqlite 游标错误或 TOCTOU。
    已持 db_lock 的内部调用方应使用 _transition_status_unlocked。

    Args:
        db: 数据库连接
        job_id: Job ID
        new_status: 目标状态
        detail: 转换原因（写入 audit_log）

    Returns:
        新状态

    Raises:
        InvalidTransitionError: 非法转换
    """
    async with db_lock:
        return await _transition_status_unlocked(db, job_id, new_status, detail)


# 非终态：应用退出时这些状态的 job 永远无法继续（pipeline 进程已死）
_STUCK_STATUSES = ("pending", "ocr_running", "ocr_done", "analyzing", "cancelling")


async def recover_stuck_jobs(process_started_at: str | None = None) -> int:
    """重启时将卡死的 job 标记为 error。

    应用崩溃 / 强杀时，处于 pending / ocr_running / ocr_done / analyzing /
    cancelling 的 job 永远不会完成（pipeline 进程已死）。在启动 lifespan 中
    调用此函数，将这些 job 标记为 error，写入 error_message，允许用户重试。

    Args:
        process_started_at: 进程启动时刻（UTC "YYYY-MM-DD HH:MM:SS"）。
            仅恢复 created_at **早于**该时刻的 job — 晚于启动时刻创建的
            job 是当前进程存活期间新上传的任务（pipeline 正在/即将运行），
            不能误判为上次崩溃的遗留。竞态场景：lifespan 中 recover 以
            background task 方式执行，可能与本进程的新上传并发。

    Returns:
        被恢复（标记为 error）的 job 数量
    """
    db = await get_db()
    placeholders = ",".join("?" * len(_STUCK_STATUSES))
    params: tuple = _STUCK_STATUSES
    where_status = f"status IN ({placeholders})"
    if process_started_at:
        where_status += " AND created_at < ?"
        params = _STUCK_STATUSES + (process_started_at,)
    cursor = await db.execute(
        f"SELECT id, status, filename, created_at FROM jobs WHERE {where_status}",
        params,
    )
    stuck = await cursor.fetchall()
    if not stuck:
        logger.info("[Startup] No stuck jobs found (all jobs in terminal state)")
        return 0

    logger.warning(
        f"[Startup] Found {len(stuck)} stuck job(s) to recover: "
        f"{[(r['id'][:8], r['status']) for r in stuck]}"
    )

    for row in stuck:
        job_id = row["id"]
        old_status = row["status"]
        filename = row["filename"] if "filename" in row.keys() else "?"
        created_at = row["created_at"] if "created_at" in row.keys() else "?"
        logger.warning(
            f"[{job_id}] Recovering stuck job: status={old_status} "
            f"filename={filename} created_at={created_at}"
        )
        # 直接 UPDATE 而非 transition_status：状态机不允许 ocr_running→error
        # 之外的路径（如 pending→error 是允许的），但 ocr_done→error 不在
        # VALID_TRANSITIONS 中。这里属于"崩溃恢复"场景，绕过状态机校验，
        # 直接标记 + 审计日志记录。
        # 对抗审查（中文化收尾）：UPDATE 带 status IN (...) 条件 — SELECT
        # 快照与逐行 UPDATE 之间可能被并发路径改写状态（如用户 retry 已恢复
        # 的 job），无条件覆盖会把新状态打回 error。条件更新影响 0 行时跳过
        # 通知，避免对已恢复的 job 误发"应用重启恢复"失败通知。
        placeholders = ",".join("?" * len(_STUCK_STATUSES))
        cursor = await db.execute(
            f"UPDATE jobs SET status = 'error', "
            f"error_message = ?, finished_at = CURRENT_TIMESTAMP "
            f"WHERE id = ? AND status IN ({placeholders})",
            (f"应用重启恢复：原状态 {old_status} 非终态，标记为 error 供重试",
             job_id, *_STUCK_STATUSES),
        )
        if cursor.rowcount == 0:
            logger.info(
                f"[{job_id}] Recovery skipped: status already changed "
                f"(expected {old_status}) — no longer stuck"
            )
            continue
        await _audit_log(
            db, job_id, "stuck_recovery",
            f"recovered on startup: {old_status} → error",
        )
        logger.info(f"[{job_id}] Stuck job recovered: {old_status} → error")
        # P1-8：应用重启恢复也是终态（error）— 与 pipeline 正常 error 路径
        # 一致地发飞书通知。用户不在 GUI 前时（崩溃重启典型场景）也能得知
        # 任务失败可重试。旁路：通知失败绝不影响启动流程。
        try:
            from core.notify import notify_job
            await notify_job(job_id, "error")
        except Exception:
            pass  # notify_job 自身已兜底，此处双保险防异常逃逸

    await db.commit()
    logger.warning(
        f"[Startup] Recovery complete: {len(stuck)} stuck jobs marked as error "
        f"(ids: {[r['id'] for r in stuck]})"
    )
    return len(stuck)


def launch_pipeline(job_id: str, pdf_path: str) -> asyncio.Task:
    """启动 pipeline 后台 task 并注册到 _pipeline_tasks。

    替代 FastAPI 的 background_tasks.add_task，以便：
    1. 优雅关闭时可通过 _pipeline_tasks 取消所有运行中的任务
    2. task 完成后自动从注册表移除（避免内存泄漏）

    用法（api/jobs.py）：
        from core.pipeline import launch_pipeline
        launch_pipeline(job_id, str(pdf_path))
    """
    def _on_done(task: asyncio.Task, jid: str = job_id):
        # Only pop if this task is still the registered one
        # (prevents stale callback from deleting a newer task's entry)
        if _pipeline_tasks.get(jid) is task:
            removed = _pipeline_tasks.pop(jid, None)
        else:
            removed = None
        active_after = len(_pipeline_tasks)
        if task.cancelled():
            logger.info(
                f"[{jid}] Pipeline task cancelled and unregistered "
                f"(was_registered={removed is not None}, active_after={active_after})"
            )
        elif task.exception():
            logger.error(
                f"[{jid}] Pipeline task crashed: {task.exception()!r} "
                f"(was_registered={removed is not None}, active_after={active_after})"
            )
        else:
            logger.info(
                f"[{jid}] Pipeline task completed and unregistered "
                f"(was_registered={removed is not None}, active_after={active_after})"
            )

    task = asyncio.create_task(run_pipeline(job_id, pdf_path))
    _pipeline_tasks[job_id] = task
    task.add_done_callback(_on_done)
    logger.info(
        f"[{job_id}] Pipeline task launched and registered "
        f"(pdf={Path(pdf_path).name}, active={len(_pipeline_tasks)})"
    )
    return task


async def run_pipeline(job_id: str, pdf_path: str):
    """Main async pipeline. Runs in background task.

    Phase 7: acquires a per-job async lock to prevent cancel+retry race.
    If a retry is requested while a previous pipeline is still draining,
    the retry waits for the lock, then sees status=cancelled and exits
    cleanly without spawning duplicate LLM calls.

    资源清理：pipeline 结束后（无论成功/失败/取消）删除上传的 PDF 临时文件。
    OCR 完成后 raw_html 已存入数据库，PDF 文件不再需要。

    progress_futures：OCR 线程经 run_coroutine_threadsafe 调度回主 loop 的
    进度更新 future。测试场景 loop 即刻关闭会导致协程悬挂（RuntimeWarning
    "was never awaited"）；统一在收尾 flush（超时 1s，不阻塞退出）。

    Per-job lock 用引用计数管理注册表条目（对抗审查 T3.1）：
    - 每个进入 run_pipeline 的协程 refs +1，退出（无论是否拿到锁/被取消）-1
    - 仅当 refs 归零才 pop 条目 —— 等待中的协程仍持有 refs，不会发生
      "等待者被取消时误删持有者锁 entry" 的并发窗口（旧实现无脑 pop：
      A 持锁 B 等待时 B 被取消会删掉 A 的锁条目，第三个协程 C 随后
      setdefault 建新锁而与 B 并行执行同一 job）。
    """
    progress_futures: list = []
    # Acquire per-job lock — prevents two pipelines on the same job_id.
    # Registry entry = {lock, refs}; refs counts every coroutine that
    # entered run_pipeline (holding or waiting/cancelled) so the entry is
    # only removed when no one is involved with this job anymore.
    async with _locks_guard:
        entry = _pipeline_locks.setdefault(job_id, {"lock": asyncio.Lock(), "refs": 0})
        refs_before = entry["refs"]
        entry["refs"] += 1
        lock = entry["lock"]
    if refs_before > 0:
        logger.info(f"[{job_id}] Lock contention: another pipeline still draining, waiting...")
    try:
        if lock.locked():
            logger.info(f"[{job_id}] Per-job lock held by another coroutine, waiting to acquire")
        async with lock:
            logger.info(f"[{job_id}] Per-job lock acquired")
            await _run_pipeline_impl(job_id, pdf_path, progress_futures)
    finally:
        # Flush any in-flight progress coroutines before the loop may close
        if progress_futures:
            try:
                real_futures = [
                    f for f in progress_futures if asyncio.isfuture(f)
                ]
                if real_futures:
                    await asyncio.wait_for(
                        asyncio.gather(*real_futures, return_exceptions=True),
                        timeout=1.0,
                    )
            except asyncio.TimeoutError:
                pass
            progress_futures.clear()
        # Cleanup lock entry — only when the last coroutine for this job
        # exits (waiter cancellation must not delete the holder's entry).
        popped = None
        async with _locks_guard:
            cur = _pipeline_locks.get(job_id)
            if cur is not None:
                cur["refs"] -= 1
                if cur["refs"] <= 0:
                    popped = _pipeline_locks.pop(job_id, None)
        removed = popped
        logger.info(
            f"[{job_id}] Per-job lock released "
            f"(registry_removed={removed is not None})"
        )
        # NOTE: PDF 文件不在此处删除 — 复核页 /api/jobs/{id}/pdf 需要它。
        # PDF 在 job 删除 (DELETE /api/jobs/{id}) 或归档时清理。
        # OCR 完成后 raw_html 已存入数据库，但 PDF 仍需保留用于人工复核预览。
        logger.info(f"[{job_id}] Pipeline exited, PDF retained for review: {Path(pdf_path).name}")


async def _run_ocr_with_failover(db, job_id: str, pdf_path: str, progress_cb) -> tuple[list, str, list[str]]:
    """整份 OCR 主备链执行（双 OCR 兜底）。

    返回 (pages, used_backend, failures)：
    - pages: 成功的 OCR 结果，全部失败时 []
    - used_backend: 实际成功执行的后端名（"paddle"/"mineru"）
    - failures: 失败记录列表（每个元素描述一个后端的失败原因）

    失败判定：异常 / 0 页 / 严重页数缺失（缺 >10% 且 >2 页）。
    任一失败 → 切下一个后端整单重试；全部失败时 failures 非空。
    仅整份路径使用；分片路径（MinerU + OCR_SLICES>1）保持原逻辑。
    """
    from logging_config import ocr_job_id_var

    chain = _get_ocr_chain()
    failures: list[str] = []
    for attempt, (run_fn, name) in enumerate(chain):
        if attempt > 0:
            logger.warning(
                f"[{job_id}] OCR failover: {chain[0][1]} failed → trying {name}"
            )
            await _audit_log(
                db, job_id, "ocr_failover",
                f"from={chain[0][1]} to={name} reason={failures[-1] if failures else 'unknown'}",
            )
        _ocr_ctx_token = ocr_job_id_var.set(job_id)
        try:
            try:
                pages = await asyncio.to_thread(run_fn, pdf_path, progress_cb)
            finally:
                ocr_job_id_var.reset(_ocr_ctx_token)
        except Exception as e:
            failures.append(f"{name}: {type(e).__name__}: {redact_urls(str(e))[:300]}")
            logger.error(f"[{job_id}] OCR attempt failed (backend={name}): {failures[-1]}")
            continue
        if not pages:
            failures.append(f"{name}: 0 pages returned")
            logger.error(f"[{job_id}] OCR attempt returned 0 pages (backend={name})")
            continue
        pdf_total = await asyncio.to_thread(_pdf_page_count, pdf_path)
        if pdf_total is not None and len(pages) != pdf_total:
            missing = pdf_total - len(pages)
            # 对抗审查 cr-17：阈值从 max(5, 20%) 收紧到 max(2, 10%) —
            # MinerU 服务端丢页缺陷对中小文件同样发生（丢 2-4 页
            # 时旧阈值不触发 failover，静默输出残缺页）。
            if missing > max(2, int(pdf_total * 0.1)):
                failures.append(f"{name}: page mismatch ({len(pages)}/{pdf_total})")
                logger.error(
                    f"[{job_id}] OCR page count mismatch (backend={name}): {failures[-1]}"
                )
                continue
        return pages, name, failures
    return [], "", failures


async def _run_pipeline_impl(job_id: str, pdf_path: str, progress_futures: list):
    """Pipeline implementation — guarded by per-job lock from run_pipeline."""
    db = await get_db()
    pipeline_start = time.time()

    try:
        # 清除上次运行的 error_message / finished_at（重试场景下避免 review
        # 页面残留旧的"任务处理失败"提示）。retry 端点也会清除，此处为兜底，
        # 覆盖直接调用 launch_pipeline 的路径（如未来新增的 API）。
        await db.execute(
            "UPDATE jobs SET error_message = NULL, finished_at = NULL, "
            "ocr_progress = NULL WHERE id = ?",
            (job_id,),
        )
        await db.commit()

        # ── Stage 1: OCR ────────────────────────────────────────
        await transition_status(db, job_id, "ocr_running", "Stage 1 start")
        ocr_backend = config["app"].ocr_backend
        await _audit_log(db, job_id, "pipeline_start",
                         f"pdf={Path(pdf_path).name} ocr_backend={ocr_backend}")
        logger.info(f"[{job_id}] Stage 1: Starting OCR (backend={ocr_backend})...")

        stage1_start = time.time()

        # Stage 1 流式反馈：OCR 轮询线程（to_thread）中回调进度 →
        # run_coroutine_threadsafe 调度回主事件循环 → 更新 job.ocr_progress
        # → SSE 推送，让用户实时看到 "OCR 12/51" 而不是 0% 白屏。
        loop = asyncio.get_running_loop()
        last_progress: tuple = (None, None)

        def _ocr_progress_cb(done: int, total: int):
            nonlocal last_progress
            if (done, total) == last_progress:
                return
            last_progress = (done, total)
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    _update_ocr_progress(job_id, done, total), loop
                )
                progress_futures.append(fut)
            except Exception as e:
                logger.warning(f"[{job_id}] OCR progress callback failed: {e}")

        # OCR 分片（MinerU, OCR_SLICES>1）：流式输出 — 一片 OCR 完成即
        # 落库并开始该片页面分析，用户无需等全部页 OCR 完成才看到结果。
        # 整份 OCR（默认, 含 Paddle）保持原有阻塞式流程。
        slice_pages = int(getattr(config["app"], "ocr_slices", 1) or 1)
        stage1_ms = 0
        stage2_ms = 0
        failed_pages: list[int] = []
        if ocr_backend == "mineru" and slice_pages > 1:
            logger.info(
                f"[{job_id}] Stage 1 (sliced): OCR_SLICES={slice_pages} pages/slice, "
                f"streaming per-slice analysis enabled"
            )
            stage1_ms, stage2_ms, failed_pages, sliced_total = (
                await _run_sliced_stage1_2(
                    db, job_id, pdf_path, slice_pages, _ocr_progress_cb
                )
            )
            # 0 页兜底：所有片均失败/空
            if sliced_total <= 0:
                err_msg = f"OCR 返回 0 页（backend={ocr_backend}, sliced）— 上游服务未返回任何页面内容"
                logger.error(f"[{job_id}] {err_msg}")
                await db.execute(
                    "UPDATE jobs SET error_message = ?, finished_at = CURRENT_TIMESTAMP, "
                    "stage1_ms = ? WHERE id = ?",
                    (err_msg, stage1_ms, job_id),
                )
                await transition_status(db, job_id, "error", "OCR returned 0 pages")
                await db.commit()
                await _audit_log(db, job_id, "stage1_empty", err_msg)
                return
            # 取消检查（分片函数每片回调后已检查，此处兜底）
            if await _is_cancelled(job_id):
                return
            pages = []  # Stage 3 不依赖 pages（数据已在 page_cache）
        else:
            # F3: retry 智能复用 — job 上次 OCR 已把全部页写入 page_cache 时
            # （典型场景：Stage 2/3 失败后 retry），跳过真实 OCR，直接从缓存
            # 重建 pages 列表进入 Stage 2。省掉整个 PDF 重传重 OCR（上游配额
            # + 数分钟等待）。仅整份路径生效（分片路径按片流的语义，不复用）。
            reuse_pages = None
            cursor = await db.execute(
                "SELECT total_pages FROM jobs WHERE id = ?", (job_id,)
            )
            jrow = await cursor.fetchone()
            target_pages = (jrow["total_pages"] or 0) if jrow else 0
            if target_pages > 0:
                cursor = await db.execute(
                    "SELECT raw_html FROM page_cache WHERE job_id = ? AND raw_html IS NOT NULL "
                    "ORDER BY page",
                    (job_id,),
                )
                cached_rows = await cursor.fetchall()
                if len(cached_rows) >= target_pages:
                    reuse_pages = [
                        {"markdown": {"text": r["raw_html"] or ""}}
                        for r in cached_rows
                    ]
                    await _audit_log(
                        db, job_id, "stage1_skipped",
                        f"reuse {len(reuse_pages)} cached pages (no re-OCR)",
                    )
                    logger.info(
                        f"[{job_id}] Stage 1 skipped: reusing {len(reuse_pages)} cached pages"
                    )
            if reuse_pages is not None:
                # used_backend="cached" 仅用于日志/审计；不覆盖 jobs.ocr_backend_used
                # （保留上次真实后端，见下方 UPDATE 条件）。
                pages, used_backend, ocr_failures = reuse_pages, "cached", []
            else:
                # 双 OCR 兜底：主后端失败（异常/0 页/严重缺页）自动切换备后端
                # 整单重试，job 记录实际使用的后端（jobs.ocr_backend_used）审计。
                pages, used_backend, ocr_failures = await _run_ocr_with_failover(
                    db, job_id, pdf_path, _ocr_progress_cb
                )
            stage1_ms = int((time.time() - stage1_start) * 1000)
            if not pages:
                reason = ocr_failures[0] if ocr_failures else f"backend={ocr_backend}"
                err_parts = [f"OCR 处理失败（{ocr_backend}）— {reason}"]
                if len(ocr_failures) > 1:
                    err_parts.append(
                        "备选失败: " + "; ".join(ocr_failures[1:])
                    )
                err_msg = "; ".join(err_parts)[:2000]
                logger.error(f"[{job_id}] {err_msg}")
                await db.execute(
                    "UPDATE jobs SET error_message = ?, finished_at = CURRENT_TIMESTAMP, "
                    "stage1_ms = ? WHERE id = ?",
                    (err_msg, stage1_ms, job_id),
                )
                await transition_status(db, job_id, "error", "OCR failed (all backends)")
                await db.commit()
                await _audit_log(db, job_id, "stage1_failed", err_msg)
                return

            if used_backend != "cached":
                # F3: 缓存复用路径不覆盖 jobs.ocr_backend_used — 保留上次真实后端
                # 供 GMP 溯源（"cached" 不是 OCR 后端，写进去会污染审计显示）。
                await db.execute(
                    "UPDATE jobs SET ocr_backend_used = ? WHERE id = ?",
                    (used_backend, job_id),
                )
            logger.info(
                f"[{job_id}] Stage 1: OCR complete: {len(pages)} pages "
                f"in {stage1_ms}ms (backend={used_backend})"
            )
            await _audit_log(
                db, job_id, "stage1_complete",
                f"pages={len(pages)} duration={stage1_ms}ms backend={used_backend}",
            )

            # 轻微页数差异（缺 ≤5 页且 ≤20%）→ 不阻断，但必须显式暴露：
            # P1-4 修复 — 缺失页码并入 failed_pages，job 以 partial_review 收尾，
            # 复核页横幅列出缺失页码，规则层虽无法分析缺页但用户可见，
            # 不再是"静默通过"（GMP 合规工具的核心要求）。
            pdf_total = await asyncio.to_thread(_pdf_page_count, pdf_path)
            if pdf_total is not None and len(pages) != pdf_total:
                missing = pdf_total - len(pages)
                warn_msg = (
                    f"OCR 页数与 PDF 物理页数不一致: PDF {pdf_total} 页, "
                    f"OCR 返回 {len(pages)} 页（差异 {missing} 页）— 分析可能不完整"
                )
                logger.warning(f"[{job_id}] {warn_msg}")
                await _audit_log(db, job_id, "stage1_pagemismatch", warn_msg)
                missing_pages = list(range(len(pages) + 1, pdf_total + 1))
                for pn in missing_pages:
                    if pn not in failed_pages:
                        failed_pages.append(pn)

            # Check cancellation
            if await _is_cancelled(job_id):
                return

            # Save raw HTML to page_cache (skip if already exists)
            existing_pages = await _get_existing_pages(db, job_id)
            new_pages = 0
            for i, page in enumerate(pages):
                page_num = i + 1
                if page_num in existing_pages:
                    continue
                raw_html = page.get("markdown", {}).get("text", "")
                # OCR 不完整标记（robustness-A2）：MinerU 低置信度丢弃块计数。
                # 在 raw_html 前注入警告行，LLM 能看到"此页 OCR 不完整"并降低
                # 分析置信度，前端原始页文本区域同样可见。
                discarded_count = page.get("_discarded_count")
                if discarded_count:
                    raw_html = (
                        f"[OCR 警告: 本页有 {discarded_count} 个内容块因置信度过低"
                        f"被 OCR 丢弃, 以下内容可能不完整, 分析仅供参考]\n\n{raw_html}"
                    )
                raw_html = _sanitize_ocr_text(raw_html)
                await db.execute(
                    "INSERT OR IGNORE INTO page_cache (job_id, page, raw_html) VALUES (?, ?, ?)",
                    (job_id, page_num, raw_html),
                )
                new_pages += 1
            await db.execute(
                "UPDATE jobs SET total_pages = ? WHERE id = ?",
                (len(pages), job_id),
            )

            # 空页自动重试（OCR 完整性抗挫折）：
            # MinerU 服务端处理超大 PDF（百 MB 级）存在丢页缺陷 — 同一页
            # 在大文件里输出空、小切片后完整识别（51 页实测丢 6 页，单页
            # 切片 1111-1702 字符全部完整）。对空页做小批量切片重跑并替换
            # page_cache，重跑仍空则保留原样（服务端也识别不了，走既有
            # _ocr_empty 提示路径）。只对 mineru 且页数 >= 10 的文件触发，
            # 避免小文件多余开销；sliced 路径（OCR_SLICES>1）本身就按片跑。
            # F5d 覆盖面修复：原实现只检查本次 OCR 新返回的 pages 且要求
            # used_backend=="mineru" — retry 复用缓存（F3, used_backend=
            # "cached"）时空页检测被跳过，丢页缺陷时期遗留的历史空页永远
            # 不会被恢复（真实 51pages job 6 页空页实证，用户复核只见
            # "## 第 N 页" 标题）。改为对 page_cache 统一检出空页，后端
            # 判定兼容 cached（回查 jobs.ocr_backend_used 保留的上次真实后端）。
            self_heal_backend = used_backend
            if self_heal_backend == "cached":
                cursor = await db.execute(
                    "SELECT ocr_backend_used FROM jobs WHERE id = ?", (job_id,)
                )
                row = await cursor.fetchone()
                self_heal_backend = row["ocr_backend_used"] if row else ""
            # 空页自愈（Round 3 A1 扩展）：MinerU（服务端 >100MB 丢页缺陷）
            # 与 Paddle（服务端组装失败/极短页）都启用。Paddle 无切片 API，
            # 走 fitz 提取单页独立 PDF 重新提交一次。
            if self_heal_backend in ("mineru", "paddle"):
                # 空页判定增强（对抗审查 cr-17）：仅看 raw_html 长度会漏判
                # "标签多、文字少"的页（如 <table><tr><td></td></tr></table>
                # 无文字模板 >100 字符）。去 HTML 标签后按真实文本长度判定。
                # 小文件（<10 页）也启用自愈 — 单页切片重跑仅一次调用
                # （~5s），收益大于小文件直接静默空页的成本。
                cursor = await db.execute(
                    "SELECT page, raw_html FROM page_cache WHERE job_id = ? "
                    "ORDER BY page",
                    (job_id,),
                )
                retry_targets = []
                for r in await cursor.fetchall():
                    html = r["raw_html"] or ""
                    if len(html) < 100:
                        retry_targets.append(r["page"])
                        continue
                    stripped = re.sub(r"<[^>]+>", "", html).strip()
                    if len(stripped) < 100:
                        retry_targets.append(r["page"])
                if retry_targets:
                    logger.warning(
                        f"[{job_id}] {len(retry_targets)} empty pages detected — "
                        f"retrying as small slices: p{retry_targets}"
                    )
                    await _audit_log(
                        db, job_id, "stage1_empty_pages",
                        f"pages={retry_targets} — retrying with per-page slices",
                    )
                    try:
                        if self_heal_backend == "mineru":
                            from core.mineru_client import run_ocr_pages

                            recovered = []
                            still_empty = list(retry_targets)
                            # 两轮重试：首轮 3 页小批（快）；未恢复页第二轮单页批
                            # （服务端丢页有随机性，单页批成功率最高 — 实测
                            # 3 页批 4/6 恢复、单页批全部恢复过）。
                            for attempt, batch_size in enumerate((3, 1)):
                                if not still_empty:
                                    break
                                # 对抗审查 P2-9：两轮重试各需 15-40s+，期间用户
                                # 取消 → 继续跑完并写库，浪费上游配额且取消
                                # 不生效。每轮前检查取消，中断并保留已恢复页。
                                if await _is_cancelled(job_id):
                                    logger.info(
                                        f"[{job_id}] Empty-page retry cancelled "
                                        f"(round {attempt + 1}) — "
                                        f"keeping {len(recovered)} recovered pages"
                                    )
                                    break
                                logger.info(
                                    f"[{job_id}] Empty-page retry round {attempt + 1} "
                                    f"(batch_size={batch_size}): p{still_empty}"
                                )
                                retried = await asyncio.to_thread(
                                    run_ocr_pages,
                                    pdf_path,
                                    still_empty,
                                    job_id=job_id,
                                    batch_size=batch_size,
                                )
                                still_empty = []
                                for pno, md, discarded in retried:
                                    if md and len(md.strip()) > 100:
                                        clean = _sanitize_ocr_text(md.strip())
                                        # D3 修复（Round 3）：自愈恢复页也补回
                                        # OCR 不完整警告前缀（主流程 L714 对
                                        # 自愈路径不生效，恢复页曾被静默当作
                                        # 完整页 — LLM 置信度虚高）。
                                        if discarded:
                                            clean = (
                                                f"[OCR 警告: 本页有 {discarded} 个内容块"
                                                f"因置信度过低被 OCR 丢弃, 以下内容可能"
                                                f"不完整, 分析仅供参考]\n\n{clean}"
                                            )
                                        await db.execute(
                                            "UPDATE page_cache SET raw_html = ?, "
                                            "structured_json = NULL, analyzed_at = NULL "
                                            "WHERE job_id = ? AND page = ?",
                                            (clean, job_id, pno),
                                        )
                                        recovered.append(pno)
                                    else:
                                        still_empty.append(pno)
                                await db.commit()
                                if not still_empty:
                                    break
                        else:
                            # Paddle：fitz 提取单页为独立 PDF 重提交一次。
                            # Paddle 无切片接口且服务端无大文件丢页缺陷 —
                            # 空页大概率是单次服务端波动/真空白页，一轮足够。
                            # 真空白页（扫描件末页）重跑后仍空 → 保留
                            # _ocr_empty 标记走人工复核路径。
                            import fitz
                            from core import ocr_client

                            job_dir_p = Path(config["app"].output_dir) / job_id
                            recovered = []
                            still_empty = []
                            src_doc = fitz.open(pdf_path)
                            try:
                                for pno in retry_targets:
                                    if await _is_cancelled(job_id):
                                        logger.info(
                                            f"[{job_id}] Paddle empty-page retry "
                                            f"cancelled — keeping {len(recovered)} "
                                            f"recovered pages"
                                        )
                                        break
                                    logger.info(
                                        f"[{job_id}] Paddle self-heal: re-OCR page "
                                        f"{pno} as standalone slice"
                                    )
                                    slice_path = job_dir_p / f"selfheal-p{pno}.pdf"
                                    out = fitz.open()
                                    out.insert_pdf(
                                        src_doc, from_page=pno - 1, to_page=pno - 1
                                    )
                                    out.save(str(slice_path))
                                    out.close()
                                    try:
                                        pages = await asyncio.to_thread(
                                            ocr_client.run_ocr, str(slice_path)
                                        )
                                        md = (
                                            pages[0]["markdown"]["text"]
                                            if pages and len(pages) > 0
                                            else ""
                                        )
                                    except Exception as slice_err:
                                        logger.warning(
                                            f"[{job_id}] Paddle self-heal p{pno} "
                                            f"failed: "
                                            f"{redact_urls(str(slice_err))[:200]}"
                                        )
                                        md = ""
                                    finally:
                                        slice_path.unlink(missing_ok=True)
                                    if md and len(md.strip()) > 100:
                                        clean = _sanitize_ocr_text(md.strip())
                                        await db.execute(
                                            "UPDATE page_cache SET raw_html = ?, "
                                            "structured_json = NULL, analyzed_at = NULL "
                                            "WHERE job_id = ? AND page = ?",
                                            (clean, job_id, pno),
                                        )
                                        recovered.append(pno)
                                    else:
                                        still_empty.append(pno)
                                    await db.commit()
                            finally:
                                src_doc.close()
                        if recovered:
                            logger.info(
                                f"[{job_id}] Empty-page retry: recovered "
                                f"{len(recovered)}/{len(retry_targets)} pages: "
                                f"p{recovered}"
                            )
                            await _audit_log(
                                db, job_id, "stage1_empty_recovered",
                                f"recovered_pages={recovered}",
                            )
                        if still_empty:
                            logger.warning(
                                f"[{job_id}] Empty-page retry: still empty "
                                f"after re-OCR — p{still_empty} truly "
                                f"unrecognizable by the OCR backend"
                            )
                    except Exception as retry_err:
                        logger.error(
                            f"[{job_id}] Empty-page retry failed: {retry_err}"
                        )
            await transition_status(db, job_id, "ocr_done", f"Stage 1 complete: {len(pages)} pages")
            await db.commit()
            logger.info(
                f"[{job_id}] DB: page_cache inserted {new_pages} new pages, "
                f"jobs.total_pages={len(pages)} ({len(existing_pages)} cached)"
            )

            # Check cancellation
            if await _is_cancelled(job_id):
                return

            # ── Stage 2: Per-page LLM analysis (concurrent) ─────────
            await transition_status(db, job_id, "analyzing", "Stage 2 start")
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

            # Run all page analyses concurrently
            tasks = [
                asyncio.create_task(
                    _analyze_one(db, job_id, pn, pg, sem, failed_pages,
                                 state_lock, completed, total_pages)
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
                if await _is_cancelled(job_id):
                    for t in tasks:
                        t.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    break

            stage2_ms = int((time.time() - stage2_start) * 1000)
            logger.info(
                f"[{job_id}] Stage 2: Complete in {stage2_ms}ms, "
                f"{len(failed_pages)} pages failed"
            )
            await _audit_log(db, job_id, "stage2_complete",
                             f"duration={stage2_ms}ms failed={failed_pages}")

        # Check cancellation
        if await _is_cancelled(job_id):
            return

        # ── Stage 3: Cross-page analysis ────────────────────────
        logger.info(f"[{job_id}] Stage 3: Cross-page analysis...")
        stage3_start = time.time()

        cursor = await db.execute(
            "SELECT page, structured_json FROM page_cache WHERE job_id = ? ORDER BY page",
            (job_id,),
        )
        page_structures = []
        for row in await cursor.fetchall():
            if row["structured_json"]:
                try:
                    data = json.loads(row["structured_json"])
                    # Skip pages with parse errors
                    if not data.get("_parse_error") and not data.get("_ocr_empty"):
                        page_structures.append({"page": row["page"], "data": data})
                except json.JSONDecodeError:
                    logger.warning(f"[{job_id}] Stage 3: Failed to parse page_cache page={row['page']}")

        # P-C3 修复：analyze_cross_page 调用前检查取消状态，
        # 避免取消后仍进入跨页分析（cancelling → review 非法转换）
        if await _is_cancelled(job_id):
            return
        findings = await analyze_cross_page(page_structures, job_id=job_id)
        # P-C3 修复：analyze_cross_page 调用后再检查一次取消状态，
        # 避免在跨页分析期间用户点取消后继续写入 findings / 转 review
        if await _is_cancelled(job_id):
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
        cur = await db.execute(
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
        batch_rows: list[tuple] = []
        for f in findings:
            # 跳过已在 Stage 2 写入的 page-level LLM findings
            if f.get("source") == "llm_page":
                skipped_llm_page += 1
                continue
            # robustness-B4: retry 会重新执行 Stage 3，确定性生成的 findings
            # 按 (job_id, source, page, type, description) 指纹去重。
            fingerprint = (
                job_id, f.get("source", "rule"), f["page"], f["type"], f["description"],
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
                job_id, f["page"], f["type"], f["severity"], f["description"],
                f.get("ocr_text"), f.get("operator"), f.get("source", "rule"),
                f.get("rule_id") if f.get("source") == "user_rule" else None,
            ))
            inserted += 1
        if batch_rows:
            # 对抗审查 T3.2：批量写 findings 与其他写事务（transition/audit/
            # 进度更新）串行化 —— 多 job 并行进入 Stage 3 时防止 executemany+
            # commit 与其他写穿插交错事务边界。
            async with db_lock:
                await db.executemany(
                    "INSERT OR IGNORE INTO findings "
                    "(job_id, page, type, severity, description, ocr_text, operator, source, user_rule_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    batch_rows,
                )
                await db.commit()
        logger.info(
            f"[{job_id}] DB: findings inserted ({inserted} new + {skipped_llm_page} "
            f"llm_page skipped, severity={severity_counts})"
        )

        # Determine final status
        total_cost_ms = int((time.time() - pipeline_start) * 1000)
        final_status = "partial_review" if failed_pages else "review"

        await db.execute(
            "UPDATE jobs SET finished_at = CURRENT_TIMESTAMP, "
            "stage1_ms = ?, stage2_ms = ?, stage3_ms = ?, failed_pages = ? "
            "WHERE id = ?",
            (stage1_ms, stage2_ms, stage3_ms,
             json.dumps(failed_pages) if failed_pages else None, job_id),
        )
        await transition_status(db, job_id, final_status,
                                f"Pipeline complete: {len(findings)} findings, {len(failed_pages)} failed")

        # 飞书通知（旁路：失败不影响主流程；成功/部分完成均推送）
        try:
            from core.notify import notify_job
            await notify_job(job_id, final_status)
        except Exception:
            pass  # notify_job 自身已兜底，此处双保险防异常逃逸

        logger.info(f"[{job_id}] Pipeline complete: status={final_status}, "
                     f"{len(findings)} findings, {len(failed_pages)} failed pages, "
                     f"total={total_cost_ms}ms (OCR={stage1_ms} LLM={stage2_ms} Cross={stage3_ms})")
        await _audit_log(db, job_id, "pipeline_complete",
                         f"status={final_status} findings={len(findings)} "
                         f"failed={len(failed_pages)} total={total_cost_ms}ms")

    except asyncio.CancelledError:
        # 优雅关闭：应用退出时 background task 被取消
        # 将 job 标记为 error（带"中断"标记），允许用户重试
        logger.warning(f"[{job_id}] Pipeline cancelled (app shutdown or task revoke)")
        try:
            await transition_status(db, job_id, "error", "Pipeline cancelled (interrupted)")
        except InvalidTransitionError:
            # 已是终态（review/cancelled），保持不变
            pass
        # 重新抛出让 asyncio 正常清理
        raise
    except InvalidTransitionError as e:
        # P-C3 修复：Stage 3 期间用户取消 → status=cancelling，pipeline 末尾
        # 调用 transition_status(..., "review") 会因 cancelling → review 非法
        # 而抛 InvalidTransitionError。原代码仅记日志不做状态恢复，job 卡在
        # cancelling（非终态）。此处检查当前状态：cancelling → cancelled（合法
        # 终态），否则 → error，确保 job 进入终态。
        logger.warning(f"[{job_id}] Invalid transition at pipeline end: {e}")
        final_recovery_status = None
        try:
            cur = await db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
            cur_row = await cur.fetchone()
            cur_status = cur_row["status"] if cur_row else None
            if cur_status == "cancelling":
                await transition_status(db, job_id, "cancelled", "cancelled during stage 3")
                final_recovery_status = "cancelled"
            else:
                # 其他非预期状态 → error（直接 UPDATE，与 recover_stuck_jobs 同模式）
                await db.execute(
                    "UPDATE jobs SET status = 'error', error_message = ?, "
                    "finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (f"Pipeline failed: invalid transition {e}", job_id),
                )
                await db.commit()
                final_recovery_status = "error"
        except Exception as recover_err:
            # 恢复失败也不抛出，避免异常逃逸导致 job 卡死；记日志供排查
            logger.error(
                f"[{job_id}] Recovery after InvalidTransition failed: {recover_err}",
                exc_info=True,
            )
        # 与正常 error 终态一致地发终态通知（对抗审查：此前该恢复路径
        # 静默无通知，GMP 任务失败用户不知道）
        if final_recovery_status:
            try:
                from core.notify import notify_job

                await notify_job(job_id, final_recovery_status)
            except Exception:
                pass  # notify_job 自身已兜底，此处双保险防异常逃逸
    except Exception as e:
        logger.error(f"[{job_id}] Pipeline failed: {redact_urls(str(e))}", exc_info=True)
        await _audit_log(db, job_id, "pipeline_error", redact_urls(str(e))[:200])
        # P1-7：签名 URL 反刍兜底 — OCR 客户端异常消息可能回显完整签名 URL
        # （requests MaxRetryError），error_message 会出现在报告/飞书通知中
        error_msg = redact_urls(str(e))[:500]
        try:
            await transition_status(db, job_id, "error", f"Pipeline failed: {redact_urls(str(e))[:100]}")
            # transition_status 只更新 status 字段，还需显式写入 error_message + finished_at
            await db.execute(
                "UPDATE jobs SET error_message = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                (error_msg, job_id),
            )
            await db.commit()
            # 飞书通知（旁路，失败不影响主流程）
            try:
                from core.notify import notify_job
                await notify_job(job_id, "error")
            except Exception:
                pass  # notify_job 自身已兜底，此处双保险防异常逃逸
        except InvalidTransitionError as ie:
            # P-W7 修复：已是终态，直接更新 error_message；但 DB 写入本身可能
            # 失败（连接断开/锁竞争），用内层 try/except 兜住，避免异常逃逸
            # 导致 job 卡在非终态。记日志但不抛出。
            logger.warning(f"[{job_id}] {ie}")
            try:
                await db.execute(
                    "UPDATE jobs SET error_message = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (error_msg, job_id),
                )
                await db.commit()
            except Exception as audit_err:
                logger.error(
                    f"[{job_id}] Failed to update error_message after InvalidTransition: {audit_err}",
                    exc_info=True,
                )


async def _analyze_one(
    db, job_id: str, page_num: int, page: dict,
    sem: asyncio.Semaphore, failed_pages: list[int],
    state_lock: asyncio.Lock, completed: dict, total_pages: int,
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
    """
    # 取消检查：跳过后续页的 LLM 调用；已在运行的调用会自然结束
    # （HTTP 请求无法中途打断）。
    if await _is_cancelled(job_id):
        logger.info(f"[{job_id}] Stage 2: Skipped page {page_num} (cancelled)")
        return

    raw_html = page.get("markdown", {}).get("text", "")
    async with sem:
        try:
            try:
                page_start = time.time()
                # P1-2: 取消检查点注入 — analyze_page 在 LLM 调用之间（chat_json
                # 后 / schema 修复重试前）轮询 cancel_check，取消后不再发起新的
                # LLM 调用（此前重试链最长 ~12 分钟，cancel 后 job 迟迟不终态，
                # delete/archive 被拒且文案"数秒"严重不符）。
                structured = await analyze_page(
                    raw_html, page_num=page_num, job_id=job_id,
                    cancel_check=lambda: _is_cancelled(job_id),
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
                        "UPDATE page_cache SET structured_json = ?, analyzed_at = CURRENT_TIMESTAMP "
                        "WHERE job_id = ? AND page = ?",
                        (json.dumps(error_data, ensure_ascii=False), job_id, page_num),
                    )
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
                    "UPDATE page_cache SET structured_json = ?, analyzed_at = CURRENT_TIMESTAMP "
                    "WHERE job_id = ? AND page = ?",
                    (payload, job_id, page_num),
                )
                # 流式输出：立即把该页 LLM 产生的 findings 写入 findings 表。
                # 对抗审查(cr-3): llm_page 路径同样依赖 idx_findings_dedup UNIQUE
                # 索引（v5）做原子去重，防御"部分提交残留 + retry"组合路径下的重复行。
                llm_page_rows = []
                for f in page_findings:
                    if not isinstance(f, dict):
                        continue
                    if not {"type", "severity", "description"}.issubset(f.keys()):
                        continue
                    llm_page_rows.append((
                        job_id, page_num, f.get("type", "info"),
                        f.get("severity", "info"), f.get("description", ""),
                        f.get("ocr_text", ""), f.get("operator", ""),
                    ))
                if llm_page_rows:
                    await db.executemany(
                        "INSERT OR IGNORE INTO findings "
                        "(job_id, page, type, severity, description, ocr_text, operator, source) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'llm_page')",
                        llm_page_rows,
                    )
                await db.commit()
                logger.info(
                    f"[{job_id}] DB: page_cache updated + {len(page_findings)} "
                    f"page-level findings inserted (page={page_num})"
                )
        except Exception as e:
            logger.error(
                f"[{job_id}] Stage 2: Page {page_num} failed: {e}", exc_info=True
            )
            async with state_lock:
                failed_pages.append(page_num)
            error_data = {
                "page_number": page_num,
                "_parse_error": True,
                "_error": str(e)[:200],
                "overall_confidence": "low",
            }
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
                    "UPDATE page_cache SET structured_json = ?, analyzed_at = CURRENT_TIMESTAMP "
                    "WHERE job_id = ? AND page = ?",
                    (json.dumps(error_data, ensure_ascii=False), job_id, page_num),
                )
                await db.commit()
                logger.warning(f"[{job_id}] DB: page_cache updated with _parse_error (page={page_num})")
            return

    async with state_lock:
        completed["n"] += 1
    logger.info(
        f"[{job_id}] Stage 2: Page {page_num}/{total_pages} analyzed "
        f"({completed['n']} done)"
    )


async def _run_sliced_stage1_2(
    db, job_id: str, pdf_path: str, slice_pages: int, ocr_progress_cb,
) -> tuple[int, int, list[int], int]:
    """MinerU 分片 OCR + 渐进分析（流式输出核心，问题 2）。

    分片 OCR：PDF 切成多片独立提交 MinerU batch 并行轮询；**一片 OCR
    完成立即落库 page_cache 并启动该片页面的 LLM 分析**（不等其他片），
    用户无需等全部页 OCR 完成才看到 findings。

    返回 (stage1_ms, stage2_ms, failed_pages, total_pages)。
    取消时提前返回（stage1/2_ms 可能为部分值），主流程的取消检查兜底。
    """
    from core.mineru_client import run_ocr_sliced

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def _on_batch(start_page: int, pages: list, total: int):
        loop.call_soon_threadsafe(q.put_nowait, (start_page, pages, total))

    ocr_task: asyncio.Task = asyncio.create_task(
        asyncio.to_thread(
            run_ocr_sliced, pdf_path, slice_pages, _on_batch,
            ocr_progress_cb, job_id=job_id,
        )
    )

    concurrency = config["app"].llm_concurrency
    sem = asyncio.Semaphore(concurrency)
    state_lock = asyncio.Lock()
    failed_pages: list[int] = []
    completed = {"n": 0}
    analysis_tasks: list[asyncio.Task] = []

    stage1_start = time.time()
    existing = await _get_existing_pages(db, job_id)
    analyzed = await _get_analyzed_pages(db, job_id)
    new_pages = 0
    total_pages = 0
    seen_max = 0
    # 对抗审查 P1-1：记录已收到回调的页区间，OCR 结束后计算缺口。
    # 原实现只在 seen_max < total_pages 时补记尾页 — 中间片失败时
    # （如第 2 片失败、第 3 片成功）seen_max 可达 total，缺页静默通过。
    received_ranges: list[tuple[int, int]] = []

    while True:
        try:
            start_page, pages, total = await asyncio.wait_for(q.get(), timeout=_SLICE_QUEUE_TIMEOUT)
        except asyncio.TimeoutError:
            if ocr_task.done():
                exc = ocr_task.exception()
                if exc is not None:
                    # P1-1 修复：单片 OCR 失败不再整单 error — 已产出的片
                    # 照常分析（analysis_tasks 已入队），缺失页由下方
                    # stage1_complete 前的缺页补记并入 failed_pages，job
                    # 以 partial_review 收尾，用户可见而非静默炸单。
                    logger.warning(
                        f"[{job_id}] Sliced OCR failed mid-way: {exc} — "
                        f"continuing with {seen_max} produced pages"
                    )
                    await _audit_log(db, job_id, "stage1_slice_failed", str(exc)[:500])
                break
            continue
        total_pages = total
        if pages:
            seen_max = max(seen_max, start_page + len(pages) - 1)
            received_ranges.append((start_page, start_page + len(pages) - 1))
        # 该片页面落库（INSERT OR IGNORE 兼容 resume）
        # P1-3 修复：落库 + commit 整体持 db_lock — 否则与 _analyze_one 的
        # rollback() 竞争：分析失败回滚会撤销本循环尚未提交的 INSERT，
        # 随后本循环的 commit 提交空事务 → 该片 raw_html 永久丢失。
        async with db_lock:
            for i, page in enumerate(pages):
                page_num = start_page + i
                if page_num in existing:
                    continue
                raw_html = page.get("markdown", {}).get("text", "")
                # OCR 不完整标记（与整份路径 653-658 保持一致）：sliced 路径
                # 曾遗漏 _discarded_count 警告注入 — 分片模式下同一页由单片
                # OCR 产出，丢弃块的风险同样存在，LLM 需感知。
                discarded_count = page.get("_discarded_count")
                if discarded_count:
                    raw_html = (
                        f"[OCR 警告: 本页有 {discarded_count} 个内容块因置信度过低"
                        f"被 OCR 丢弃, 以下内容可能不完整, 分析仅供参考]\n\n{raw_html}"
                    )
                raw_html = _sanitize_ocr_text(raw_html)
                await db.execute(
                    "INSERT OR IGNORE INTO page_cache (job_id, page, raw_html) VALUES (?, ?, ?)",
                    (job_id, page_num, raw_html),
                )
                new_pages += 1
            await db.execute(
                # 对抗审查 P1-1 关联：必须用 split_pdf 的权威总数 total_pages，
                # 不能用 seen_max — 中间片失败时 seen_max < 真实页数，会把
                # jobs.total_pages 永久覆盖成小值（复核页导航/报告全错）
                "UPDATE jobs SET total_pages = ? WHERE id = ?", (total_pages, job_id)
            )
            await db.commit()
            logger.info(
            f"[{job_id}] Stage 1 (sliced): slice start={start_page} pages={len(pages)} "
            f"persisted ({new_pages} new)"
        )
        # 立即启动该片页面分析（跳过已分析页）
        for i, page in enumerate(pages):
            page_num = start_page + i
            if page_num in existing or page_num in analyzed:
                continue
            analysis_tasks.append(
                asyncio.create_task(
                    _analyze_one(
                        db, job_id, page_num, page, sem, failed_pages,
                        state_lock, completed, total_pages,
                    )
                )
            )
        if await _is_cancelled(job_id):
            # 取消已排队的分析任务：否则孤儿协程继续跑 LLM（最长 240s）
            # 并写入已取消的 job（对抗审查 — retry 后新旧 pipeline 对同一
            # 页写入互相竞争，应用退出时还抛 "Task was destroyed"）。
            for t in analysis_tasks:
                t.cancel()
            if analysis_tasks:
                await asyncio.gather(*analysis_tasks, return_exceptions=True)
            return 0, 0, failed_pages, total_pages

    stage1_ms = int((time.time() - stage1_start) * 1000)
    # 对抗审查 P1-1：缺页显式暴露 — 按"已收到回调的区间"计算全部缺口
    # （不仅限尾页）。中间片失败（第 2 片失败、第 3 片成功）时原逻辑
    # seen_max==total 会漏报，GMP 复核页数错乱而 job 仍显示成功。
    if total_pages:
        received_ranges.sort()
        missing_pages: list[int] = []
        ptr = 1
        for s, e in received_ranges:
            for pn in range(ptr, min(s, total_pages + 1)):
                missing_pages.append(pn)
            ptr = max(ptr, e + 1)
        for pn in range(ptr, total_pages + 1):
            missing_pages.append(pn)
        for pn in missing_pages:
            if pn not in failed_pages:
                failed_pages.append(pn)
        if missing_pages:
            logger.warning(
                f"[{job_id}] Sliced OCR pages missing: {missing_pages} "
                f"(total={total_pages}, produced={received_ranges})"
            )
            await _audit_log(
                db, job_id, "stage1_pagemismatch",
                f"sliced: missing {len(missing_pages)}/{total_pages} pages "
                f"ranges={received_ranges}",
            )
    logger.info(
        f"[{job_id}] Stage 1 (sliced): OCR complete: {new_pages} new pages "
        f"(total={total_pages}) in {stage1_ms}ms"
    )
    # 分片路径也记录实际后端（分片仅 MinerU 支持）— 与整单路径 :529 对齐，
    # 保证 GMP 审计字段 ocr_backend_used 在所有路径下都有值
    await db.execute(
        "UPDATE jobs SET ocr_backend_used = ? WHERE id = ?", ("mineru", job_id)
    )
    await _audit_log(db, job_id, "stage1_complete",
                     f"pages={total_pages} duration={stage1_ms}ms")
    await transition_status(db, job_id, "ocr_done", f"Stage 1 (sliced) complete: {total_pages} pages")
    await db.commit()
    if await _is_cancelled(job_id):
        # 与片内取消分支一致：先取消并等待已排队分析任务再退出
        for t in analysis_tasks:
            t.cancel()
        if analysis_tasks:
            await asyncio.gather(*analysis_tasks, return_exceptions=True)
        return stage1_ms, 0, failed_pages, total_pages

    # Stage 2 收尾：等待所有已排队的分析任务（含最后一片刚入队的）
    await transition_status(db, job_id, "analyzing", "Stage 2 (sliced) start")
    stage2_start = time.time()
    await asyncio.gather(*analysis_tasks)
    stage2_ms = int((time.time() - stage2_start) * 1000)
    logger.info(
        f"[{job_id}] Stage 2 (sliced): Complete in {stage2_ms}ms, "
        f"{len(failed_pages)} pages failed"
    )
    await _audit_log(db, job_id, "stage2_complete",
                     f"duration={stage2_ms}ms failed={failed_pages}")
    return stage1_ms, stage2_ms, failed_pages, total_pages


async def _is_cancelled(job_id: str) -> bool:
    """Check if job has been cancelled. Transitions cancelling → cancelled via
    _transition_status_unlocked so the audit_log entry is written (no bypass).

    P-C2 修复：本函数已持 db_lock，调用 _transition_status_unlocked（不加锁）
    而非 transition_status，避免 asyncio.Lock 不可重入导致死锁。
    """
    db = await get_db()
    async with db_lock:
        cursor = await db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        if row and row["status"] in ("cancelling", "cancelled", "error"):
            if row["status"] == "cancelling":
                # Use _transition_status_unlocked so audit_log captures the transition
                # （已持 db_lock，不能再调用会加锁的 transition_status）
                try:
                    await _transition_status_unlocked(db, job_id, "cancelled", "Pipeline acknowledged cancel")
                except InvalidTransitionError as e:
                    # Race: another caller already transitioned; log and treat as cancelled
                    logger.warning(f"[{job_id}] Cancel transition race: {e}")
            # Always set finished_at (transition_status doesn't set this column)
            await db.execute(
                "UPDATE jobs SET finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
            )
            await db.commit()
            logger.info(f"[{job_id}] Pipeline cancelled")
            return True
    return False


async def _update_ocr_progress(job_id: str, done: int, total: int) -> None:
    """更新 job.ocr_progress（JSON）供 SSE 实时推送。

    由 OCR 轮询线程通过 run_coroutine_threadsafe 调用；用 db_lock
    串行化，避免与 Stage 2 的并发 DB 写入冲突。
    """
    db = await get_db()
    payload = json.dumps({"done": done, "total": total})
    try:
        async with db_lock:
            await db.execute(
                "UPDATE jobs SET ocr_progress = ? WHERE id = ?", (payload, job_id)
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"[{job_id}] OCR progress update failed: {e}")


async def _get_existing_pages(db, job_id: str) -> set[int]:
    """Get page numbers that already have raw_html in page_cache."""
    cursor = await db.execute(
        "SELECT page FROM page_cache WHERE job_id = ?", (job_id,)
    )
    return {row["page"] for row in await cursor.fetchall()}


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
