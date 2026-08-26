"""Knowledge base storage: load the bundled GMP2010 seed JSON.

The derived JSON (core/kb/data/gmp2010.json) is generated once at dev time by
scripts/seed_kb.py and committed to git, so end-user machines never need Word
COM. This module owns the in-memory entry table used by the retriever and the
one-time idempotent seeding of the SQLite kb_entries mirror (v8).

Deliberately dependency-free: retrieval works purely from the in-memory copy.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_PATH = Path(__file__).resolve().parent / "data" / "gmp2010.json"

_payload: dict | None = None


def _load() -> dict:
    """Load and cache the seed payload (process-wide singleton)."""
    global _payload
    if _payload is None:
        if not _DATA_PATH.exists():
            logger.warning("[kb] seed data missing: %s", _DATA_PATH)
            _payload = {"source_id": "", "title": "", "chapters": [],
                        "entries": [], "_version": ""}
        else:
            raw = _DATA_PATH.read_bytes()
            _payload = json.loads(raw.decode("utf-8"))
            # RAG 审计规范：知识库版本必须可溯源 —— 取内容哈希前 12 位，
            # 随每次注入写入 llm_call_audit.kb_used。
            _payload["_version"] = hashlib.sha256(raw).hexdigest()[:12]
            logger.info(
                "[kb] loaded %s@%s: %d entries",
                _payload.get("source_id"), _payload["_version"],
                len(_payload.get("entries", [])),
            )
    return _payload


def kb_version() -> str:
    return str(_load().get("_version", ""))


def source_meta() -> dict:
    p = _load()
    return {"source_id": p.get("source_id", ""),
            "title": p.get("title", ""), "effective": p.get("effective", "")}


def chapters() -> list[dict]:
    return _load().get("chapters", [])


def entries() -> list[dict]:
    return _load().get("entries", [])


def get_entry(entry_id: str) -> dict | None:
    for e in entries():
        if e["entry_id"] == entry_id:
            return e
    return None


async def ensure_db_seeded(db) -> int:
    """Mirror entries into the kb_entries table (idempotent, run once).

    Called from lifespan startup; failures are logged and never fatal —
    retrieval itself only needs the in-memory copy.
    """
    rows = entries()
    if not rows:
        return 0
    cur = await db.execute("SELECT COUNT(*) FROM kb_entries")
    if (await cur.fetchone())[0] >= len(rows):
        return 0
    await db.execute("DELETE FROM kb_entries")
    meta = source_meta()
    await db.executemany(
        "INSERT INTO kb_entries (entry_id, source_id, chapter, "
        "article_label, no, text) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (e["entry_id"], meta["source_id"], e["chapter"],
             e["article_label"], e["no"], e["text"])
            for e in rows
        ],
    )
    await db.commit()
    logger.info("[kb] seeded kb_entries with %d rows", len(rows))
    return len(rows)


def search_index(q: str, limit: int = 20) -> list[dict]:
    """Naive substring browse for the settings page (not BM25)."""
    needle = (q or "").strip()
    out = []
    for e in entries():
        if not needle or needle in e["text"] or needle in e["article_label"] \
                or needle in e["chapter"]:
            out.append(e)
            if len(out) >= limit:
                break
    return out
