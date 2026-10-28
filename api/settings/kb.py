"""Knowledge base browse + source switch API for the settings page.

GET  /api/settings/kb?q=&limit=&source=
  -> {sources:[...], kb_version, total_entries, returned, entries:[...]}
     Omitting ``source`` returns every source's entries (multi-source browse).

PUT  /api/settings/kb/sources   {"disabled": ["fda_21cfr_part11", ...]}
  -> persists config.json ``kb.disabled_sources`` so retrieval skips them.

Both are local-request guarded like every other settings endpoint. Entries come
from the in-memory multi-source store (no DB round-trip needed).
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from pydantic import BaseModel

from api.settings import router
from api.settings.rules import _read_raw_config, _write_raw_config
from core.kb import store

logger = logging.getLogger(__name__)


class KbSourceUpdate(BaseModel):
    """全量替换被禁用来源列表。"""
    disabled: list[str] = []


@router.get("/api/settings/kb")
async def browse_kb(request: Request, q: str = "", limit: int = 50,
                    source: str = ""):
    if not is_local_guard(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    limit = max(1, min(limit, 200))
    srcs = store.sources()
    if not srcs:
        raise HTTPException(404, "知识库未装载")

    source_id = source.strip() or None
    if source_id and source_id not in store.source_ids():
        raise HTTPException(404, f"来源不存在: {source_id}")

    rows = store.search_index(q, limit=limit, source_id=source_id)
    meta = store.source_meta(source_id)
    # 当前被停用的来源（设置页据此回填开关；读失败降级为"全启用"）
    try:
        from config import load_disabled_kb_sources

        disabled = load_disabled_kb_sources()
    except Exception:  # config 不可用不应让浏览接口失败
        disabled = []
    return {
        # 兼容旧前端字段（单源时代的 source/chapters）
        "source": meta,
        "chapters": store.chapters(source_id),
        # 多源（M5）
        "sources": srcs,
        "kb_version": store.kb_version(),
        "active_source": source_id,
        "disabled_sources": disabled,
        "total_entries": (len(store.entries(source_id)) if source_id
                          else len(store.entries())),
        "returned": len(rows),
        "entries": rows,
    }


@router.put("/api/settings/kb/sources")
async def update_kb_sources(req: KbSourceUpdate, request: Request):
    """切换知识库来源（写入 config.json 的 kb.disabled_sources）。

    校验：来源 id 必须存在；不允许禁用全部来源（否则检索恒空，
    排查成本极高，直接拒绝比"静默无引用"更安全）。
    """
    if not is_local_guard(request):
        raise HTTPException(403, "Forbidden (non-local request)")

    known = set(store.source_ids())
    unknown = [s for s in req.disabled if s not in known]
    if unknown:
        raise HTTPException(400, detail={"errors": [f"未知来源: {unknown}"]})
    if known and set(req.disabled) >= known:
        raise HTTPException(
            400,
            detail={"errors": ["至少保留一个知识库来源，否则检索恒为空"]},
        )

    existing = _read_raw_config()
    kb = existing.get("kb")
    if not isinstance(kb, dict):
        kb = {}
    kb["disabled_sources"] = sorted(set(req.disabled))
    existing["kb"] = kb
    _write_raw_config(existing)

    try:
        from db.client import get_db

        db = await get_db()
        await db.execute(
            "INSERT INTO audit_log (job_id, action, detail, created_at) "
            "VALUES (?, ?, ?, datetime('now','localtime'))",
            ("system", "kb_sources_update",
             f"disabled={sorted(set(req.disabled))}"),
        )
        await db.commit()
    except Exception as e:  # 审计写失败不阻断设置保存
        logger.warning(f"Failed to write kb_sources audit log: {e}")

    logger.info("KB sources updated: disabled=%s", sorted(set(req.disabled)))
    return {"ok": True,
            "disabled": sorted(set(req.disabled)),
            "message": f"已更新知识库来源开关（禁用 {len(set(req.disabled))} 个）"}


def is_local_guard(request: Request) -> bool:
    from core.security import is_local_request

    return request is None or is_local_request(request)
