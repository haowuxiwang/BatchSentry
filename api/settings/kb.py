"""Knowledge base browse API (read-only) for the settings page.

GET /api/settings/kb?q=&limit=
  -> {source, chapters, entries:[{entry_id,chapter,article_label,text}],
      total_entries, returned}
Local-request guarded like every other read endpoint. Entries come from the
in-memory seed store (no DB round-trip needed).
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from api.settings import router
from core.kb import store

logger = logging.getLogger(__name__)


@router.get("/api/settings/kb")
async def browse_kb(request: Request, q: str = "", limit: int = 50):
    if not is_local_guard(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    limit = max(1, min(limit, 200))
    meta = store.source_meta()
    if not meta["source_id"]:
        raise HTTPException(404, "知识库未装载")
    rows = store.search_index(q, limit=limit)
    return {
        "source": meta,
        "chapters": store.chapters(),
        "total_entries": len(store.entries()),
        "returned": len(rows),
        "entries": rows,
    }


def is_local_guard(request: Request) -> bool:
    from core.security import is_local_request

    return request is None or is_local_request(request)
