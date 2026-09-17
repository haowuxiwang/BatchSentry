"""Job status + SSE progress stream."""
from __future__ import annotations

import json
import logging

from fastapi import HTTPException, Request

from db.client import get_db
from api.jobs import _TERMINAL_STATUSES, router
from api.jobs.page_image import _page_finding_counts

logger = logging.getLogger(__name__)


async def _count_analyzed_pages(db, job_id: str) -> int:
    """已**产出可用结果**的页数。

    口径唯一来自 `core.pipeline.stage2._get_analyzed_pages`（排除
    `_parse_error` 占位页）。

    #131：此前这里直接 `COUNT(*) ... WHERE structured_json IS NOT NULL`，
    但失败页同样会写入 structured_json（带 `_parse_error` 标记）→ 失败页被
    算作"已分析"。后果是终态 `error`（原因文本写"0 页产出可用结果"）与
    同一份响应里的 `pages_analyzed=1` **自相矛盾**，GMP 审阅会当场追问
    "到底分析了几页"。现复用同一函数而非复制 SQL 字符串——避免再出现
    第二个真值源（§十五 同款教训：可见性字段必须与真值同源）。
    """
    from core.pipeline.stage2 import _get_analyzed_pages

    return len(await _get_analyzed_pages(db, job_id))


@router.get("/{job_id}")
async def get_job_status(job_id: str, request: Request = None):
    """Get job status, progress, and findings summary."""
    # P2-1: GET 读端点守卫统一
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = await cursor.fetchone()
    if not job:
        raise HTTPException(404, "Job not found")

    cursor = await db.execute(
        "SELECT COUNT(*) FROM page_cache WHERE job_id = ?", (job_id,)
    )
    pages_ocr = (await cursor.fetchone())[0]

    pages_analyzed = await _count_analyzed_pages(db, job_id)

    cursor = await db.execute(
        "SELECT COUNT(*) FROM findings WHERE job_id = ?", (job_id,)
    )
    total_findings = (await cursor.fetchone())[0]

    cursor = await db.execute(
        "SELECT COUNT(*) FROM findings WHERE job_id = ? AND status = 'pending'", (job_id,)
    )
    review_findings = (await cursor.fetchone())[0]

    page_finding_counts = await _page_finding_counts(db, job_id)

    return {
        "id": job["id"],
        "filename": job["filename"],
        "status": job["status"],
        "total_pages": job["total_pages"],
        # #132：必须解成 list —— 原样透传会得到 "[2, 1]" 字符串，前端
        # `Array.isArray()` 拿到即静默丢弃（见 _parse_failed_pages）。
        "failed_pages": _parse_failed_pages(job["failed_pages"]),
        "pages_ocr_done": pages_ocr,
        "pages_analyzed": pages_analyzed,
        "total_findings": total_findings,
        "review_findings": review_findings,
        "created_at": job["created_at"],
        "finished_at": job["finished_at"],
        "error_message": job["error_message"],
        "stage1_ms": job["stage1_ms"],
        "stage2_ms": job["stage2_ms"],
        "stage3_ms": job["stage3_ms"],
        "ocr_progress": _parse_ocr_progress(job["ocr_progress"] if "ocr_progress" in job.keys() else None),
        "self_heal_progress": _parse_self_heal_progress(job["ocr_progress"] if "ocr_progress" in job.keys() else None),
        "cross_progress": _parse_cross_progress(job["ocr_progress"] if "ocr_progress" in job.keys() else None),
        "phase": _derive_phase(job["status"], pages_analyzed, job["total_pages"] or 0,
                               cross_started=bool(_parse_cross_progress(job["ocr_progress"] if "ocr_progress" in job.keys() else None))),
        "page_finding_counts": page_finding_counts,
        "ocr_backend_used": job["ocr_backend_used"] if "ocr_backend_used" in job.keys() else None,
        "ocr_backend_display": _ocr_backend_display(job),
        "stall": _stall_payload(job),
    }

def _ocr_backend_display(job) -> str | None:
    """ocr_backend_used → 中文显示名（zh_map 单一来源）。"""
    raw = job["ocr_backend_used"] if "ocr_backend_used" in job.keys() else None
    if not raw:
        return None
    from core.zh_map import zh_ocr_backend
    return zh_ocr_backend(raw)


def _stall_payload(job) -> dict | None:
    """派生停滞可见性（P2，「停滞可见性」）—— 见 `core.watchdog.stall_report`。

    **派生量，不写库**：空闲秒数与阈值都能从既有字段算出，落库只会在
    每次推送时多一次 fsync，并让"这个字段过期了"成为新的失效模式。
    阈值口径由 watchdog 单一提供（含 PBC_WATCHDOG_SCALE 现场缩放）。

    行内无 `last_activity_at` 列（如精简投影漏列）时返回 None —— 明确
    "判不了"，而不是拿 0 冒充"没停滞"。
    """
    if "last_activity_at" not in job.keys():
        return None
    from core.watchdog import stall_report

    try:
        return stall_report(
            job["status"],
            job["total_pages"] if "total_pages" in job.keys() else None,
            job["last_activity_at"],
        )
    except Exception:  # 可见性是增强项：绝不因此打断状态查询
        logger.warning("stall_report failed (non-fatal)", exc_info=True)
        return None


async def _get_job_progress(db, job_id: str) -> dict:
    """获取 job 进度快照（SSE 推送用）。

    复用 get_job_status 的查询逻辑，但返回精简字段。
    对抗审查（中文化收尾）：每轮 SSE 推送此前 SELECT * 全列 —
    pdf_path / md5 / error_message 全文等无关字段随每次推送传输；
    改为投影到推送实际使用的列（jobs 表行内多数列从不用于进度）。

    S2（M6/T6.2）：**终态快照缓存**。终态 job 的快照在定义上不再变化
    （findings 只新增于分析期；复核改的是 finding.status 而非计数），
    但仍会被反复查询：`_live_jobs_snapshot` 每轮会带上"近 10 分钟内
    完成的终态 job"，每个终态 job 要跑 4 条 COUNT + 1 条分组统计。
    此处以 (status, finished_at) 为键缓存终态快照，命中直接复用 —
    状态或完成时间变化即失效（retry 复用同一行 id 的场景）。
    活跃 job 照常实时计算。缓存是**唯一一份**（原先 listings 另有一份，
    双套易漂移，已合并到此处）。
    """
    from api.jobs import _ACTIVE_STATUSES

    cursor = await db.execute(
        "SELECT id, status, total_pages, error_message, failed_pages, "
        "finished_at, stage1_ms, stage2_ms, stage3_ms, ocr_progress, "
        "ocr_backend_used, last_activity_at FROM jobs WHERE id = ?", (job_id,)
    )
    job = await cursor.fetchone()
    if not job:
        return None

    status = job["status"]
    if status not in _ACTIVE_STATUSES:
        cached = cached_terminal_snapshot(job_id, status, job["finished_at"])
        if cached is not None:
            return cached

    cursor = await db.execute(
        "SELECT COUNT(*) FROM page_cache WHERE job_id = ?", (job_id,)
    )
    pages_ocr = (await cursor.fetchone())[0]

    pages_analyzed = await _count_analyzed_pages(db, job_id)

    cursor = await db.execute(
        "SELECT COUNT(*) FROM findings WHERE job_id = ?", (job_id,)
    )
    total_findings = (await cursor.fetchone())[0]

    page_finding_counts = await _page_finding_counts(db, job_id)

    progress = {
        "id": job["id"],
        "status": job["status"],
        "total_pages": job["total_pages"] or 0,
        "pages_ocr_done": pages_ocr,
        "pages_analyzed": pages_analyzed,
        "total_findings": total_findings,
        "error_message": job["error_message"],
        # #132：SSE 快照与 GET 状态端点同契约 —— 也必须是 list。
        "failed_pages": _parse_failed_pages(job["failed_pages"]),
        "stage1_ms": job["stage1_ms"],
        "stage2_ms": job["stage2_ms"],
        "stage3_ms": job["stage3_ms"],
        "ocr_progress": _parse_ocr_progress(job["ocr_progress"] if "ocr_progress" in job.keys() else None),
        "self_heal_progress": _parse_self_heal_progress(job["ocr_progress"] if "ocr_progress" in job.keys() else None),
        "cross_progress": _parse_cross_progress(job["ocr_progress"] if "ocr_progress" in job.keys() else None),
        "phase": _derive_phase(job["status"], pages_analyzed, job["total_pages"] or 0,
                               cross_started=bool(_parse_cross_progress(job["ocr_progress"] if "ocr_progress" in job.keys() else None))),
        "page_finding_counts": page_finding_counts,
        "ocr_backend_used": job["ocr_backend_used"] if "ocr_backend_used" in job.keys() else None,
        "ocr_backend_display": _ocr_backend_display(job),
        # P2 停滞可见性（派生）：SSE 每 2s 一帧，用户据此在"被杀之前"就知道
        # 任务无进展，可以自己决定取消/重试。终态 job 不适用 → None。
        "stall": _stall_payload(job),
    }
    if status not in _ACTIVE_STATUSES:
        _store_terminal_snapshot(job_id, _snap_key(status, job["finished_at"]), progress)
    return progress


# 终态快照缓存（S2）—— 见 _get_job_progress docstring。
# 键 = job_id，值 = ((status, finished_at), snapshot)。
_TERMINAL_SNAP_CACHE: dict[str, tuple[tuple, dict]] = {}
_TERMINAL_SNAP_CACHE_MAX = 100


def _snap_key(status: str, finished_at) -> tuple:
    """缓存键：状态 + 完成时间。任一变化即失效（retry 复用同一 job 行）。"""
    return (status, str(finished_at or ""))


def cached_terminal_snapshot(job_id: str, status: str, finished_at) -> dict | None:
    """命中则返回终态快照的浅拷贝，否则 None。

    供**已持有** (status, finished_at) 的调用方直接取用 —— 例如聚合流
    `_live_jobs_snapshot` 每轮先列出候选 job，本就带着这两列；若仍走
    `_get_job_progress`，为了拼缓存键还得再查一次 jobs 行（终态 job 数 × 1
    条查询/轮，白白吃掉缓存收益）。
    """
    hit = _TERMINAL_SNAP_CACHE.get(job_id)
    if hit is not None and hit[0] == _snap_key(status, finished_at):
        return dict(hit[1])
    return None


def _store_terminal_snapshot(job_id: str, key: tuple, progress: dict) -> None:
    """写入缓存（存副本，与调用方持有的 dict 解耦）。

    容量兜底：达上限整体清空重建，而非 LRU —— 终态集合是"近 10 分钟"的
    滚动态，清空后下一轮自然重建，代价可控且实现简单。
    """
    if len(_TERMINAL_SNAP_CACHE) >= _TERMINAL_SNAP_CACHE_MAX:
        _TERMINAL_SNAP_CACHE.clear()
    # 浅拷贝：调用方改一个顶层字段不得改脏缓存节点（曾导致跨请求串数据）。
    _TERMINAL_SNAP_CACHE[job_id] = (key, dict(progress))


def _reset_terminal_snap_cache() -> None:
    """测试钩子：清空终态快照缓存（跨用例隔离）。"""
    _TERMINAL_SNAP_CACHE.clear()

def _parse_ocr_progress(raw) -> dict:
    """解析 jobs.ocr_progress JSON 字符串 → {"done": N, "total": M}。

    容忍 None / 空 / 非法 JSON（返回空 dict，前端回退到 pages_ocr_done）。
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {"done": int(data.get("done", 0)), "total": int(data.get("total", 0))}
    except (ValueError, TypeError):
        pass
    return {}


def _parse_self_heal_progress(raw) -> dict | None:
    """解析 ocr_progress JSON 中的 self_heal 子键（空页自愈进度）。

    主 OCR 完成后自愈期间 done==total 不变，客户端靠该子键显示
    "空页自愈 x/y"；无自愈/未进行中返回 None（前端不显示）。
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
        sh = data.get("self_heal") if isinstance(data, dict) else None
        if isinstance(sh, dict) and sh.get("total"):
            return {
                "done": int(sh.get("done", 0)),
                "total": int(sh.get("total", 0)),
                "pages": [int(p) for p in (sh.get("pages") or [])],
            }
    except (ValueError, TypeError):
        pass
    return None


def _parse_cross_progress(raw) -> dict | None:
    """解析 ocr_progress JSON 中的 cross 子键（Stage 3 跨页分析子进度）。

    {done, total, label} — label 为中文里程碑（规则校验/LLM 兜底判定/
    LLM 语义分析/完成）。无进行中跨页分析返回 None。
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
        cr = data.get("cross") if isinstance(data, dict) else None
        if isinstance(cr, dict) and cr.get("total"):
            return {
                "done": int(cr.get("done", 0)),
                "total": int(cr.get("total", 0)),
                "label": str(cr.get("label", "")),
            }
    except (ValueError, TypeError):
        pass
    return None


def _parse_failed_pages(raw) -> list[int] | None:
    """解析 `jobs.failed_pages` JSON 字符串 → [页码, ...]。

    该列在 SQLite 里只能存 TEXT（无原生数组类型），但**字段语义是列表**，
    所以接口层必须解出来再交给 JSON 编码。直接透传会得到
    `"failed_pages": "[2, 1]"` —— 一个装着 JSON 的字符串，客户端得二次解码。

    #132（本轮产物级 e2e 发现）：前端按 `Array.isArray(job.failed_pages)`
    取用，收到字符串即**静默退化为 `[]`** ⇒ 失败页数与页码在任务列表里
    永远不显示；而复核页（SSR，`main.py` 早已自行 `json.loads`）却显示正常。
    同一字段在两张页面上行为不一致，用户只会读成"这次没有失败页"——
    #127 专门为"让失败页可见"加的前端渲染，就此形同虚设。
    同一文件里 `_parse_ocr_progress` / `_parse_self_heal_progress` /
    `_parse_cross_progress` 都做了同类的「TEXT → 结构」解析，此列是遗漏。

    None/空/非法 JSON → `None`（= "未能给出失败页清单"），**不伪造 `[]`**：
    `[]` 是"确认零失败页"的断言，GMP 审阅下两者含义不同。非法值记
    warning —— 否则"数据损坏"与"记录真的无异常"在界面上不可区分。
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("failed_pages 不是合法 JSON，按未知处理：%r", str(raw)[:80])
        return None
    if not isinstance(data, list):
        logger.warning("failed_pages 不是数组，按未知处理：%r", str(raw)[:80])
        return None
    try:
        return [int(p) for p in data]
    except (TypeError, ValueError):
        logger.warning("failed_pages 含非页码元素，按未知处理：%r", str(raw)[:80])
        return None


def _derive_phase(status: str, pages_analyzed: int, total_pages: int,
                  cross_started: bool | None = None) -> str:
    """派生阶段指示（SSE 前端进度文案用）。

    translating 覆盖 Stage 2 + Stage 3 两段（两 stage 之间无独立状态位）。
    原"pages_analyzed >= total_pages 推断 cross"在 page_cache 行数少于
    total_pages（OCR 失败页/缺页）时永远不成立，SSE 文案卡在 analyze；
    现优先采信显式信号 cross_progress 子键是否存在（Stage 3 一启动即写入，
    结束时清除）—— 调用方通过 ocr_progress 解析结果传入。
    """
    if status == "ocr_running":
        return "ocr"
    if status == "ocr_done":
        return "analyze"
    if status == "analyzing":
        if cross_started:
            return "cross"
        return ("cross" if total_pages > 0 and pages_analyzed >= total_pages
                else "analyze")
    if status in _TERMINAL_STATUSES:
        return "done"
    return "idle"

@router.get("/{job_id}/stream")
async def stream_job_progress(job_id: str, request: Request = None):
    """SSE 端点：实时推送 job 进度，直到终态。

    前端通过 EventSource 订阅，推送间隔见 `api.jobs._SSE_POLL_SECONDS`
    （当前 2 秒）—— 该常量同时驱动 `retry:` 帧与服务端 sleep，两者必须
    一致（客户端重连不能快过服务端推送）。
    遇到终态 (review/partial_review/error/cancelled/archived) 后推送最终状态并关闭。
    """
    # P2-1: 守卫统一
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    import asyncio
    from fastapi.responses import StreamingResponse
    from api.jobs import _SSE_POLL_SECONDS  # 调用期解析（测试可 patch）

    async def event_generator():
        db = await get_db()
        seq = 0
        yield f"retry: {int(_SSE_POLL_SECONDS * 1000)}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    logger.info(f"[{job_id}] SSE client disconnected, stopping progress stream")
                    return
                try:
                    progress = await _get_job_progress(db, job_id)
                except Exception as e:
                    # P0-4 修复：DB 异常（连接断/锁）此前静默冒泡 → 流中断
                    # 且服务端零日志，"进度卡住"无法定位。记录后推送错误帧
                    # 让前端走重连逻辑。
                    logger.error(f"[{job_id}] SSE progress query failed: {e!r}")
                    seq += 1
                    yield (f"id: {seq}\n"
                           f"data: {json.dumps({'type': 'error', 'message': '进度查询失败'}, ensure_ascii=False)}\n\n")
                    await asyncio.sleep(_SSE_POLL_SECONDS)
                    continue
                if progress is None:
                    seq += 1
                    # 注意：不能用 `event: error` 帧 — SSE 规范中 error 是保留事件
                    # 类型，浏览器收到后立即断开连接且不暴露 data，前端无法区分
                    # "job 不存在" 与网络抖动。改用普通 message 帧携带 type 字段。
                    yield (f"id: {seq}\n"
                           f"data: {json.dumps({'type': 'error', 'message': '任务不存在'}, ensure_ascii=False)}\n\n")
                    return

                payload = json.dumps(progress, ensure_ascii=False)
                seq += 1
                # 每条事件带自增 id：EventSource 断线重连时自动携带
                # Last-Event-ID；快照是幂等全量，重放/重连后立即自愈。
                yield f"id: {seq}\ndata: {payload}\n\n"

                if progress["status"] in _TERMINAL_STATUSES:
                    seq += 1
                    yield f"id: {seq}\nevent: done\ndata: {payload}\n\n"
                    return

                await asyncio.sleep(_SSE_POLL_SECONDS)
        except asyncio.CancelledError:
            # 客户端断开时 Starlette 取消生成器 — 正常路径，不算错误
            raise
        except Exception as e:
            logger.error(f"[{job_id}] SSE stream crashed: {e!r}", exc_info=True)
            raise

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲（如有反向代理）
        },
    )
